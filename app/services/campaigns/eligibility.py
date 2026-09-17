"""Campaign audience snapshot and purpose-specific eligibility."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from app.models.campaigns import ContactEndpoint, ContactPermissionEvidence
from app.models.messaging import ContactVerificationState, MessagingUser
class CampaignPolicyError(ValueError):
    pass


@dataclass
class ContactEligibility:
    user: MessagingUser
    endpoint: ContactEndpoint | None
    endpoint_hash: str | None
    verification_status: str | None
    verification_expires_at: datetime | None
    permission_status: str | None
    permission_evidence_id: int | None
    eligible: bool
    reason: str | None


def validate_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    policy = dict(policy or {})
    permission_mode = str(policy.get("permission_mode") or "explicit_consent")
    if permission_mode not in {
        "explicit_consent", "documented_relationship", "subscribed_no_optout",
    }:
        raise CampaignPolicyError(
            "permission_mode must be explicit_consent, documented_relationship "
            "or subscribed_no_optout"
        )
    purpose = str(policy.get("purpose") or "promotional")
    if purpose != "promotional":
        raise CampaignPolicyError(
            "Campaigns always use the promotional lane; transactional relabeling is not allowed"
        )
    if policy.get("permission_type") not in {None, "marketing"}:
        raise CampaignPolicyError("Campaign permission_type is fixed to email marketing")
    policy["permission_type"] = "marketing"
    if permission_mode == "subscribed_no_optout":
        acknowledgement = policy.get("risk_acknowledgement")
        if not isinstance(acknowledgement, dict) or acknowledgement.get("accepted") is not True:
            raise CampaignPolicyError(
                "subscribed_no_optout requires risk_acknowledgement.accepted=true"
            )
        if not str(acknowledgement.get("reason") or "").strip():
            raise CampaignPolicyError(
                "subscribed_no_optout requires a documented risk acknowledgement reason"
            )
    verification_mode = str(policy.get("verification_mode") or "require_valid")
    if verification_mode != "require_valid":
        raise CampaignPolicyError(
            "Campaign recipients require a fresh, valid verification result"
        )
    policy["permission_mode"] = permission_mode
    policy["purpose"] = purpose
    policy["verification_mode"] = verification_mode
    return policy


class CampaignEligibilityService:
    def __init__(self, db: Session):
        self.db = db

    def evaluate(
        self,
        *,
        project_id: int,
        candidate_query: Query,
        selection_config: dict[str, Any] | None,
        policy_config: dict[str, Any] | None,
        channel: str = "email",
        now: datetime | None = None,
    ) -> list[ContactEligibility]:
        now = now or datetime.utcnow()
        policy = validate_policy(policy_config)
        adapter = __import__(
            "app.services.campaigns.channel_registry",
            fromlist=["CampaignChannelRegistry"],
        ).CampaignChannelRegistry.get(channel)
        if not adapter:
            raise CampaignPolicyError(f"Campaign channel is not registered: {channel}")
        if channel != "email" or tuple(adapter.endpoint_types) != ("email",):
            raise CampaignPolicyError(
                f"Purpose-specific campaign eligibility is not implemented for channel: {channel}"
            )
        selection = dict(selection_config or {})
        candidate_ids = candidate_query.subquery()
        users = self.db.query(MessagingUser).join(
            candidate_ids,
            candidate_ids.c.id == MessagingUser.id,
        ).order_by(MessagingUser.id).all()
        if not users:
            return []

        endpoints_by_user: dict[int, list[tuple[ContactEndpoint, ContactVerificationState | None]]] = defaultdict(list)
        endpoint_rows = self.db.query(ContactEndpoint, ContactVerificationState).join(
            candidate_ids,
            candidate_ids.c.id == ContactEndpoint.user_id,
        ).outerjoin(
            ContactVerificationState,
            (ContactVerificationState.project_id == ContactEndpoint.project_id)
            & (ContactVerificationState.user_id == ContactEndpoint.user_id)
            & (ContactVerificationState.verification_type == "email")
            & (ContactVerificationState.identifier_hash == ContactEndpoint.value_hash)
            & (ContactVerificationState.endpoint_id == ContactEndpoint.id),
        ).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.endpoint_type == "email",
            ContactEndpoint.status == "active",
        ).order_by(ContactEndpoint.user_id, ContactEndpoint.id).all()
        for endpoint, verification in endpoint_rows:
            endpoints_by_user[endpoint.user_id].append((endpoint, verification))

        selected: dict[int, tuple[ContactEndpoint | None, ContactVerificationState | None, str | None]] = {}
        rule = str(selection.get("email_selection_rule") or "primary_then_verified")
        for user in users:
            selected[user.id] = self._select_endpoint(endpoints_by_user.get(user.id, []), rule, now)

        evidence_by_user: dict[int, list[ContactPermissionEvidence]] = defaultdict(list)
        evidence_rows = self.db.query(ContactPermissionEvidence).join(
            candidate_ids,
            candidate_ids.c.id == ContactPermissionEvidence.user_id,
        ).filter(
            ContactPermissionEvidence.project_id == project_id,
            ContactPermissionEvidence.channel == channel,
            ContactPermissionEvidence.permission_type == "marketing",
        ).order_by(
            ContactPermissionEvidence.user_id,
            ContactPermissionEvidence.captured_at.desc(),
            ContactPermissionEvidence.id.desc(),
        ).all()
        for row in evidence_rows:
            evidence_by_user[row.user_id].append(row)

        result: list[ContactEligibility] = []
        for user in users:
            endpoint, verification, endpoint_error = selected[user.id]
            permission = self._permission_for(
                evidence_by_user.get(user.id, []),
                endpoint.id if endpoint else None,
                now,
            )
            reason = self._suppression_reason(
                user=user,
                endpoint=endpoint,
                endpoint_error=endpoint_error,
                verification=verification,
                permission=permission,
                policy=policy,
                now=now,
            )
            result.append(ContactEligibility(
                user=user,
                endpoint=endpoint,
                endpoint_hash=endpoint.value_hash if endpoint else None,
                verification_status=verification.canonical_status if verification else None,
                verification_expires_at=verification.expires_at if verification else None,
                permission_status=permission.status if permission else None,
                permission_evidence_id=permission.id if permission else None,
                eligible=reason is None,
                reason=reason,
            ))
        return result

    @staticmethod
    def _select_endpoint(
        rows: list[tuple[ContactEndpoint, ContactVerificationState | None]],
        rule: str,
        now: datetime,
    ) -> tuple[ContactEndpoint | None, ContactVerificationState | None, str | None]:
        if not rows:
            return None, None, "missing_email_endpoint"
        if rule == "primary":
            primaries = [row for row in rows if row[0].is_primary]
            if len(primaries) == 1:
                return primaries[0][0], primaries[0][1], None
            if not primaries and len(rows) == 1:
                return rows[0][0], rows[0][1], None
            return None, None, "ambiguous_email_endpoint"
        if rule not in {"primary_then_verified", "most_recently_verified", "most_recently_seen", "oldest"}:
            return None, None, "unsupported_email_selection_rule"

        def key(item):
            endpoint, verification = item
            valid_fresh = bool(
                verification
                and verification.canonical_status == "valid"
                and verification.expires_at > now
            )
            checked = verification.checked_at if verification else datetime.min
            if rule == "most_recently_verified":
                return (valid_fresh, checked, endpoint.is_primary, endpoint.last_seen_at, -endpoint.id)
            if rule == "most_recently_seen":
                return (endpoint.last_seen_at, endpoint.is_primary, valid_fresh, checked, -endpoint.id)
            if rule == "oldest":
                # max() with negative timestamp/id deterministically selects oldest.
                return (-endpoint.first_seen_at.timestamp(), -endpoint.id)
            return (endpoint.is_primary, valid_fresh, checked, endpoint.last_seen_at, -endpoint.id)

        endpoint, verification = max(rows, key=key)
        return endpoint, verification, None

    @staticmethod
    def _permission_for(
        rows: list[ContactPermissionEvidence],
        endpoint_id: int | None,
        now: datetime,
    ) -> ContactPermissionEvidence | None:
        for row in rows:
            if row.endpoint_id not in {None, endpoint_id}:
                continue
            if row.expires_at is not None and row.expires_at <= now:
                # Expiry is an effective unknown, but a newer expired grant
                # must not reveal an older grant.
                return row
            return row
        return None

    @staticmethod
    def _suppression_reason(
        *,
        user: MessagingUser,
        endpoint: ContactEndpoint | None,
        endpoint_error: str | None,
        verification: ContactVerificationState | None,
        permission: ContactPermissionEvidence | None,
        policy: dict[str, Any],
        now: datetime,
    ) -> str | None:
        if user.status != "active" or user.is_sandbox:
            return "inactive_contact"
        if user.is_blocked:
            return "blocked_contact"
        if user.global_opt_out or user.is_subscribed is False:
            return "opted_out"
        if "email" in (user.opted_out_channels or []):
            return "email_opted_out"
        if endpoint_error:
            return endpoint_error
        if not endpoint:
            return "missing_email_endpoint"

        permission_mode = policy["permission_mode"]
        evidence_granted = bool(
            permission
            and permission.status == "granted"
            and (permission.expires_at is None or permission.expires_at > now)
        )
        if permission_mode == "explicit_consent" and not evidence_granted:
            return "missing_permission_evidence"
        if permission_mode == "documented_relationship":
            metadata = permission.evidence_metadata if permission else None
            legal_basis = metadata.get("legal_basis") if isinstance(metadata, dict) else None
            if not (
                evidence_granted
                and str(permission.source or "").strip()
                and str(permission.evidence_ref or "").strip()
                and str(legal_basis or "").strip()
            ):
                return "missing_documented_relationship_evidence"
        # subscribed_no_optout is intentionally the only mode that relies on
        # subscription + opt-out state. validate_policy() requires an explicit
        # persisted risk acknowledgement; this is never an implicit fallback.

        verification_mode = policy["verification_mode"]
        if not verification:
            return "email_unverified"
        if verification.last_attempt_status != "succeeded":
            return "email_verification_last_attempt_failed"
        if verification.endpoint_id != endpoint.id:
            return "verification_endpoint_mismatch"
        if verification.identifier_hash != endpoint.value_hash:
            return "verification_identifier_changed"
        if verification.expires_at <= now:
            return "email_verification_expired"
        if verification.canonical_status != "valid":
            return f"email_verification_{verification.canonical_status or 'unknown'}"
        return None
