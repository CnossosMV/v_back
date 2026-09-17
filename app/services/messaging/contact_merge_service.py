"""
Contact Merge Service
Handles merging two contacts into one, with property resolution and audit logging.
"""
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy.orm import Session

from app.models.messaging import (
    MessagingUser, MessagingEvent, MessagingLog, ContactIdentity, ContactMergeLog,
    MergeSuggestion, MessagingAnonymousProfile, Account, ContactAccountAssociation,
    ContactVerificationItem, ContactVerificationState, MessagingAdsConsentEvidence,
    MessagingAudienceMembership, MessagingAudienceSyncItem,
)
from app.models.campaigns import (
    ContactEndpoint,
    ContactGroupMembership,
    ContactPermissionEvidence,
    ContactVerificationRequest,
)
from app.models import (
    FunnelEnrollment, FunnelEnrollmentLog, UserFeatureStore,
    UserScoreSnapshot, ContactLedger, EventActionCooldown,
    ContactRoutingState, ContactRateWindow, ChatSession, SupportTicket, SendLog,
    DeliveryFeedback, EventActionExecution, ScheduledEventAction,
    ContactPosition, ContactPositionTransition,
)


class ContactMergeService:
    def __init__(self, db: Session):
        self.db = db

    def merge_contacts(self, project_id: int, winner_id: int, loser_id: int,
                       triggered_by: str, triggered_by_user_id: int = None) -> ContactMergeLog:
        """Full contact merge:
        1. Validate: check merge chain depth <= 3
        2. Validate: check funnel conflict — if both have active awaiting_reply enrollments, create suggestion instead
        3. Determine winner (oldest first_seen_at; contact_id holder overrides)
        4. Snapshot both contacts pre-merge
        5. Property resolution: most_recent_wins for scalars, union for tags/identities,
           restrictive_union for opted_out_channels + consent, sum for scores,
           earliest first_seen_at, latest last_seen_at
        6. Transfer identities from loser to winner in contact_identities
        7. Transfer funnel enrollments (user_id update) + resolve duplicates (further-ahead survives)
        8. Transfer events, scores, feature store, ledger, cooldowns
        9. Transfer routing states (update contact_identifier)
        10. Set loser.status='merged', loser.merged_into=winner.id
        11. Flatten tombstone chains
        12. Write merge log
        """
        # Get both contacts
        winner = self.db.query(MessagingUser).filter(
            MessagingUser.id == winner_id,
            MessagingUser.project_id == project_id
        ).first()
        loser = self.db.query(MessagingUser).filter(
            MessagingUser.id == loser_id,
            MessagingUser.project_id == project_id
        ).first()

        if not winner or not loser:
            raise ValueError("Both contacts must exist in the same project")
        if winner.status == 'merged' or loser.status == 'merged':
            raise ValueError("Cannot merge already-merged contacts")
        if winner_id == loser_id:
            raise ValueError("Cannot merge a contact with itself")

        # Check merge chain depth
        chain_depth = self._get_chain_depth(winner_id)
        if chain_depth >= 3:
            raise ValueError("Merge chain depth limit (3) exceeded")

        # Check funnel conflict: both have active awaiting_reply enrollments
        winner_active = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.user_id == winner_id,
            FunnelEnrollment.status == 'active'
        ).all()
        loser_active = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.user_id == loser_id,
            FunnelEnrollment.status == 'active'
        ).all()

        # If both have active enrollments in same funnel with awaiting_reply, create suggestion instead
        winner_funnel_ids = {e.funnel_id for e in winner_active}
        loser_funnel_ids = {e.funnel_id for e in loser_active}
        conflicting = winner_funnel_ids & loser_funnel_ids
        if conflicting and triggered_by != 'manual':
            return self.create_merge_suggestion(
                project_id, winner_id, loser_id,
                'funnel_conflict', 'medium'
            )

        # Determine true winner: oldest first_seen_at, but contact_id holder overrides
        if loser.external_id and not winner.external_id:
            winner, loser = loser, winner
            winner_id, loser_id = loser_id, winner_id
        elif (winner.first_seen_at and loser.first_seen_at and
              winner.first_seen_at > loser.first_seen_at and not winner.external_id):
            winner, loser = loser, winner
            winner_id, loser_id = loser_id, winner_id

        # Snapshot both pre-merge
        snapshot_winner = self._snapshot_contact(winner)
        snapshot_loser = self._snapshot_contact(loser)

        # Property resolution
        properties_resolved = self._resolve_properties(winner, loser)

        # Apply resolved properties to winner
        if loser.email and not winner.email:
            winner.email = loser.email
            winner.email_hash = loser.email_hash
        if loser.phone and not winner.phone:
            winner.phone = loser.phone
            winner.phone_hash = loser.phone_hash
            winner.phone_e164 = loser.phone_e164
        if loser.name and not winner.name:
            winner.name = loser.name

        # Merge properties dict
        merged_props = loser.properties or {}
        merged_props.update(winner.properties or {})
        winner.properties = merged_props

        # Union tags
        winner_tags = winner.tags or []
        loser_tags = loser.tags or []
        winner.tags = list(set(winner_tags + loser_tags))

        # Restrictive consent union
        winner.consent_channels = self._merge_consent_restrictive(
            winner.consent_channels, loser.consent_channels
        )
        email_consent = (winner.consent_channels or {}).get("email")
        if isinstance(email_consent, dict):
            winner.consent_marketing = bool(email_consent.get("granted"))
            winner.consent_version = (
                email_consent.get("policy_version")
                or winner.consent_version
            )
        else:
            winner.consent_marketing = bool(
                winner.consent_marketing and loser.consent_marketing
            )
        winner.consent_analytics = bool(
            winner.consent_analytics and loser.consent_analytics
        )

        # A merge must never erase a harder suppression state. These legacy
        # columns remain active Guardian inputs alongside consent_channels.
        winner.global_opt_out = bool(
            winner.global_opt_out or loser.global_opt_out
        )
        winner.is_subscribed = bool(
            winner.is_subscribed and loser.is_subscribed
        )
        if loser.is_blocked:
            if not winner.is_blocked:
                winner.is_blocked = True
                winner.blocked_at = loser.blocked_at
                winner.blocked_reason = loser.blocked_reason
            elif (loser.blocked_at and winner.blocked_at
                  and loser.blocked_at < winner.blocked_at):
                winner.blocked_at = loser.blocked_at
                winner.blocked_reason = loser.blocked_reason or winner.blocked_reason

        # Restrictive opted_out_channels union
        winner_opted = winner.opted_out_channels or []
        loser_opted = loser.opted_out_channels or []
        winner.opted_out_channels = list(set(winner_opted + loser_opted))

        # Carry locale/timezone/send-window columns when the winner lacks them
        # (loser's observed timezone is better than none for send-time logic).
        if loser.timezone and not winner.timezone:
            winner.timezone = loser.timezone
        if loser.locale and not winner.locale:
            winner.locale = loser.locale
        if loser.send_windows and not winner.send_windows:
            winner.send_windows = loser.send_windows

        # Restrictive automation-pause union — a merge must never un-pause.
        if loser.automations_paused:
            if not winner.automations_paused:
                winner.automations_paused = True
                winner.automations_paused_at = loser.automations_paused_at
                winner.automations_paused_reason = loser.automations_paused_reason
                winner.automations_pause_mode = loser.automations_pause_mode
            elif (loser.automations_paused_at and winner.automations_paused_at
                    and loser.automations_paused_at < winner.automations_paused_at):
                # Both paused: keep winner's mode/reason, earliest paused_at
                winner.automations_paused_at = loser.automations_paused_at

        # Earliest first_seen_at, latest last_seen_at
        if loser.first_seen_at and (not winner.first_seen_at or loser.first_seen_at < winner.first_seen_at):
            winner.first_seen_at = loser.first_seen_at
        if loser.last_seen_at and (not winner.last_seen_at or loser.last_seen_at > winner.last_seen_at):
            winner.last_seen_at = loser.last_seen_at

        # Transfer identities
        identities_transferred = []
        loser_identities = self.db.query(ContactIdentity).filter(
            ContactIdentity.user_id == loser_id,
            ContactIdentity.project_id == project_id
        ).all()
        for identity in loser_identities:
            # Check if winner already has this identity
            existing = self.db.query(ContactIdentity).filter(
                ContactIdentity.project_id == project_id,
                ContactIdentity.identity_type == identity.identity_type,
                ContactIdentity.identity_value == identity.identity_value,
                ContactIdentity.id != identity.id,
            ).first()
            if existing and existing.user_id == winner_id:
                self.db.delete(identity)
            elif existing:
                # Conflict — delete loser's, winner keeps theirs
                self.db.delete(identity)
            else:
                identity.user_id = winner_id
                identities_transferred.append({
                    "type": identity.identity_type,
                    "value": identity.identity_value
                })

        # The canonical external_id must itself be resolvable even when the
        # winner was created by an older ingestion path that did not
        # materialize contact_identities.
        canonical_identity = self.db.query(ContactIdentity).filter(
            ContactIdentity.project_id == project_id,
            ContactIdentity.identity_type == "contact_id",
            ContactIdentity.identity_value == winner.external_id,
        ).first()
        if canonical_identity is None:
            self.db.add(ContactIdentity(
                project_id=project_id,
                user_id=winner_id,
                identity_type="contact_id",
                identity_value=winner.external_id,
                verified=False,
                source="merge",
            ))

        # Transfer funnel enrollments
        for enrollment in loser_active:
            # Check if winner has same funnel enrollment
            winner_enrollment = self.db.query(FunnelEnrollment).filter(
                FunnelEnrollment.user_id == winner_id,
                FunnelEnrollment.funnel_id == enrollment.funnel_id,
                FunnelEnrollment.status == 'active'
            ).first()
            if winner_enrollment:
                # Keep the further-ahead one (higher current step position)
                winner_step_pos = winner_enrollment.current_step.position if winner_enrollment.current_step else 0
                loser_step_pos = enrollment.current_step.position if enrollment.current_step else 0
                if loser_step_pos > winner_step_pos:
                    winner_enrollment.status = 'merged_out'
                    enrollment.user_id = winner_id
                else:
                    enrollment.status = 'merged_out'
            else:
                enrollment.user_id = winner_id

        # Transfer inactive enrollments too
        self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.user_id == loser_id,
            FunnelEnrollment.status != 'active'
        ).update({"user_id": winner_id}, synchronize_session=False)

        # Transfer events
        self.db.query(MessagingEvent).filter(
            MessagingEvent.user_id == loser_id
        ).update({"user_id": winner_id}, synchronize_session=False)

        # Transfer scores (sum)
        self._transfer_scores(project_id, winner_id, loser_id)

        # Transfer feature store
        self._transfer_feature_store(project_id, winner_id, loser_id)

        # Transfer ledger
        self.db.query(ContactLedger).filter(
            ContactLedger.user_id == loser_id
        ).update({"user_id": winner_id}, synchronize_session=False)

        # Transfer cooldowns without violating the one-row-per-action/user
        # invariant. The later expiry is the restrictive choice: a merge must
        # never make an automation eligible earlier.
        self._transfer_cooldowns(project_id, winner_id, loser_id)

        # Transfer routing states
        self.db.query(ContactRoutingState).filter(
            ContactRoutingState.project_id == project_id,
            ContactRoutingState.contact_identifier == loser.external_id
        ).update({"contact_identifier": winner.external_id}, synchronize_session=False)

        # Preserve and consolidate recent-send cap history keyed by the
        # contact's external id. Multiple active windows would otherwise make
        # the limiter's `.first()` lookup under-count the combined history.
        self._transfer_rate_windows(
            project_id, winner.external_id, loser.external_id
        )

        # Transfer account associations
        loser_assocs = self.db.query(ContactAccountAssociation).filter(
            ContactAccountAssociation.user_id == loser_id
        ).all()
        for assoc in loser_assocs:
            existing = self.db.query(ContactAccountAssociation).filter(
                ContactAccountAssociation.user_id == winner_id,
                ContactAccountAssociation.account_id == assoc.account_id,
                ContactAccountAssociation.project_id == project_id
            ).first()
            if existing:
                self.db.delete(assoc)
            else:
                assoc.user_id = winner_id

        # Transfer anonymous profiles
        self.db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.merged_to_user_id == loser_id
        ).update({"merged_to_user_id": winner_id}, synchronize_session=False)

        # Transfer chat sessions
        self.db.query(ChatSession).filter(
            ChatSession.contact_id == loser_id
        ).update({"contact_id": winner_id}, synchronize_session=False)

        # Transfer support tickets
        self.db.query(SupportTicket).filter(
            SupportTicket.contact_id == loser_id
        ).update({"contact_id": winner_id}, synchronize_session=False)

        # Transfer send logs
        self.db.query(SendLog).filter(
            SendLog.user_id == loser_id
        ).update({"user_id": winner_id}, synchronize_session=False)

        # Transfer messaging logs
        self.db.query(MessagingLog).filter(
            MessagingLog.user_id == loser_id
        ).update({"user_id": winner_id}, synchronize_session=False)

        # Transfer delivery feedback
        self.db.query(DeliveryFeedback).filter(
            DeliveryFeedback.user_id == loser_id
        ).update({"user_id": winner_id}, synchronize_session=False)

        # Transfer event action executions
        self.db.query(EventActionExecution).filter(
            EventActionExecution.user_id == loser_id
        ).update({"user_id": winner_id}, synchronize_session=False)

        # Transfer scheduled event actions
        self.db.query(ScheduledEventAction).filter(
            ScheduledEventAction.user_id == loser_id
        ).update({"user_id": winner_id}, synchronize_session=False)

        # Canonical contact data used by campaigns and verification. Campaign
        # recipients are deliberately not reassigned: they are immutable
        # delivery history and continue to identify the original tombstone.
        self._merge_campaign_contact_data(project_id, winner, loser)
        self._merge_audience_data(project_id, winner_id, loser_id)
        self._merge_positions(project_id, winner_id, loser_id)

        # Set loser as merged
        loser.status = 'merged'
        loser.merged_into = winner_id

        # Flatten tombstone chains
        self._flatten_tombstones(project_id, winner_id)

        # Write merge log
        merge_log = ContactMergeLog(
            project_id=project_id,
            winner_id=winner_id,
            loser_id=loser_id,
            triggered_by=triggered_by,
            triggered_by_user_id=triggered_by_user_id,
            snapshot_winner=snapshot_winner,
            snapshot_loser=snapshot_loser,
            identities_transferred=identities_transferred,
            properties_resolved=properties_resolved
        )
        self.db.add(merge_log)
        self.db.commit()
        self.db.refresh(merge_log)

        return merge_log

    def _merge_campaign_contact_data(
        self,
        project_id: int,
        winner: MessagingUser,
        loser: MessagingUser,
    ) -> None:
        """Move provider-neutral contact data without rewriting send history.

        Endpoints are canonicalized by their project/contact/type/hash key.
        Permission evidence is append-only history, so duplicate evidence rows
        are retained and only their owner/endpoint references are canonicalized.
        """
        winner_endpoints = self.db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == winner.id,
        ).all()
        loser_endpoints = self.db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == loser.id,
        ).all()
        winner_by_key = {
            (endpoint.endpoint_type, endpoint.value_hash): endpoint
            for endpoint in winner_endpoints
        }
        endpoint_id_map: dict[int, int] = {}
        duplicate_endpoints: list[ContactEndpoint] = []

        for endpoint in loser_endpoints:
            key = (endpoint.endpoint_type, endpoint.value_hash)
            canonical = winner_by_key.get(key)
            if canonical is None:
                endpoint.user_id = winner.id
                winner_by_key[key] = endpoint
                winner_endpoints.append(endpoint)
                endpoint_id_map[endpoint.id] = endpoint.id
                continue

            endpoint_id_map[endpoint.id] = canonical.id
            duplicate_endpoints.append(endpoint)
            if endpoint.first_seen_at and (
                not canonical.first_seen_at or endpoint.first_seen_at < canonical.first_seen_at
            ):
                canonical.first_seen_at = endpoint.first_seen_at
            if endpoint.last_seen_at and (
                not canonical.last_seen_at or endpoint.last_seen_at > canonical.last_seen_at
            ):
                canonical.last_seen_at = endpoint.last_seen_at
            canonical.endpoint_metadata = {
                **(endpoint.endpoint_metadata or {}),
                **(canonical.endpoint_metadata or {}),
            }
            if canonical.status != "active" and endpoint.status == "active":
                canonical.status = "active"

        # Evidence is compliance history: retain every row, including multiple
        # observations for the same canonical endpoint.
        for evidence in self.db.query(ContactPermissionEvidence).filter(
            ContactPermissionEvidence.project_id == project_id,
            ContactPermissionEvidence.user_id == loser.id,
        ).all():
            evidence.user_id = winner.id
            if evidence.endpoint_id in endpoint_id_map:
                evidence.endpoint_id = endpoint_id_map[evidence.endpoint_id]

        # Durable verification requests follow the canonical contact/endpoint;
        # their attempt/result fields remain unchanged for auditability.
        for request in self.db.query(ContactVerificationRequest).filter(
            ContactVerificationRequest.project_id == project_id,
            ContactVerificationRequest.user_id == loser.id,
        ).all():
            request.user_id = winner.id
            if request.endpoint_id in endpoint_id_map:
                request.endpoint_id = endpoint_id_map[request.endpoint_id]

        # Verification items are immutable attempt history. Repoint them when
        # the canonical user has no item for the same job/type; otherwise keep
        # the duplicate attached to the tombstone instead of deleting audit.
        for item in self.db.query(ContactVerificationItem).filter(
            ContactVerificationItem.project_id == project_id,
            ContactVerificationItem.user_id == loser.id,
        ).all():
            existing = self.db.query(ContactVerificationItem).filter(
                ContactVerificationItem.job_id == item.job_id,
                ContactVerificationItem.user_id == winner.id,
                ContactVerificationItem.verification_type == item.verification_type,
                ContactVerificationItem.id != item.id,
            ).first()
            if existing is not None:
                continue
            item.user_id = winner.id
            if item.endpoint_id in endpoint_id_map:
                item.endpoint_id = endpoint_id_map[item.endpoint_id]

        self._merge_group_memberships(project_id, winner.id, loser.id)
        self._merge_verification_states(
            project_id,
            winner,
            loser,
            endpoint_id_map=endpoint_id_map,
        )

        # ON DELETE SET NULL intentionally preserves any CampaignRecipient
        # pointing at a duplicate endpoint. Its user_id and endpoint_hash are
        # historical snapshot data and must not be rewritten during a merge.
        for endpoint in duplicate_endpoints:
            self.db.delete(endpoint)

        self._normalize_primary_endpoints(winner, winner_endpoints, duplicate_endpoints)

    def _merge_audience_data(self, project_id: int, winner_id: int, loser_id: int) -> None:
        """Canonicalize ads/audience state while retaining append-only history."""
        winner_memberships = {
            membership.audience_id: membership
            for membership in self.db.query(MessagingAudienceMembership).filter(
                MessagingAudienceMembership.project_id == project_id,
                MessagingAudienceMembership.user_id == winner_id,
            ).all()
        }
        loser_memberships = self.db.query(MessagingAudienceMembership).filter(
            MessagingAudienceMembership.project_id == project_id,
            MessagingAudienceMembership.user_id == loser_id,
        ).all()
        for membership in loser_memberships:
            existing = winner_memberships.get(membership.audience_id)
            if existing is None:
                membership.user_id = winner_id
                winner_memberships[membership.audience_id] = membership
                continue

            # Exclusion is the safe/restrictive result. For equal states, keep
            # the most recently evaluated evidence and provider status.
            if membership.state == "excluded" and existing.state != "excluded":
                existing.state = membership.state
                existing.eligibility_reason = membership.eligibility_reason
                existing.eligibility_details = membership.eligibility_details
                existing.identifiers_present = membership.identifiers_present
            if membership.last_evaluated_at and (
                not existing.last_evaluated_at
                or membership.last_evaluated_at > existing.last_evaluated_at
            ):
                existing.last_evaluated_at = membership.last_evaluated_at
                if membership.state == existing.state:
                    existing.eligibility_reason = membership.eligibility_reason
                    existing.eligibility_details = membership.eligibility_details
                    existing.identifiers_present = membership.identifiers_present
            if membership.last_synced_at and (
                not existing.last_synced_at
                or membership.last_synced_at > existing.last_synced_at
            ):
                existing.last_synced_at = membership.last_synced_at
                existing.provider_status = membership.provider_status
            self.db.delete(membership)

        # Consent observations and provider sync items are historical ledgers;
        # reassignment preserves every row and does not trigger a provider sync.
        self.db.query(MessagingAdsConsentEvidence).filter(
            MessagingAdsConsentEvidence.project_id == project_id,
            MessagingAdsConsentEvidence.user_id == loser_id,
        ).update({"user_id": winner_id}, synchronize_session=False)
        self.db.query(MessagingAudienceSyncItem).filter(
            MessagingAudienceSyncItem.project_id == project_id,
            MessagingAudienceSyncItem.user_id == loser_id,
        ).update({"user_id": winner_id}, synchronize_session=False)

    def _merge_positions(self, project_id: int, winner_id: int, loser_id: int) -> None:
        """Keep one current position per model and retain all transitions."""
        winner_positions = {
            position.lifecycle_model_id: position
            for position in self.db.query(ContactPosition).filter(
                ContactPosition.project_id == project_id,
                ContactPosition.user_id == winner_id,
            ).all()
        }
        loser_positions = self.db.query(ContactPosition).filter(
            ContactPosition.project_id == project_id,
            ContactPosition.user_id == loser_id,
        ).all()
        for position in loser_positions:
            existing = winner_positions.get(position.lifecycle_model_id)
            if existing is None:
                position.user_id = winner_id
                winner_positions[position.lifecycle_model_id] = position
                continue
            if position.computed_at and (
                not existing.computed_at or position.computed_at > existing.computed_at
            ):
                for field in (
                    "type", "stage", "age_bucket", "position_entered_at",
                    "type_entered_at", "stage_entered_at", "computed_at",
                    "provenance", "explanation",
                ):
                    setattr(existing, field, getattr(position, field))
            self.db.delete(position)

        self.db.query(ContactPositionTransition).filter(
            ContactPositionTransition.project_id == project_id,
            ContactPositionTransition.user_id == loser_id,
        ).update({"user_id": winner_id}, synchronize_session=False)

    def _merge_group_memberships(
        self,
        project_id: int,
        winner_id: int,
        loser_id: int,
    ) -> None:
        winner_memberships = {
            membership.group_id: membership
            for membership in self.db.query(ContactGroupMembership).filter(
                ContactGroupMembership.project_id == project_id,
                ContactGroupMembership.user_id == winner_id,
            ).all()
        }
        loser_memberships = self.db.query(ContactGroupMembership).filter(
            ContactGroupMembership.project_id == project_id,
            ContactGroupMembership.user_id == loser_id,
        ).all()
        for membership in loser_memberships:
            existing = winner_memberships.get(membership.group_id)
            if existing is None:
                membership.user_id = winner_id
                winner_memberships[membership.group_id] = membership
                continue

            # An explicit exclusion is more restrictive than inclusion and
            # must survive a merge. Keep the latest evaluation metadata.
            if membership.state == "excluded":
                existing.state = "excluded"
                existing.source = membership.source
                existing.reason = membership.reason
            if membership.evaluated_at and (
                not existing.evaluated_at or membership.evaluated_at > existing.evaluated_at
            ):
                existing.evaluated_at = membership.evaluated_at
            self.db.delete(membership)

    def _merge_verification_states(
        self,
        project_id: int,
        winner: MessagingUser,
        loser: MessagingUser,
        endpoint_id_map: Optional[dict[int, int]] = None,
    ) -> None:
        endpoint_id_map = endpoint_id_map or {}
        loser_endpoint_states = self.db.query(ContactVerificationState).filter(
            ContactVerificationState.project_id == project_id,
            ContactVerificationState.user_id == loser.id,
            ContactVerificationState.endpoint_id.isnot(None),
        ).all()
        for loser_state in loser_endpoint_states:
            canonical_endpoint_id = endpoint_id_map.get(
                loser_state.endpoint_id,
                loser_state.endpoint_id,
            )
            existing = self.db.query(ContactVerificationState).filter(
                ContactVerificationState.project_id == project_id,
                ContactVerificationState.endpoint_id == canonical_endpoint_id,
                ContactVerificationState.verification_type == loser_state.verification_type,
                ContactVerificationState.id != loser_state.id,
            ).first()
            if existing is None:
                loser_state.user_id = winner.id
                loser_state.endpoint_id = canonical_endpoint_id
                continue
            if loser_state.checked_at > existing.checked_at:
                self._copy_verification_state(existing, loser_state)
            existing.user_id = winner.id
            self.db.delete(loser_state)

        # Legacy states have no endpoint_id and retain the old one-row-per-type
        # uniqueness rule. Only a result matching the resolved winner identity
        # may become the winner's current legacy state.
        winner_states = {
            state.verification_type: state
            for state in self.db.query(ContactVerificationState).filter(
                ContactVerificationState.project_id == project_id,
                ContactVerificationState.user_id == winner.id,
                ContactVerificationState.endpoint_id.is_(None),
            ).all()
        }
        loser_states = self.db.query(ContactVerificationState).filter(
            ContactVerificationState.project_id == project_id,
            ContactVerificationState.user_id == loser.id,
            ContactVerificationState.endpoint_id.is_(None),
        ).all()
        current_hashes = {
            "email": winner.email_hash,
            "whatsapp": winner.phone_hash,
        }
        for loser_state in loser_states:
            current_hash = current_hashes.get(loser_state.verification_type)
            winner_state = winner_states.get(loser_state.verification_type)
            loser_matches = bool(current_hash and loser_state.identifier_hash == current_hash)
            winner_matches = bool(
                winner_state and current_hash and winner_state.identifier_hash == current_hash
            )
            if not winner_state and loser_matches:
                loser_state.user_id = winner.id
                winner_states[loser_state.verification_type] = loser_state
                continue
            if winner_state and loser_matches and (
                not winner_matches or loser_state.checked_at > winner_state.checked_at
            ):
                self._copy_verification_state(winner_state, loser_state)
            if winner_state:
                self.db.delete(loser_state)

    @staticmethod
    def _copy_verification_state(
        target: ContactVerificationState,
        source: ContactVerificationState,
    ) -> None:
        for field in (
            "identifier_hash", "provider", "provider_key_source", "provider_version",
            "canonical_status", "provider_status", "provider_metadata",
            "checked_at", "expires_at", "last_attempt_status",
            "last_attempt_at", "last_error_code",
        ):
            setattr(target, field, getattr(source, field))

    @staticmethod
    def _normalize_primary_endpoints(
        winner: MessagingUser,
        endpoints: list[ContactEndpoint],
        deleted: list[ContactEndpoint],
    ) -> None:
        deleted_ids = {endpoint.id for endpoint in deleted}
        by_type: dict[str, list[ContactEndpoint]] = {}
        for endpoint in endpoints:
            if endpoint.id not in deleted_ids:
                by_type.setdefault(endpoint.endpoint_type, []).append(endpoint)

        desired_hashes = {
            "email": winner.email_hash,
            "phone": winner.phone_hash,
            "whatsapp": winner.phone_hash,
        }
        for endpoint_type, candidates in by_type.items():
            active_candidates = [
                endpoint for endpoint in candidates if endpoint.status == "active"
            ]
            if not active_candidates:
                for endpoint in candidates:
                    endpoint.is_primary = False
                continue
            desired_hash = desired_hashes.get(endpoint_type)
            selected = next(
                (
                    endpoint for endpoint in active_candidates
                    if desired_hash and endpoint.value_hash == desired_hash
                ),
                None,
            )
            if selected is None:
                selected = next(
                    (endpoint for endpoint in active_candidates if endpoint.is_primary),
                    None,
                )
            if selected is None:
                selected = max(
                    active_candidates,
                    key=lambda endpoint: endpoint.last_seen_at or endpoint.created_at or datetime.min,
                )
            for endpoint in candidates:
                endpoint.is_primary = endpoint is selected

    def resolve_tombstone(self, project_id: int, user_id: int) -> int:
        """Follow merged_into chain to final active contact. Max 3 hops."""
        current_id = user_id
        for _ in range(3):
            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == current_id,
                MessagingUser.project_id == project_id
            ).first()
            if not user or user.status != 'merged' or not user.merged_into:
                return current_id
            current_id = user.merged_into
        return current_id

    def create_merge_suggestion(self, project_id: int, contact_a_id: int,
                                contact_b_id: int, match_reason: str,
                                match_confidence: str) -> MergeSuggestion:
        """Create a merge suggestion for manual review."""
        # Check if suggestion already exists
        existing = self.db.query(MergeSuggestion).filter(
            MergeSuggestion.project_id == project_id,
            MergeSuggestion.status == 'pending',
            ((MergeSuggestion.contact_a_id == contact_a_id) & (MergeSuggestion.contact_b_id == contact_b_id)) |
            ((MergeSuggestion.contact_a_id == contact_b_id) & (MergeSuggestion.contact_b_id == contact_a_id))
        ).first()
        if existing:
            return existing

        suggestion = MergeSuggestion(
            project_id=project_id,
            contact_a_id=contact_a_id,
            contact_b_id=contact_b_id,
            match_reason=match_reason,
            match_confidence=match_confidence,
            status='pending'
        )
        self.db.add(suggestion)
        self.db.commit()
        self.db.refresh(suggestion)
        return suggestion

    def accept_merge_suggestion(
        self,
        suggestion_id: int,
        user_id: int,
        project_id: int,
    ) -> ContactMergeLog:
        """Accept a merge suggestion and perform the merge."""
        suggestion = self.db.query(MergeSuggestion).filter(
            MergeSuggestion.id == suggestion_id,
            MergeSuggestion.project_id == project_id,
            MergeSuggestion.status == 'pending'
        ).first()
        if not suggestion:
            raise ValueError("Suggestion not found or already processed")

        suggestion.status = 'accepted'
        suggestion.reviewed_by = user_id
        suggestion.reviewed_at = datetime.utcnow()

        merge_log = self.merge_contacts(
            suggestion.project_id,
            suggestion.contact_a_id,
            suggestion.contact_b_id,
            triggered_by='manual',
            triggered_by_user_id=user_id
        )
        self.db.commit()
        return merge_log

    def reject_merge_suggestion(
        self,
        suggestion_id: int,
        user_id: int,
        project_id: int,
    ) -> None:
        """Reject a merge suggestion."""
        suggestion = self.db.query(MergeSuggestion).filter(
            MergeSuggestion.id == suggestion_id,
            MergeSuggestion.project_id == project_id,
            MergeSuggestion.status == 'pending'
        ).first()
        if not suggestion:
            raise ValueError("Suggestion not found or already processed")

        suggestion.status = 'rejected'
        suggestion.reviewed_by = user_id
        suggestion.reviewed_at = datetime.utcnow()
        self.db.commit()

    def undo_merge(self, merge_log_id: int, project_id: int) -> ContactMergeLog:
        """Fail closed until merge logs persist a complete reversal manifest."""
        merge_log = self.db.query(ContactMergeLog).filter(
            ContactMergeLog.id == merge_log_id,
            ContactMergeLog.project_id == project_id
        ).first()
        if not merge_log:
            raise ValueError("Merge log not found")
        if merge_log.undone_at is not None:
            raise ValueError("This merge has already been undone")
        raise ValueError(
            "Merge cannot be safely undone because the merge log does not "
            "contain a complete reversal manifest"
        )

    def _get_chain_depth(self, user_id: int) -> int:
        """Measure actual tombstone chain depth: how many hops from this user
        following merged_into until we reach a non-merged contact.
        Fan-in (many contacts merged into one winner) is safe and unlimited.
        Only deep chains (A→B→C→D) are a concern for lookup performance."""
        current_id = user_id
        for depth in range(10):
            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == current_id
            ).first()
            if not user or user.status != 'merged' or not user.merged_into:
                return depth
            current_id = user.merged_into
        return 10

    def _snapshot_contact(self, user: MessagingUser) -> dict:
        """Create a JSON snapshot of a contact for audit logging."""
        return {
            "id": user.id,
            "external_id": user.external_id,
            "email": user.email,
            "phone": user.phone,
            "name": user.name,
            "properties": user.properties,
            "tags": user.tags,
            "status": user.status,
            "lifecycle_stage": user.lifecycle_stage,
            "first_seen_at": user.first_seen_at.isoformat() if user.first_seen_at else None,
            "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
            "consent_channels": user.consent_channels,
            "consent_marketing": user.consent_marketing,
            "consent_analytics": user.consent_analytics,
            "global_opt_out": user.global_opt_out,
            "is_subscribed": user.is_subscribed,
            "opted_out_channels": user.opted_out_channels,
            "is_blocked": user.is_blocked,
            "blocked_at": user.blocked_at.isoformat() if user.blocked_at else None,
            "blocked_reason": user.blocked_reason,
            "timezone": user.timezone,
            "locale": user.locale,
            "send_windows": user.send_windows,
        }

    def _resolve_properties(self, winner: MessagingUser, loser: MessagingUser) -> dict:
        """Document which property resolution was applied."""
        return {
            "email": winner.email or loser.email,
            "phone": winner.phone or loser.phone,
            "name": winner.name or loser.name,
            "strategy": "most_recent_wins_for_scalars_union_for_collections"
        }

    def _merge_consent_restrictive(self, winner_consent: dict, loser_consent: dict) -> dict:
        """Restrictive union: consent only if BOTH had it per channel."""
        if not winner_consent and not loser_consent:
            return None
        winner_consent = winner_consent or {}
        loser_consent = loser_consent or {}
        all_channels = set(list(winner_consent.keys()) + list(loser_consent.keys()))
        merged = {}
        for channel in all_channels:
            w_present = channel in winner_consent
            l_present = channel in loser_consent
            w = winner_consent.get(channel, {})
            l = loser_consent.get(channel, {})
            w_granted = w.get("granted", False) if isinstance(w, dict) else False
            l_granted = l.get("granted", False) if isinstance(l, dict) else False
            granted = bool(w_granted and l_granted)

            # Preserve the evidence metadata that explains the restrictive
            # result. An explicit denial/withdrawal wins; if one projection is
            # missing, retain the existing evidence but mark the merged state
            # unknown rather than inventing a denial source.
            restrictive_entry = None
            if w_present and not w_granted and isinstance(w, dict):
                restrictive_entry = w
            elif l_present and not l_granted and isinstance(l, dict):
                restrictive_entry = l
            source_entry = restrictive_entry or (
                w if isinstance(w, dict) and w else l if isinstance(l, dict) else {}
            )
            merged_entry = dict(source_entry)
            merged_entry["granted"] = granted
            if granted:
                merged_entry["status"] = "granted"
            elif restrictive_entry is not None:
                merged_entry["status"] = restrictive_entry.get("status") or "denied"
            else:
                merged_entry["status"] = "unknown"
            merged_entry["source"] = merged_entry.get("source") or "merge"
            merged_entry["timestamp"] = (
                merged_entry.get("timestamp")
                or merged_entry.get("captured_at")
                or datetime.utcnow().isoformat()
            )
            merged[channel] = merged_entry
        return merged

    def _flatten_tombstones(self, project_id: int, final_winner_id: int):
        """Ensure no multi-hop tombstone chains exist — all point directly to winner."""
        # Find all contacts that are merged into contacts that are merged into winner
        merged_contacts = self.db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.status == 'merged'
        ).all()
        for contact in merged_contacts:
            resolved = self.resolve_tombstone(project_id, contact.id)
            if resolved != contact.merged_into and resolved == final_winner_id:
                contact.merged_into = final_winner_id

    def _transfer_scores(self, project_id: int, winner_id: int, loser_id: int):
        """Transfer score snapshots: sum scores for same definitions."""
        loser_scores = self.db.query(UserScoreSnapshot).filter(
            UserScoreSnapshot.user_id == loser_id,
            UserScoreSnapshot.project_id == project_id
        ).all()
        for ls in loser_scores:
            ws = self.db.query(UserScoreSnapshot).filter(
                UserScoreSnapshot.user_id == winner_id,
                UserScoreSnapshot.score_definition_id == ls.score_definition_id
            ).first()
            if ws:
                ws.score = ws.score + ls.score
                ws.calculated_at = datetime.utcnow()
                self.db.delete(ls)
            else:
                ls.user_id = winner_id

    def _transfer_feature_store(self, project_id: int, winner_id: int, loser_id: int):
        """Transfer feature store entries: sum counts for same events."""
        loser_features = self.db.query(UserFeatureStore).filter(
            UserFeatureStore.user_id == loser_id,
            UserFeatureStore.project_id == project_id
        ).all()
        from app.services.scoring.feature_store_service import TRAIT_PREFIX
        for lf in loser_features:
            wf = self.db.query(UserFeatureStore).filter(
                UserFeatureStore.user_id == winner_id,
                UserFeatureStore.event_name == lf.event_name,
                UserFeatureStore.project_id == project_id
            ).first()
            if wf:
                # Trait rows (client:*) are point-in-time values, not counts —
                # keep the most recently observed payload instead of summing.
                if lf.event_name.startswith(TRAIT_PREFIX):
                    if lf.last_seen_at > wf.last_seen_at:
                        wf.last_value = lf.last_value
                        wf.last_seen_at = lf.last_seen_at
                    if lf.first_seen_at < wf.first_seen_at:
                        wf.first_seen_at = lf.first_seen_at
                    self.db.delete(lf)
                    continue
                wf.count_total += lf.count_total
                wf.count_1d += lf.count_1d
                wf.count_7d += lf.count_7d
                wf.count_30d += lf.count_30d
                wf.sum_value += lf.sum_value
                if lf.first_seen_at < wf.first_seen_at:
                    wf.first_seen_at = lf.first_seen_at
                if lf.last_seen_at > wf.last_seen_at:
                    wf.last_seen_at = lf.last_seen_at
                self.db.delete(lf)
            else:
                lf.user_id = winner_id

    def _transfer_cooldowns(self, project_id: int, winner_id: int, loser_id: int):
        """Canonicalize cooldown rows while retaining the stricter window."""
        loser_rows = self.db.query(EventActionCooldown).filter(
            EventActionCooldown.project_id == project_id,
            EventActionCooldown.user_id == loser_id,
        ).all()
        for loser_row in loser_rows:
            winner_row = self.db.query(EventActionCooldown).filter(
                EventActionCooldown.project_id == project_id,
                EventActionCooldown.user_id == winner_id,
                EventActionCooldown.event_action_id == loser_row.event_action_id,
            ).first()
            if winner_row is None:
                loser_row.user_id = winner_id
                continue
            if loser_row.last_triggered_at > winner_row.last_triggered_at:
                winner_row.last_triggered_at = loser_row.last_triggered_at
            if loser_row.expires_at > winner_row.expires_at:
                winner_row.expires_at = loser_row.expires_at
            self.db.delete(loser_row)

    def _transfer_rate_windows(
        self,
        project_id: int,
        winner_external_id: str,
        loser_external_id: str,
    ) -> None:
        """Move rate counters and combine active windows conservatively."""
        if winner_external_id == loser_external_id:
            return

        cutoff = datetime.utcnow() - timedelta(minutes=5)
        loser_rows = self.db.query(ContactRateWindow).filter(
            ContactRateWindow.project_id == project_id,
            ContactRateWindow.contact_identifier == loser_external_id,
        ).all()
        for row in loser_rows:
            row.contact_identifier = winner_external_id

        # A contact may have at most one semantically active counter per
        # channel. Keep the latest start (the stricter expiry) and sum counts.
        active_rows = self.db.query(ContactRateWindow).filter(
            ContactRateWindow.project_id == project_id,
            ContactRateWindow.contact_identifier.in_([
                winner_external_id,
                loser_external_id,
            ]),
            ContactRateWindow.window_start > cutoff,
        ).all()
        by_channel: dict[str, list[ContactRateWindow]] = {}
        for row in active_rows:
            by_channel.setdefault(row.channel, []).append(row)
        for rows in by_channel.values():
            if len(rows) < 2:
                continue
            keeper = max(rows, key=lambda row: row.window_start)
            keeper.contact_identifier = winner_external_id
            keeper.message_count = sum(row.message_count for row in rows)
            for row in rows:
                if row is not keeper:
                    self.db.delete(row)
