"""
DSAR Processor Service
Handles Data Subject Access Requests (export, delete, rectify, withdraw consent).
"""
from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
import json

from app.models.messaging import (
    MessagingDSARRequest, MessagingUser, MessagingEvent,
    MessagingAnonymousProfile, MessagingAttributionTouch, MessagingLog,
    DSARRequestType, DSARRequestStatus,
    ContactIdentity, ContactMergeLog, ContactAccountAssociation,
    Account, MergeSuggestion, ContactVerificationItem,
    ContactVerificationJob, ContactVerificationOperation,
    ContactVerificationState,
)
from app.models import SendLog
from app.models.campaigns import (
    Campaign,
    CampaignRecipient,
    CampaignRun,
    ContactEndpoint,
    ContactGroup,
    ContactGroupMembership,
    ContactPermissionEvidence,
    ContactVerificationRequest,
)
from app.services.messaging.consent_manager import consent_manager
from app.services.messaging.contact_profile_service import ContactProfileService
from app.services.messaging.pii_hasher import pii_hasher
from app.services.contact_verification.service import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_ITEM_STATUSES,
    UPLOAD_RECONCILIATION_STATUSES,
    ContactVerificationService,
)


class DSARProcessor:
    """
    Processes DSAR requests for GDPR/LGPD compliance.

    Supported request types:
    - export: Export all user data as JSON
    - delete: Delete all user data (right to be forgotten)
    - rectify: Update user data
    - withdraw_consent: Withdraw all consent
    """

    async def create_dsar_request(
        self,
        db: Session,
        project_id: int,
        request_type: DSARRequestType,
        user_id: Optional[int] = None,
        external_id: Optional[str] = None,
        email: Optional[str] = None,
        ip: Optional[str] = None
    ) -> MessagingDSARRequest:
        """
        Create a new DSAR request.

        At least one identifier must be provided:
        - user_id (internal)
        - external_id (customer's user ID)
        - email (will be hashed for lookup)
        """
        email_hash = None
        if email:
            email_hash, _ = pii_hasher.hash_user_pii(db, project_id, email)

        # An explicitly supplied internal id must belong to this project. This
        # prevents a cross-project DSAR record from pointing at another
        # tenant's contact even when the caller already knows its numeric id.
        if user_id:
            user = db.query(MessagingUser).filter(
                MessagingUser.id == user_id,
                MessagingUser.project_id == project_id,
            ).first()
            if not user:
                raise ValueError("Contact not found in project")
        else:
            user = self._find_user(db, project_id, external_id, email_hash)
            if user:
                user_id = user.id

        request = MessagingDSARRequest(
            project_id=project_id,
            request_type=request_type,
            user_id=user_id,
            external_id=external_id,
            email_hash=email_hash,
            requested_by_ip=ip,
            status=DSARRequestStatus.pending
        )
        db.add(request)
        db.commit()
        db.refresh(request)

        return request

    def _find_user(
        self,
        db: Session,
        project_id: int,
        external_id: Optional[str],
        email_hash: Optional[str]
    ) -> Optional[MessagingUser]:
        """Find user by external_id or email_hash."""
        if external_id:
            user = db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.external_id == external_id
            ).first()
            if user:
                return user

        if email_hash:
            user = db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.email_hash == email_hash
            ).first()
            if user:
                return user

        return None

    async def process_request(
        self,
        db: Session,
        project_id: int,
        request_id: int,
        processed_by_user_id: Optional[int] = None
    ) -> MessagingDSARRequest:
        """
        Process a pending DSAR request.
        Routes to appropriate handler based on request type.
        """
        request = db.query(MessagingDSARRequest).filter(
            MessagingDSARRequest.id == request_id,
            MessagingDSARRequest.project_id == project_id,
        ).first()

        if not request:
            raise ValueError(f"DSAR request {request_id} not found")

        retrying_remote_delete = (
            request.request_type == DSARRequestType.delete
            and request.status == DSARRequestStatus.processing
        )
        if request.status != DSARRequestStatus.pending and not retrying_remote_delete:
            raise ValueError(f"DSAR request {request_id} is not pending")

        # Mark as processing
        request.status = DSARRequestStatus.processing
        request.processed_by_user_id = processed_by_user_id
        db.commit()

        try:
            completion_ready = True
            if request.request_type == DSARRequestType.export:
                await self._process_export(db, request)
            elif request.request_type == DSARRequestType.delete:
                completion_ready = await self._process_delete(db, request)
            elif request.request_type == DSARRequestType.rectify:
                await self._process_rectify(db, request)
            elif request.request_type == DSARRequestType.withdraw_consent:
                await self._process_withdraw_consent(db, request)
            else:
                raise ValueError(f"Unsupported DSAR request type: {request.request_type}")

            if completion_ready:
                request.status = DSARRequestStatus.completed
                request.completed_at = datetime.utcnow()
                request.error_message = None
            else:
                # A delete remains retryable while a provider still holds a
                # PII-bearing bulk file. The durable verification rows and the
                # contact itself are intentionally retained until confirmation.
                request.status = DSARRequestStatus.processing
                request.completed_at = None

        except Exception as e:
            db.rollback()
            request = db.query(MessagingDSARRequest).filter(
                MessagingDSARRequest.id == request_id,
                MessagingDSARRequest.project_id == project_id,
            ).one()
            request.status = DSARRequestStatus.failed
            request.error_message = str(e)

        db.commit()
        db.refresh(request)

        return request

    async def _process_export(
        self,
        db: Session,
        request: MessagingDSARRequest
    ) -> None:
        """
        Export all user data as JSON.
        Stores result in result_data field.
        """
        user = self._get_user_for_request(db, request)
        if not user:
            request.error_message = "User not found"
            return
        subject_user_ids = self._subject_user_ids(db, request.project_id, user.id)

        # Collect all user data
        export_data = {
            "export_date": datetime.utcnow().isoformat(),
            "request_id": request.id,
            "user": {
                "id": user.id,
                "external_id": user.external_id,
                "email": user.email,
                "phone": user.phone,
                "name": user.name,
                "properties": user.properties,
                "is_subscribed": user.is_subscribed,
                "first_seen_at": user.first_seen_at.isoformat() if user.first_seen_at else None,
                "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
                "consent": {
                    "marketing": user.consent_marketing,
                    "analytics": user.consent_analytics,
                    "given_at": user.consent_given_at.isoformat() if user.consent_given_at else None,
                    "version": user.consent_version
                }
            },
            "events": [],
            "messages": []
        }

        # Get events
        events = db.query(MessagingEvent).filter(
            MessagingEvent.project_id == request.project_id,
            MessagingEvent.user_id.in_(subject_user_ids),
        ).order_by(MessagingEvent.created_at.desc()).limit(1000).all()

        for event in events:
            export_data["events"].append({
                "id": event.id,
                "event_name": event.event_name,
                "properties": event.properties,
                "source": event.source,
                "created_at": event.created_at.isoformat() if event.created_at else None
            })

        touches = db.query(MessagingAttributionTouch).filter(
            MessagingAttributionTouch.project_id == request.project_id,
            MessagingAttributionTouch.user_id.in_(subject_user_ids),
        ).order_by(MessagingAttributionTouch.captured_at.desc()).all()
        export_data["attribution_touches"] = [
            {
                "provider": touch.provider,
                "identifier_type": touch.identifier_type,
                "identifier_value": touch.identifier_value,
                "anonymous_id": touch.anonymous_id,
                "capture_source": touch.capture_source,
                "captured_at": touch.captured_at.isoformat() if touch.captured_at else None,
                "expires_at": touch.expires_at.isoformat() if touch.expires_at else None,
                "last_seen_at": touch.last_seen_at.isoformat() if touch.last_seen_at else None,
            }
            for touch in touches
        ]
        # Get message logs
        logs = db.query(MessagingLog).filter(
            MessagingLog.project_id == request.project_id,
            MessagingLog.user_id.in_(subject_user_ids),
        ).order_by(MessagingLog.created_at.desc()).limit(500).all()

        for log in logs:
            export_data["messages"].append({
                "id": log.id,
                "template_slug": log.template_slug,
                "channel_type": log.channel_type,
                "recipient": log.recipient,
                "status": log.status.value if log.status else None,
                "sent_at": log.sent_at.isoformat() if log.sent_at else None,
                "created_at": log.created_at.isoformat() if log.created_at else None
            })

        # Get anonymous profiles merged to this user
        profiles = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == request.project_id,
            MessagingAnonymousProfile.merged_to_user_id.in_(subject_user_ids),
        ).all()

        if profiles:
            export_data["anonymous_profiles"] = []
            for profile in profiles:
                export_data["anonymous_profiles"].append({
                    "anonymous_id": profile.anonymous_id,
                    "first_seen_at": profile.first_seen_at.isoformat() if profile.first_seen_at else None,
                    "merged_at": profile.merged_at.isoformat() if profile.merged_at else None,
                    "properties": profile.properties
                })

        # Get contact identities
        identities = db.query(ContactIdentity).filter(
            ContactIdentity.project_id == request.project_id,
            ContactIdentity.user_id.in_(subject_user_ids),
        ).all()

        if identities:
            export_data["identities"] = []
            for identity in identities:
                export_data["identities"].append({
                    "id": identity.id,
                    "identity_type": identity.identity_type,
                    "identity_value": identity.identity_value,
                    "channel_instance_id": identity.channel_instance_id,
                    "verified": identity.verified,
                    "source": identity.source,
                    "created_at": identity.created_at.isoformat() if identity.created_at else None
                })

        # Get account associations (with account details)
        associations = db.query(ContactAccountAssociation, Account).join(
            Account, ContactAccountAssociation.account_id == Account.id
        ).filter(
            ContactAccountAssociation.project_id == request.project_id,
            ContactAccountAssociation.user_id.in_(subject_user_ids),
            Account.project_id == request.project_id,
        ).all()

        if associations:
            export_data["accounts"] = []
            for assoc, account in associations:
                export_data["accounts"].append({
                    "association_id": assoc.id,
                    "role": assoc.role,
                    "account": {
                        "id": account.id,
                        "external_id": account.external_id,
                        "name": account.name,
                        "properties": account.properties
                    },
                    "created_at": assoc.created_at.isoformat() if assoc.created_at else None
                })

        # Get merge history
        from sqlalchemy import or_
        merge_logs = db.query(ContactMergeLog).filter(
            ContactMergeLog.project_id == request.project_id,
            or_(
                ContactMergeLog.winner_id.in_(subject_user_ids),
                ContactMergeLog.loser_id.in_(subject_user_ids),
            )
        ).order_by(ContactMergeLog.created_at.desc()).all()

        if merge_logs:
            export_data["merge_history"] = []
            for log in merge_logs:
                export_data["merge_history"].append({
                    "id": log.id,
                    "winner_id": log.winner_id,
                    "loser_id": log.loser_id,
                    "triggered_by": log.triggered_by,
                    "identities_transferred": log.identities_transferred,
                    "properties_resolved": log.properties_resolved,
                    "created_at": log.created_at.isoformat() if log.created_at else None
                })

        self._export_campaign_contact_data(
            db,
            request,
            user,
            export_data,
            subject_user_ids=subject_user_ids,
        )

        request.result_data = export_data

    def _export_campaign_contact_data(
        self,
        db: Session,
        request: MessagingDSARRequest,
        user: MessagingUser,
        export_data: Dict[str, Any],
        subject_user_ids: Optional[set[int]] = None,
    ) -> None:
        """Append provider-neutral contact and campaign data to a DSAR export."""
        project_id = request.project_id
        subject_user_ids = subject_user_ids or self._subject_user_ids(
            db, project_id, user.id,
        )
        endpoints = db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id.in_(subject_user_ids),
        ).order_by(ContactEndpoint.created_at).all()
        export_data["contact_endpoints"] = [
            {
                "id": endpoint.id,
                "endpoint_type": endpoint.endpoint_type,
                "value": endpoint.value,
                "normalized_value": endpoint.normalized_value,
                "value_hash": endpoint.value_hash,
                "is_primary": endpoint.is_primary,
                "status": endpoint.status,
                "source": endpoint.source,
                "metadata": endpoint.endpoint_metadata,
                "first_seen_at": self._iso(endpoint.first_seen_at),
                "last_seen_at": self._iso(endpoint.last_seen_at),
                "created_at": self._iso(endpoint.created_at),
            }
            for endpoint in endpoints
        ]

        evidence_rows = db.query(ContactPermissionEvidence).filter(
            ContactPermissionEvidence.project_id == project_id,
            ContactPermissionEvidence.user_id.in_(subject_user_ids),
        ).order_by(ContactPermissionEvidence.captured_at).all()
        export_data["permission_evidence"] = [
            {
                "id": evidence.id,
                "endpoint_id": evidence.endpoint_id,
                "channel": evidence.channel,
                "permission_type": evidence.permission_type,
                "status": evidence.status,
                "source": evidence.source,
                "policy_version": evidence.policy_version,
                "evidence_ref": evidence.evidence_ref,
                "captured_at": self._iso(evidence.captured_at),
                "expires_at": self._iso(evidence.expires_at),
                "metadata": evidence.evidence_metadata,
            }
            for evidence in evidence_rows
        ]

        membership_rows = db.query(ContactGroupMembership, ContactGroup).join(
            ContactGroup,
            ContactGroup.id == ContactGroupMembership.group_id,
        ).filter(
            ContactGroupMembership.project_id == project_id,
            ContactGroupMembership.user_id.in_(subject_user_ids),
            ContactGroup.project_id == project_id,
        ).order_by(ContactGroupMembership.created_at).all()
        export_data["contact_group_memberships"] = [
            {
                "id": membership.id,
                "group_id": group.id,
                "group_name": group.name,
                "group_type": group.group_type,
                "state": membership.state,
                "source": membership.source,
                "reason": membership.reason,
                "evaluated_at": self._iso(membership.evaluated_at),
                "created_at": self._iso(membership.created_at),
            }
            for membership, group in membership_rows
        ]

        states = db.query(ContactVerificationState).filter(
            ContactVerificationState.project_id == project_id,
            ContactVerificationState.user_id.in_(subject_user_ids),
        ).order_by(ContactVerificationState.checked_at).all()
        export_data["verification_states"] = [
            {
                "id": state.id,
                "endpoint_id": state.endpoint_id,
                "verification_type": state.verification_type,
                "identifier_hash": state.identifier_hash,
                "provider": state.provider,
                "provider_key_source": state.provider_key_source,
                "provider_version": state.provider_version,
                "canonical_status": state.canonical_status,
                "provider_status": state.provider_status,
                "provider_metadata": state.provider_metadata,
                "checked_at": self._iso(state.checked_at),
                "expires_at": self._iso(state.expires_at),
                "last_attempt_status": state.last_attempt_status,
                "last_attempt_at": self._iso(state.last_attempt_at),
                "last_error_code": state.last_error_code,
            }
            for state in states
        ]

        verification_requests = db.query(ContactVerificationRequest).filter(
            ContactVerificationRequest.project_id == project_id,
            ContactVerificationRequest.user_id.in_(subject_user_ids),
        ).order_by(ContactVerificationRequest.requested_at).all()
        export_data["verification_requests"] = [
            {
                "id": verification_request.id,
                "endpoint_id": verification_request.endpoint_id,
                "job_id": verification_request.job_id,
                "verification_type": verification_request.verification_type,
                "provider": verification_request.provider,
                "trigger_type": verification_request.trigger_type,
                "idempotency_key": verification_request.idempotency_key,
                "status": verification_request.status,
                "attempt_count": verification_request.attempt_count,
                "provider_status": verification_request.provider_status,
                "canonical_status": verification_request.canonical_status,
                "error_code": verification_request.error_code,
                "error_message": verification_request.error_message,
                "request_metadata": verification_request.request_metadata,
                "requested_at": self._iso(verification_request.requested_at),
                "started_at": self._iso(verification_request.started_at),
                "finished_at": self._iso(verification_request.finished_at),
            }
            for verification_request in verification_requests
        ]

        verification_attempts = db.query(ContactVerificationItem).filter(
            ContactVerificationItem.project_id == project_id,
            ContactVerificationItem.user_id.in_(subject_user_ids),
        ).order_by(ContactVerificationItem.created_at).all()
        export_data["verification_attempts"] = [
            {
                "id": attempt.id,
                "job_id": attempt.job_id,
                "endpoint_id": attempt.endpoint_id,
                "verification_type": attempt.verification_type,
                "identifier_hash": attempt.identifier_hash,
                "status": attempt.status,
                "attempt_count": attempt.attempt_count,
                "canonical_status": attempt.canonical_status,
                "provider_status": attempt.provider_status,
                "provider_metadata": attempt.provider_metadata,
                "error_code": attempt.error_code,
                "error_message": attempt.error_message,
                "started_at": self._iso(attempt.started_at),
                "finished_at": self._iso(attempt.finished_at),
                "created_at": self._iso(attempt.created_at),
            }
            for attempt in verification_attempts
        ]

        campaign_rows = db.query(CampaignRecipient, CampaignRun, Campaign).join(
            CampaignRun,
            CampaignRun.id == CampaignRecipient.run_id,
        ).join(
            Campaign,
            Campaign.id == CampaignRun.campaign_id,
        ).filter(
            CampaignRecipient.project_id == project_id,
            CampaignRecipient.user_id.in_(subject_user_ids),
            CampaignRun.project_id == project_id,
            Campaign.project_id == project_id,
        ).order_by(CampaignRecipient.created_at).all()
        export_data["campaign_deliveries"] = [
            {
                "recipient_id": recipient.id,
                "campaign_id": campaign.id,
                "campaign_name": campaign.name,
                "run_id": run.id,
                "run_key": run.run_key,
                "action_id": recipient.action_id,
                "variant_id": recipient.variant_id,
                "endpoint_id": recipient.endpoint_id,
                "endpoint_hash": recipient.endpoint_hash,
                "channel": recipient.channel,
                "status": recipient.status,
                "suppression_reason": recipient.suppression_reason,
                "provider": recipient.provider,
                "idempotency_key": recipient.idempotency_key,
                "send_log_id": recipient.send_log_id,
                "scheduled_at": self._iso(recipient.scheduled_at),
                "sent_at": self._iso(recipient.sent_at),
                "completed_at": self._iso(recipient.completed_at),
                "created_at": self._iso(recipient.created_at),
            }
            for recipient, run, campaign in campaign_rows
        ]

        send_logs = db.query(SendLog).filter(
            SendLog.project_id == project_id,
            SendLog.user_id.in_(subject_user_ids),
        ).order_by(SendLog.queued_at).all()
        export_data["send_layer_messages"] = [
            {
                "id": log.id,
                "channel": log.channel,
                "recipient": log.recipient,
                "content_type": log.content_type,
                "content_summary": log.content_summary,
                "content_payload": log.content_payload,
                "source_type": log.source_type,
                "source_id": log.source_id,
                "status": log.status,
                "provider_message_id": log.provider_message_id,
                "provider_response": log.provider_response,
                "error_message": log.error_message,
                "render_context": log.render_context,
                "scheduled_at": self._iso(log.scheduled_at),
                "queued_at": self._iso(log.queued_at),
                "sent_at": self._iso(log.sent_at),
                "delivered_at": self._iso(log.delivered_at),
                "read_at": self._iso(log.read_at),
                "failed_at": self._iso(log.failed_at),
            }
            for log in send_logs
        ]

    def _verification_job_ids_for_subject(
        self,
        db: Session,
        project_id: int,
        user_ids: set[int],
    ) -> set[int]:
        endpoint_ids = {
            int(row[0])
            for row in db.query(ContactEndpoint.id).filter(
                ContactEndpoint.project_id == project_id,
                ContactEndpoint.user_id.in_(user_ids),
            ).all()
        }
        job_ids = {
            int(row[0])
            for row in db.query(ContactVerificationItem.job_id).filter(
                ContactVerificationItem.project_id == project_id,
                ContactVerificationItem.user_id.in_(user_ids),
            ).distinct().all()
        }
        job_ids.update(
            int(row[0])
            for row in db.query(ContactVerificationRequest.job_id).filter(
                ContactVerificationRequest.project_id == project_id,
                ContactVerificationRequest.user_id.in_(user_ids),
                ContactVerificationRequest.job_id.isnot(None),
            ).distinct().all()
        )
        if endpoint_ids:
            job_ids.update(
                int(row[0])
                for row in db.query(ContactVerificationJob.id).filter(
                    ContactVerificationJob.project_id == project_id,
                    ContactVerificationJob.endpoint_id.in_(endpoint_ids),
                ).all()
            )
            job_ids.update(
                int(row[0])
                for row in db.query(ContactVerificationOperation.job_id).filter(
                    ContactVerificationOperation.project_id == project_id,
                    ContactVerificationOperation.endpoint_id.in_(endpoint_ids),
                ).distinct().all()
            )
        return job_ids

    @staticmethod
    def _verification_cleanup_complete(
        operations: list[ContactVerificationOperation],
    ) -> bool:
        for operation in operations:
            summary = operation.response_summary or {}
            if operation.status in UPLOAD_RECONCILIATION_STATUSES:
                return False
            if operation.provider_operation_id and summary.get("remote_file_deleted") is not True:
                return False
            if (
                operation.operation_type == "bulk_file"
                and operation.status in {"provider_processing", "provider_deleting"}
                and not operation.provider_operation_id
            ):
                return False
        return True

    async def _coordinate_verification_cleanup(
        self,
        db: Session,
        project_id: int,
        user_ids: set[int],
    ) -> tuple[bool, set[int], list[int]]:
        """Cancel subject jobs and confirm deletion of every remote bulk file."""
        job_ids = self._verification_job_ids_for_subject(db, project_id, user_ids)
        if not job_ids:
            return True, set(), []

        service = ContactVerificationService(db)
        pending_job_ids: list[int] = []
        jobs = db.query(ContactVerificationJob).filter(
            ContactVerificationJob.project_id == project_id,
            ContactVerificationJob.id.in_(job_ids),
        ).order_by(ContactVerificationJob.id).all()
        now = datetime.utcnow()
        for job in jobs:
            operations = db.query(ContactVerificationOperation).filter(
                ContactVerificationOperation.project_id == project_id,
                ContactVerificationOperation.job_id == job.id,
                ContactVerificationOperation.operation_type == "bulk_file",
            ).order_by(ContactVerificationOperation.id).all()
            cleanup_needed = not self._verification_cleanup_complete(operations)
            active = job.status in (ACTIVE_JOB_STATUSES | {"reconciliation_required"})
            if not active and not cleanup_needed:
                continue

            job.cancel_requested = True
            job.status = "cancel_requested"
            job.finished_at = None
            db.query(ContactVerificationItem).filter(
                ContactVerificationItem.project_id == project_id,
                ContactVerificationItem.job_id == job.id,
                ~ContactVerificationItem.status.in_(TERMINAL_ITEM_STATUSES),
            ).update({
                ContactVerificationItem.status: "cancelled",
                ContactVerificationItem.error_code: "dsar_delete",
                ContactVerificationItem.error_message: "Cancelled for data-subject deletion",
                ContactVerificationItem.finished_at: now,
            }, synchronize_session=False)

            # Older terminal rows may predate durable cleanup tracking. Adopt
            # any known provider file and delete it idempotently (404/absence
            # is accepted by the provider adapter as confirmation).
            unreconcilable = False
            for operation in operations:
                summary = operation.response_summary or {}
                if (
                    operation.provider_operation_id
                    and summary.get("remote_file_deleted") is not True
                    and operation.status not in (
                        {"provider_processing", "provider_deleting"}
                        | UPLOAD_RECONCILIATION_STATUSES
                    )
                ):
                    operation.status = "provider_deleting"
                    operation.finished_at = None
                if (
                    operation.status in {"provider_processing", "provider_deleting"}
                    and not operation.provider_operation_id
                ):
                    unreconcilable = True

            cleanup_complete = not cleanup_needed
            if cleanup_needed and not unreconcilable:
                try:
                    cleanup_complete = await service._cancel_provider_operations(job)
                except Exception as exc:
                    # Provider configuration/transport failure must not turn a
                    # privacy delete into a completed local-only operation.
                    cleanup_complete = False
                    job.error_message = f"Remote verification cleanup pending: {exc}"
            if cleanup_complete:
                db.flush()
                operations = db.query(ContactVerificationOperation).filter(
                    ContactVerificationOperation.project_id == project_id,
                    ContactVerificationOperation.job_id == job.id,
                    ContactVerificationOperation.operation_type == "bulk_file",
                ).order_by(ContactVerificationOperation.id).all()
                cleanup_complete = self._verification_cleanup_complete(operations)

            if cleanup_complete:
                job.status = "cancelled"
                job.finished_at = now
                job.error_message = None
                service.access.release(job.billing_reservation_id)
            else:
                job.status = "cancel_requested"
                job.finished_at = None
                pending_job_ids.append(job.id)

        db.flush()
        return not pending_job_ids, job_ids, pending_job_ids

    def _erase_campaign_contact_data(
        self,
        db: Session,
        project_id: int,
        user_ids: set[int],
        verification_job_ids: Optional[set[int]] = None,
    ) -> None:
        """Erase contact-owned data and pseudonymize durable delivery audit."""
        recipients = db.query(CampaignRecipient).filter(
            CampaignRecipient.project_id == project_id,
            CampaignRecipient.user_id.in_(user_ids),
        ).all()
        for recipient in recipients:
            recipient.user_id = None
            recipient.endpoint_id = None
            recipient.endpoint_hash = None
            # The original key embeds the contact id. Row identity keeps this
            # replacement unique without retaining a data-subject reference.
            recipient.idempotency_key = f"erased:{recipient.id}"
            recipient.last_error = None

        # Campaign SendLogs are compliance/operational audit and are retained,
        # but every field capable of identifying or reconstructing the contact
        # or message is removed. The status/timestamps/source relationship stay.
        db.query(SendLog).filter(
            SendLog.project_id == project_id,
            SendLog.user_id.in_(user_ids),
        ).update({
            "user_id": None,
            "recipient": "[DELETED]",
            "content_summary": None,
            "content_payload": None,
            "decision_trace": None,
            "provider_message_id": None,
            "provider_response": None,
            "error_message": None,
            "render_context": None,
            "tracking_token": None,
        }, synchronize_session=False)

        # Contact-owned facts have no useful non-identifying form in the
        # current schema, so remove them explicitly instead of relying only on
        # database cascades (which are not enabled in every test database).
        db.query(ContactPermissionEvidence).filter(
            ContactPermissionEvidence.project_id == project_id,
            ContactPermissionEvidence.user_id.in_(user_ids),
        ).delete(synchronize_session=False)
        db.query(ContactGroupMembership).filter(
            ContactGroupMembership.project_id == project_id,
            ContactGroupMembership.user_id.in_(user_ids),
        ).delete(synchronize_session=False)
        db.query(ContactVerificationRequest).filter(
            ContactVerificationRequest.project_id == project_id,
            ContactVerificationRequest.user_id.in_(user_ids),
        ).delete(synchronize_session=False)
        db.query(ContactVerificationState).filter(
            ContactVerificationState.project_id == project_id,
            ContactVerificationState.user_id.in_(user_ids),
        ).delete(synchronize_session=False)
        db.query(ContactVerificationItem).filter(
            ContactVerificationItem.project_id == project_id,
            ContactVerificationItem.user_id.in_(user_ids),
        ).delete(synchronize_session=False)
        if verification_job_ids:
            db.query(ContactVerificationJob).filter(
                ContactVerificationJob.project_id == project_id,
                ContactVerificationJob.id.in_(verification_job_ids),
            ).update({
                ContactVerificationJob.endpoint_id: None,
                ContactVerificationJob.selection: {"erased_by_dsar": True},
            }, synchronize_session=False)
            db.query(ContactVerificationOperation).filter(
                ContactVerificationOperation.project_id == project_id,
                ContactVerificationOperation.job_id.in_(verification_job_ids),
            ).update({
                ContactVerificationOperation.endpoint_id: None,
            }, synchronize_session=False)
        db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id.in_(user_ids),
        ).delete(synchronize_session=False)

    @staticmethod
    def _iso(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    async def _process_delete(
        self,
        db: Session,
        request: MessagingDSARRequest
    ) -> bool:
        """
        Delete all user data (right to be forgotten).
        Anonymizes data that cannot be deleted.
        """
        user = self._get_user_for_request(db, request)
        if not user:
            request.error_message = "User not found"
            return True

        user_id = user.id
        subject_user_ids = self._subject_user_ids(db, request.project_id, user_id)

        cleanup_complete, verification_job_ids, pending_job_ids = (
            await self._coordinate_verification_cleanup(
                db,
                request.project_id,
                subject_user_ids,
            )
        )
        if not cleanup_complete:
            request.result_data = {
                "phase": "remote_verification_cleanup",
                "pending_verification_job_ids": pending_job_ids,
            }
            request.error_message = (
                "Remote verification cleanup is pending provider confirmation"
            )
            return False

        # Paid-click identifiers are personal data and are removed with the contact.
        db.query(MessagingAttributionTouch).filter(
            MessagingAttributionTouch.project_id == request.project_id,
            MessagingAttributionTouch.user_id.in_(subject_user_ids),
        ).delete(synchronize_session=False)
        # Delete events
        db.query(MessagingEvent).filter(
            MessagingEvent.project_id == request.project_id,
            MessagingEvent.user_id.in_(subject_user_ids),
        ).delete(synchronize_session=False)

        # Anonymize message logs (keep for compliance but remove PII)
        db.query(MessagingLog).filter(
            MessagingLog.project_id == request.project_id,
            MessagingLog.user_id.in_(subject_user_ids),
        ).update({
            "user_id": None,
            "recipient": "[DELETED]",
            "rendered_subject": None,
            "rendered_body": None
        }, synchronize_session=False)

        # Update anonymous profiles
        db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == request.project_id,
            MessagingAnonymousProfile.merged_to_user_id.in_(subject_user_ids),
        ).update({
            "merged_to_user_id": None,
            "merged_at": None,
            "properties": None
        }, synchronize_session=False)

        # Delete contact identities
        db.query(ContactIdentity).filter(
            ContactIdentity.project_id == request.project_id,
            ContactIdentity.user_id.in_(subject_user_ids),
        ).delete(synchronize_session=False)

        # Delete contact-account associations
        db.query(ContactAccountAssociation).filter(
            ContactAccountAssociation.project_id == request.project_id,
            ContactAccountAssociation.user_id.in_(subject_user_ids),
        ).delete(synchronize_session=False)

        self._erase_campaign_contact_data(
            db,
            request.project_id,
            subject_user_ids,
            verification_job_ids=verification_job_ids,
        )

        # Anonymize contact merge logs (clear snapshots)
        from sqlalchemy import or_
        db.query(ContactMergeLog).filter(
            ContactMergeLog.project_id == request.project_id,
            or_(
                ContactMergeLog.winner_id.in_(subject_user_ids),
                ContactMergeLog.loser_id.in_(subject_user_ids),
            )
        ).update({
            "snapshot_winner": {},
            "snapshot_loser": {},
            "identities_transferred": [],
            "properties_resolved": {},
        }, synchronize_session=False)

        # Delete merge suggestions referencing this contact
        db.query(MergeSuggestion).filter(
            MergeSuggestion.project_id == request.project_id,
            or_(
                MergeSuggestion.contact_a_id.in_(subject_user_ids),
                MergeSuggestion.contact_b_id.in_(subject_user_ids),
            )
        ).delete(synchronize_session=False)

        erased_at = datetime.utcnow()
        # Previous exports can themselves retain all of the data just erased.
        # Keep the DSAR audit row, but remove its identifiers and result payload.
        subject_identifiers = db.query(
            MessagingUser.external_id,
            MessagingUser.email_hash,
        ).filter(
            MessagingUser.project_id == request.project_id,
            MessagingUser.id.in_(subject_user_ids),
        ).all()
        external_ids = {row.external_id for row in subject_identifiers if row.external_id}
        email_hashes = {row.email_hash for row in subject_identifiers if row.email_hash}
        dsar_subject_filters = [MessagingDSARRequest.user_id.in_(subject_user_ids)]
        if external_ids:
            dsar_subject_filters.append(MessagingDSARRequest.external_id.in_(external_ids))
        if email_hashes:
            dsar_subject_filters.append(MessagingDSARRequest.email_hash.in_(email_hashes))
        db.query(MessagingDSARRequest).filter(
            MessagingDSARRequest.project_id == request.project_id,
            or_(*dsar_subject_filters),
        ).update({
            "user_id": None,
            "external_id": None,
            "email_hash": None,
            "result_url": None,
            "result_data": {"erased_at": erased_at.isoformat()},
            "error_message": None,
            "requested_by_ip": None,
        }, synchronize_session=False)

        # The tombstones and winner are one data subject. Deleting the full
        # cluster avoids retaining PII on a merged loser while its campaign
        # audit has already been pseudonymized above.
        db.query(MessagingUser).filter(
            MessagingUser.project_id == request.project_id,
            MessagingUser.id.in_(subject_user_ids),
        ).delete(synchronize_session=False)

        request.result_data = {
            "deleted_user_id": user_id,
            "deleted_user_ids": sorted(subject_user_ids),
            "deleted_at": erased_at.isoformat()
        }
        return True

    async def _process_rectify(
        self,
        db: Session,
        request: MessagingDSARRequest
    ) -> None:
        """
        Rectify (update) user data.
        Updates are passed in result_data field before processing.
        """
        user = self._get_user_for_request(db, request)
        if not user:
            request.error_message = "User not found"
            return

        updates = request.result_data or {}
        updated_fields = ContactProfileService(db).rectify_identifiers(
            user,
            updates,
            source="dsar_rectification",
        )

        if "name" in updates:
            user.name = updates["name"]
            updated_fields.append("name")

        if "properties" in updates:
            existing = dict(user.properties or {})
            existing.update(updates["properties"])
            user.properties = existing
            updated_fields.append("properties")

        user.updated_at = datetime.utcnow()

        request.result_data = {
            "updated_user_id": user.id,
            "updated_fields": updated_fields,
            "updated_at": datetime.utcnow().isoformat()
        }

    async def _process_withdraw_consent(
        self,
        db: Session,
        request: MessagingDSARRequest
    ) -> None:
        """Withdraw all consent for a user."""
        user = self._get_user_for_request(db, request)
        if not user:
            request.error_message = "User not found"
            return

        consent_manager.revoke_all_consent(
            db=db,
            user=user,
            ip=request.requested_by_ip
        )

        ContactProfileService(db).withdraw_permission_evidence(
            user,
            dsar_request_id=request.id,
            source="dsar",
        )

        # Also unsubscribe
        user.is_subscribed = False

        request.result_data = {
            "user_id": user.id,
            "consent_withdrawn_at": datetime.utcnow().isoformat()
        }

    def _get_user_for_request(
        self,
        db: Session,
        request: MessagingDSARRequest
    ) -> Optional[MessagingUser]:
        """Get the user associated with a DSAR request."""
        if request.user_id:
            user = db.query(MessagingUser).filter(
                MessagingUser.id == request.user_id,
                MessagingUser.project_id == request.project_id,
            ).first()
        else:
            user = self._find_user(
                db,
                request.project_id,
                request.external_id,
                request.email_hash
            )
        if not user:
            return None

        # A merged loser is a tombstone for the same data subject. Resolve to
        # the active winner so exports/deletes cannot omit the canonical data.
        seen: set[int] = set()
        while user.status == "merged" and user.merged_into and user.id not in seen:
            seen.add(user.id)
            next_user = db.query(MessagingUser).filter(
                MessagingUser.id == user.merged_into,
                MessagingUser.project_id == request.project_id,
            ).first()
            if not next_user:
                break
            user = next_user
        return user

    @staticmethod
    def _subject_user_ids(
        db: Session,
        project_id: int,
        canonical_user_id: int,
    ) -> set[int]:
        """Return the winner and every project-scoped tombstone in its chain."""
        user_ids = {canonical_user_id}
        while True:
            merged_ids = {
                row.id
                for row in db.query(MessagingUser.id).filter(
                    MessagingUser.project_id == project_id,
                    MessagingUser.merged_into.in_(user_ids),
                ).all()
            }
            new_ids = merged_ids - user_ids
            if not new_ids:
                return user_ids
            user_ids.update(new_ids)

    def get_pending_requests(
        self,
        db: Session,
        project_id: int,
        limit: int = 50
    ) -> list[MessagingDSARRequest]:
        """Get pending DSAR requests for a project."""
        return db.query(MessagingDSARRequest).filter(
            MessagingDSARRequest.project_id == project_id,
            MessagingDSARRequest.status == DSARRequestStatus.pending
        ).order_by(MessagingDSARRequest.requested_at).limit(limit).all()


# Singleton instance
dsar_processor = DSARProcessor()
