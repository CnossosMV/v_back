from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Project, WhatsAppInstance
from app.models.campaigns import ContactEndpoint, OperationalAlert
from app.models.messaging import (
    ContactVerificationItem,
    ContactVerificationJob,
    ContactVerificationOperation,
    ContactVerificationState,
    MessagingUser,
    ProjectVerificationSettings,
)
from app.services.contact_verification.access import VerificationAccessService
from app.services.contact_verification.providers import (
    ProviderConfigurationError,
    ProviderRequestError,
    VerificationResult,
    provider_registry,
)
from app.services.campaigns.alerts import upsert_operational_alert
from app.services.messaging.pii_hasher import pii_hasher

logger = logging.getLogger(__name__)

TERMINAL_ITEM_STATUSES = {"succeeded", "failed", "skipped", "cancelled"}
ACTIVE_JOB_STATUSES = {"queued", "running", "provider_processing", "cancel_requested"}
UPLOAD_RECONCILIATION_STATUSES = {
    "upload_intent",
    "upload_reconciling",
    "upload_retry_ready",
    "upload_reconciliation_conflict",
    "upload_reconciliation_unsupported",
}
ACTIVE_OPERATION_STATUSES = {
    "queued", "running", "provider_processing", "provider_deleting",
    *UPLOAD_RECONCILIATION_STATUSES,
}
VALID_SCOPES = {"all", "never_verified", "stale", "recent"}
VALID_RECENT_FIELDS = {"created_at", "updated_at", "last_seen_at"}
MAX_BULK_IDENTIFIERS = 50_000
WHATSAPP_INTERVAL_SECONDS = 10
WHATSAPP_DAILY_LIMIT = 250
UPLOAD_RECONCILIATION_MIN_ATTEMPTS = 3
UPLOAD_RECONCILIATION_CONSISTENCY_DELAY = timedelta(minutes=2)
_IDEMPOTENCY_SELECTION_KEY = "_idempotency"


class ContactVerificationService:
    def __init__(self, db: Session):
        self.db = db
        self.access = VerificationAccessService(db)

    def get_or_create_settings(self, project_id: int) -> ProjectVerificationSettings:
        row = self.db.query(ProjectVerificationSettings).filter(
            ProjectVerificationSettings.project_id == project_id,
        ).first()
        if not row:
            row = ProjectVerificationSettings(project_id=project_id)
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        return row

    def update_settings(self, project_id: int, values: Dict[str, Any]) -> ProjectVerificationSettings:
        row = self.get_or_create_settings(project_id)
        allowed = {
            "email_auto_verify", "whatsapp_auto_verify",
            "email_verify_on_first_seen", "email_recheck_enabled",
            "whatsapp_verify_on_first_seen", "whatsapp_recheck_enabled",
            "email_recheck_days", "whatsapp_recheck_days",
            "email_send_policy", "whatsapp_send_policy", "whatsapp_instance_id",
        }
        for key, value in values.items():
            if key in allowed and (value is not None or key == "whatsapp_instance_id"):
                setattr(row, key, value)
        self._validate_settings(row)
        if row.whatsapp_instance_id:
            instance = self.db.query(WhatsAppInstance).filter(
                WhatsAppInstance.id == row.whatsapp_instance_id,
                WhatsAppInstance.project_id == project_id,
                WhatsAppInstance.provider_type == "evolution_api",
                WhatsAppInstance.is_active == True,
            ).first()
            if not instance:
                raise ValueError("WhatsApp instance must be an active Evolution instance linked to this project")
        self.db.commit()
        self.db.refresh(row)
        return row

    @staticmethod
    def _validate_settings(row: ProjectVerificationSettings) -> None:
        policies = {"report_only", "block_invalid", "block_invalid_and_risky"}
        if row.email_send_policy not in policies or row.whatsapp_send_policy not in policies:
            raise ValueError("Invalid verification send policy")
        if not 1 <= row.email_recheck_days <= 3650 or not 1 <= row.whatsapp_recheck_days <= 3650:
            raise ValueError("Recheck days must be between 1 and 3650")

    def preview(self, project_id: int, verification_type: str, selection: Dict[str, Any]) -> Dict[str, Any]:
        self.access.authorize(project_id)
        users = self._selected_users(project_id, verification_type, selection)
        hashes: set[str] = set()
        missing = 0
        for user in users:
            identifier, identifier_hash = self._identifier_and_hash(project_id, user, verification_type)
            if not identifier or not identifier_hash:
                missing += 1
                continue
            hashes.add(identifier_hash)
        return {
            "verification_type": verification_type,
            "candidate_count": len(users),
            "unique_count": len(hashes),
            "skipped_missing_identifier": missing,
            "estimated_days": (
                max(1, (len(hashes) + WHATSAPP_DAILY_LIMIT - 1) // WHATSAPP_DAILY_LIMIT)
                if verification_type == "whatsapp" and hashes else 0
            ),
            "selection": selection,
        }

    def resolve_individual_endpoint(
        self,
        project_id: int,
        contact_id: int,
        verification_type: str,
    ) -> ContactEndpoint:
        """Resolve the canonical endpoint used by an individual verification."""
        expected_type = "email" if verification_type == "email" else "phone"
        endpoint = self.db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == contact_id,
            ContactEndpoint.endpoint_type == expected_type,
            ContactEndpoint.status == "active",
        ).order_by(
            ContactEndpoint.is_primary.desc(),
            ContactEndpoint.updated_at.desc(),
            ContactEndpoint.id.desc(),
        ).first()
        if not endpoint:
            raise ValueError(
                f"Contact has no active {expected_type} endpoint to verify"
            )
        return endpoint

    def create_job(
        self,
        project_id: int,
        verification_type: str,
        selection: Dict[str, Any],
        requested_by_user_id: Optional[int],
        *,
        trigger_type: str = "manual",
        expected_candidate_count: Optional[int] = None,
        expected_unique_count: Optional[int] = None,
        contact_id: Optional[int] = None,
        endpoint_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> ContactVerificationJob:
        auth = self.access.authorize(project_id)
        settings = self.get_or_create_settings(project_id)
        provider = settings.email_provider if verification_type == "email" else settings.whatsapp_provider
        persisted_selection = {
            key: value for key, value in dict(selection or {}).items()
            if key != _IDEMPOTENCY_SELECTION_KEY
        }
        selected_endpoint = None
        if endpoint_id is not None:
            expected_type = "email" if verification_type == "email" else "phone"
            selected_endpoint = self.db.query(ContactEndpoint).filter(
                ContactEndpoint.id == endpoint_id,
                ContactEndpoint.project_id == project_id,
                ContactEndpoint.endpoint_type == expected_type,
                ContactEndpoint.status == "active",
            ).first()
            if not selected_endpoint:
                raise ValueError("Contact endpoint not found or incompatible with verification type")
            if contact_id is not None and selected_endpoint.user_id != contact_id:
                raise ValueError("Contact endpoint belongs to another contact")
            contact_id = selected_endpoint.user_id
        if trigger_type == "manual":
            normalized_key = str(idempotency_key or "").strip()
            if len(normalized_key) < 8 or len(normalized_key) > 128:
                raise ValueError("Manual verification jobs require an idempotency key between 8 and 128 characters")
            key_hash = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
            request_hash = self._manual_job_request_hash(
                verification_type=verification_type,
                selection=persisted_selection,
                expected_candidate_count=expected_candidate_count,
                expected_unique_count=expected_unique_count,
            )
            self._lock_manual_job_idempotency(project_id, key_hash)
            existing = self._manual_job_for_idempotency_key(project_id, key_hash)
            if existing:
                existing_hash = str(
                    ((existing.selection or {}).get(_IDEMPOTENCY_SELECTION_KEY) or {}).get("request_hash")
                    or ""
                )
                if existing_hash != request_hash:
                    raise ValueError("Idempotency key was already used for a different verification request")
                # Release the transaction-scoped advisory lock before the API
                # serializes the replayed row.
                self.db.commit()
                self.db.refresh(existing)
                setattr(existing, "_idempotency_replayed", True)
                return existing
            persisted_selection[_IDEMPOTENCY_SELECTION_KEY] = {
                "version": 1,
                "key_hash": key_hash,
                "request_hash": request_hash,
            }
        elif trigger_type == "individual":
            normalized_key = str(idempotency_key or "").strip()
            if len(normalized_key) < 8 or len(normalized_key) > 128:
                raise ValueError(
                    "Individual verification jobs require an idempotency key between 8 and 128 characters"
                )
            if not selected_endpoint:
                raise ValueError("Individual verification jobs require a canonical contact endpoint")
            key_hash = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
            request_hash = self._individual_job_request_hash(
                verification_type=verification_type,
                endpoint_id=int(selected_endpoint.id),
            )
            self._lock_individual_job_idempotency(project_id, key_hash)
            existing = self._individual_job_for_idempotency_key(project_id, key_hash)
            if existing:
                metadata = (existing.selection or {}).get(_IDEMPOTENCY_SELECTION_KEY) or {}
                existing_hash = str(metadata.get("request_hash") or "")
                if (
                    existing_hash != request_hash
                    or existing.endpoint_id != selected_endpoint.id
                    or existing.verification_type != verification_type
                ):
                    raise ValueError(
                        "Idempotency key was already used for a different individual verification request"
                    )
                self.db.commit()
                self.db.refresh(existing)
                setattr(existing, "_idempotency_replayed", True)
                return existing
            persisted_selection[_IDEMPOTENCY_SELECTION_KEY] = {
                "version": 1,
                "operation": "individual",
                "key_hash": key_hash,
                "request_hash": request_hash,
                "endpoint_id": int(selected_endpoint.id),
                "verification_type": verification_type,
            }
        if contact_id is not None:
            users = self.db.query(MessagingUser).filter(
                MessagingUser.id == contact_id,
                MessagingUser.project_id == project_id,
                MessagingUser.status == "active",
                MessagingUser.is_sandbox == False,
            ).all()
        else:
            users = self._selected_users(project_id, verification_type, persisted_selection)
        prepared: List[Tuple[MessagingUser, str, Optional[int]]] = []
        unique_hashes: set[str] = set()
        for user in users:
            current_endpoint_id = selected_endpoint.id if selected_endpoint else None
            _identifier, identifier_hash = self._identifier_and_hash(
                project_id, user, verification_type, endpoint_id=current_endpoint_id,
            )
            if identifier_hash:
                prepared.append((user, identifier_hash, current_endpoint_id))
                unique_hashes.add(identifier_hash)
        if expected_candidate_count is not None and expected_candidate_count != len(users):
            raise ValueError("Selection changed since preview; refresh the preview before confirming")
        if expected_unique_count is not None and expected_unique_count != len(unique_hashes):
            raise ValueError("Selection changed since preview; refresh the preview before confirming")
        if not prepared:
            raise ValueError("No eligible contacts found")
        reservation_id = self.access.reserve(project_id, len(unique_hashes))
        job = ContactVerificationJob(
            project_id=project_id,
            endpoint_id=selected_endpoint.id if selected_endpoint else None,
            verification_type=verification_type,
            provider=provider,
            trigger_type=trigger_type,
            status="queued",
            selection=persisted_selection,
            requested_by_user_id=requested_by_user_id,
            candidate_count=len(prepared),
            unique_count=len(unique_hashes),
            billing_disposition=auth.billing_disposition,
            billing_reservation_id=reservation_id,
        )
        self.db.add(job)
        self.db.flush()
        for user, identifier_hash, current_endpoint_id in prepared:
            self.db.add(ContactVerificationItem(
                project_id=project_id,
                job_id=job.id,
                user_id=user.id,
                endpoint_id=current_endpoint_id,
                verification_type=verification_type,
                identifier_hash=identifier_hash,
                status="queued",
            ))
        self.db.commit()
        self.db.refresh(job)
        setattr(job, "_idempotency_replayed", False)
        return job

    @staticmethod
    def _manual_job_request_hash(
        *,
        verification_type: str,
        selection: Dict[str, Any],
        expected_candidate_count: Optional[int],
        expected_unique_count: Optional[int],
    ) -> str:
        canonical = json.dumps(
            {
                "verification_type": verification_type,
                "selection": selection,
                "expected_candidate_count": expected_candidate_count,
                "expected_unique_count": expected_unique_count,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _individual_job_request_hash(
        *,
        verification_type: str,
        endpoint_id: int,
    ) -> str:
        canonical = json.dumps(
            {
                "verification_type": verification_type,
                "endpoint_id": endpoint_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def find_manual_job_replay(
        self,
        project_id: int,
        verification_type: str,
        selection: Dict[str, Any],
        *,
        expected_candidate_count: Optional[int],
        expected_unique_count: Optional[int],
        idempotency_key: str,
    ) -> Optional[ContactVerificationJob]:
        """Resolve an API retry before any provider health/credit request."""
        normalized_key = str(idempotency_key or "").strip()
        if len(normalized_key) < 8 or len(normalized_key) > 128:
            raise ValueError("Manual verification jobs require an idempotency key between 8 and 128 characters")
        key_hash = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
        request_hash = self._manual_job_request_hash(
            verification_type=verification_type,
            selection={
                key: value for key, value in dict(selection or {}).items()
                if key != _IDEMPOTENCY_SELECTION_KEY
            },
            expected_candidate_count=expected_candidate_count,
            expected_unique_count=expected_unique_count,
        )
        existing = self._manual_job_for_idempotency_key(project_id, key_hash)
        if not existing:
            return None
        existing_hash = str(
            ((existing.selection or {}).get(_IDEMPOTENCY_SELECTION_KEY) or {}).get("request_hash")
            or ""
        )
        if existing_hash != request_hash:
            raise ValueError("Idempotency key was already used for a different verification request")
        setattr(existing, "_idempotency_replayed", True)
        return existing

    def _lock_manual_job_idempotency(self, project_id: int, key_hash: str) -> None:
        bind = self.db.get_bind()
        if bind.dialect.name != "postgresql":
            raise ValueError("Manual verification idempotency requires PostgreSQL advisory locks")
        lock_scope = f"contact-verification:{project_id}:{key_hash}"
        self.db.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_scope))))

    def _manual_job_for_idempotency_key(
        self,
        project_id: int,
        key_hash: str,
    ) -> Optional[ContactVerificationJob]:
        # No new column/index is introduced in this patch. The advisory lock
        # makes creation safe; the JSON audit envelope keeps retries durable.
        candidates = self.db.query(ContactVerificationJob).filter(
            ContactVerificationJob.project_id == project_id,
            ContactVerificationJob.trigger_type == "manual",
        ).order_by(ContactVerificationJob.id.desc()).all()
        for candidate in candidates:
            metadata = (candidate.selection or {}).get(_IDEMPOTENCY_SELECTION_KEY) or {}
            if str(metadata.get("key_hash") or "") == key_hash:
                return candidate
        return None

    def _lock_individual_job_idempotency(self, project_id: int, key_hash: str) -> None:
        bind = self.db.get_bind()
        if bind.dialect.name != "postgresql":
            raise ValueError("Individual verification idempotency requires PostgreSQL advisory locks")
        lock_scope = f"contact-verification:individual:{project_id}:{key_hash}"
        self.db.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_scope))))

    def _individual_job_for_idempotency_key(
        self,
        project_id: int,
        key_hash: str,
    ) -> Optional[ContactVerificationJob]:
        candidates = self.db.query(ContactVerificationJob).filter(
            ContactVerificationJob.project_id == project_id,
            ContactVerificationJob.trigger_type == "individual",
        ).order_by(ContactVerificationJob.id.desc()).all()
        for candidate in candidates:
            metadata = (candidate.selection or {}).get(_IDEMPOTENCY_SELECTION_KEY) or {}
            if str(metadata.get("key_hash") or "") == key_hash:
                return candidate
        return None

    def cancel_job(self, job: ContactVerificationJob) -> ContactVerificationJob:
        if job.status not in (ACTIVE_JOB_STATUSES | {"reconciliation_required"}):
            return job
        job.cancel_requested = True
        job.status = "cancel_requested"
        self.db.commit()
        self.db.refresh(job)
        return job

    async def process_job(self, job: ContactVerificationJob) -> None:
        try:
            self.access.authorize(job.project_id)
            if job.cancel_requested or job.status == "cancel_requested":
                cleanup_complete = await self._cancel_provider_operations(job)
                self.db.query(ContactVerificationItem).filter(
                    ContactVerificationItem.job_id == job.id,
                    ~ContactVerificationItem.status.in_(TERMINAL_ITEM_STATUSES),
                ).update({"status": "cancelled", "finished_at": datetime.utcnow()}, synchronize_session=False)
                self._refresh_counts(job)
                if not cleanup_complete:
                    # Cancellation stops contact work immediately, but the job
                    # remains durable until every remote PII-bearing file has
                    # actually been deleted by the provider.
                    job.status = "cancel_requested"
                    job.finished_at = None
                    self.db.commit()
                    return
                job.status = "cancelled"
                job.finished_at = datetime.utcnow()
                self.access.release(job.billing_reservation_id)
                self.db.commit()
                return
            if not job.started_at:
                job.started_at = datetime.utcnow()
            job.status = "running"
            self.db.commit()
            if job.verification_type == "email":
                if job.trigger_type in {"individual", "first_seen_or_change"}:
                    await self._process_email_single(job)
                else:
                    await self._process_email_bulk(job)
            elif job.verification_type == "whatsapp":
                await self._process_whatsapp(job)
            else:
                raise ValueError(f"Unsupported verification type: {job.verification_type}")
            self._finalize_if_done(job)
            self.db.commit()
        except (ProviderConfigurationError, ProviderRequestError, ValueError) as exc:
            logger.warning("Verification job %s failed: %s", job.id, exc)
            if self._remote_cleanup_pending(job.id):
                self._defer_job_for_remote_cleanup(job, exc)
                return
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = datetime.utcnow()
            self.access.release(job.billing_reservation_id)
            self.db.commit()
        except Exception as exc:
            logger.exception("Verification job %s crashed", job.id)
            self.db.rollback()
            job = self.db.query(ContactVerificationJob).filter(
                ContactVerificationJob.id == job.id,
            ).first()
            if job and self._remote_cleanup_pending(job.id):
                self._defer_job_for_remote_cleanup(job, exc)
                return
            if not job:
                return
            job.status = "failed"
            job.error_message = "Unexpected verification worker error"
            job.finished_at = datetime.utcnow()
            self.access.release(job.billing_reservation_id)
            self.db.commit()

    async def _process_email_single(self, job: ContactVerificationJob) -> None:
        adapter = provider_registry.email(job.provider)
        group = self._next_hash_group(job.id)
        if not group:
            return
        identifier_hash, items = group
        user, identifier = self._current_identifier_for_group(job, identifier_hash, items)
        if not user or not identifier:
            self._skip_items(items, "identifier_changed")
            return
        operation = ContactVerificationOperation(
            project_id=job.project_id, job_id=job.id, provider=job.provider,
            endpoint_id=items[0].endpoint_id if items else None,
            provider_key_source=adapter.key_source,
            operation_type="single", status="running", item_count=1,
            billing_disposition=job.billing_disposition, started_at=datetime.utcnow(),
        )
        self.db.add(operation)
        self.db.flush()
        try:
            credits = await adapter.credits()
            if int(credits.get("credits") or 0) < 1:
                raise ProviderRequestError(
                    "Insufficient MillionVerifier single-verification credits",
                    code="insufficient_credits",
                    transient=False,
                )
            result = await adapter.verify_single(identifier)
            operation.status = "completed"
            operation.provider_units = 0 if result.provider_status in {"catch_all", "unknown"} else 1
            operation.customer_units = 1
            operation.response_summary = {"provider_status": result.provider_status}
            operation.finished_at = datetime.utcnow()
            self._apply_result(
                job, identifier_hash, items, result,
                adapter.provider_code, adapter.provider_version, adapter.key_source,
            )
        except ProviderRequestError as exc:
            operation.status = "failed"
            operation.error_message = str(exc)
            operation.finished_at = datetime.utcnow()
            self._record_group_error(items, exc)
            if not exc.transient:
                raise

    async def _process_email_bulk(self, job: ContactVerificationJob) -> None:
        adapter = provider_registry.email(job.provider)
        operations = self.db.query(ContactVerificationOperation).filter(
            ContactVerificationOperation.job_id == job.id,
            ContactVerificationOperation.operation_type == "bulk_file",
        ).order_by(ContactVerificationOperation.id).all()

        # An upload intent is committed before the mutating HTTP request. If a
        # process dies or times out after that commit, the only safe next step
        # is provider-side reconciliation by the deterministic opaque filename.
        for operation in [op for op in operations if op.status in UPLOAD_RECONCILIATION_STATUSES]:
            if operation.status == "upload_retry_ready":
                if not await self._dispatch_bulk_upload_intent(job, operation, adapter):
                    return
            else:
                await self._reconcile_bulk_upload_intent(job, operation, adapter)

        operations = self.db.query(ContactVerificationOperation).filter(
            ContactVerificationOperation.job_id == job.id,
            ContactVerificationOperation.operation_type == "bulk_file",
        ).order_by(ContactVerificationOperation.id).all()
        pending = [
            op for op in operations
            if op.status in ({"provider_processing", "provider_deleting"} | UPLOAD_RECONCILIATION_STATUSES)
        ]
        remote_pending = [op for op in pending if op.status in {"provider_processing", "provider_deleting"}]
        if not pending and self._queued_groups(job.id):
            groups = self._queued_groups(job.id)
            upload_rows: List[Tuple[str, str]] = []
            for identifier_hash, items in groups.items():
                _user, identifier = self._current_identifier_for_group(job, identifier_hash, items)
                if not identifier:
                    self._skip_items(items, "identifier_changed")
                    continue
                upload_rows.append((identifier_hash, identifier))
            credits = await adapter.credits()
            if int(credits.get("bulk_credits") or 0) < len(upload_rows):
                raise ProviderRequestError(
                    "Insufficient MillionVerifier bulk credits",
                    code="insufficient_credits",
                    transient=False,
                )
            for start in range(0, len(upload_rows), MAX_BULK_IDENTIFIERS):
                chunk = upload_rows[start:start + MAX_BULK_IDENTIFIERS]
                if not chunk:
                    continue
                operation = self._create_bulk_upload_intent(
                    job,
                    chunk,
                    adapter.key_source,
                    chunk_ordinal=(start // MAX_BULK_IDENTIFIERS) + 1,
                )
                if not await self._dispatch_bulk_upload_intent(job, operation, adapter, rows=chunk):
                    return
            job.status = "provider_processing"
            self.db.commit()
            return

        for op in remote_pending:
            if op.status == "provider_deleting":
                await self._delete_bulk_file(job, op, adapter)
                continue
            try:
                info = await adapter.bulk_info(str(op.provider_operation_id))
            except ProviderRequestError as exc:
                attempt = self._record_bulk_transport_failure(job, op, adapter.key_source, "bulk_poll", exc)
                if exc.transient and attempt < 3:
                    job.status = "provider_processing"
                    continue
                raise
            op.response_summary = {
                "status": info.get("status"), "percent": info.get("percent"),
                "verified": info.get("verified"), "unverified": info.get("unverified"),
            }
            provider_status = str(info.get("status") or "")
            if provider_status in {"error"}:
                op.status = "provider_deleting"
                op.error_message = str(info.get("error") or "MillionVerifier bulk job failed")
                op.response_summary = {
                    **(op.response_summary or {}),
                    "provider_error": op.error_message,
                }
                await self._delete_bulk_file(job, op, adapter)
                continue
            if provider_status not in {"finished", "canceled"}:
                job.status = "provider_processing"
                continue
            try:
                results = await adapter.download_bulk(str(op.provider_operation_id))
            except ProviderRequestError as exc:
                attempt = self._record_bulk_transport_failure(job, op, adapter.key_source, "bulk_download", exc)
                if exc.transient and attempt < 3:
                    job.status = "provider_processing"
                    continue
                raise
            for result_key, result in results.items():
                identifier_hash = result_key
                if result_key.startswith("email:"):
                    project = self.db.query(Project).filter(Project.id == job.project_id).first()
                    if not project:
                        continue
                    salt = project.pii_salt or pii_hasher.get_or_create_project_salt(self.db, job.project_id)
                    identifier_hash = pii_hasher.hash_email(result_key[6:], salt) or ""
                items = self.db.query(ContactVerificationItem).filter(
                    ContactVerificationItem.job_id == job.id,
                    ContactVerificationItem.identifier_hash == identifier_hash,
                    ContactVerificationItem.status == "provider_processing",
                ).all()
                if items:
                    self._apply_result(
                        job, identifier_hash, items, result,
                        adapter.provider_code, adapter.provider_version, adapter.key_source,
                    )
            op.status = "provider_deleting"
            op.provider_units = int(info.get("credit") or 0)
            op.customer_units = op.item_count
            await self._delete_bulk_file(job, op, adapter)
        operations = self.db.query(ContactVerificationOperation).filter(
            ContactVerificationOperation.job_id == job.id,
            ContactVerificationOperation.operation_type == "bulk_file",
        ).all()
        if any(op.status in ACTIVE_OPERATION_STATUSES for op in operations):
            job.status = "provider_processing"
        else:
            # A completed provider report must account for every submitted
            # hash. Never leave an item stuck forever when a malformed report
            # omits a row.
            self.db.query(ContactVerificationItem).filter(
                ContactVerificationItem.job_id == job.id,
                ContactVerificationItem.status == "provider_processing",
            ).update(
                {
                    "status": "failed",
                    "error_code": "missing_provider_result",
                    "error_message": "Provider report omitted this identifier",
                    "finished_at": datetime.utcnow(),
                },
                synchronize_session=False,
            )

    def _create_bulk_upload_intent(
        self,
        job: ContactVerificationJob,
        rows: List[Tuple[str, str]],
        key_source: str,
        *,
        chunk_ordinal: int,
    ) -> ContactVerificationOperation:
        fingerprint = hashlib.sha256(
            "\n".join(identifier_hash for identifier_hash, _identifier in rows).encode("ascii")
        ).hexdigest()
        intent_token = hashlib.sha256(
            f"v1:{job.project_id}:{job.id}:{chunk_ordinal}:{fingerprint}".encode("ascii")
        ).hexdigest()
        filename = f"versya-contact-verification-{intent_token}.csv"
        operation = ContactVerificationOperation(
            project_id=job.project_id,
            job_id=job.id,
            provider=job.provider,
            provider_key_source=key_source,
            operation_type="bulk_file",
            status="upload_intent",
            item_count=len(rows),
            billing_disposition=job.billing_disposition,
            request_summary={
                "unique_identifiers": len(rows),
                "chunk_ordinal": chunk_ordinal,
                "upload_intent_token": intent_token,
                "upload_filename": filename,
                "content_fingerprint": fingerprint,
            },
            response_summary={"reconciliation_state": "intent_persisted"},
            started_at=datetime.utcnow(),
        )
        self.db.add(operation)
        self.db.flush()
        marker = self._upload_intent_marker(operation)
        hashes = [identifier_hash for identifier_hash, _identifier in rows]
        self.db.query(ContactVerificationItem).filter(
            ContactVerificationItem.job_id == job.id,
            ContactVerificationItem.status == "queued",
            ContactVerificationItem.identifier_hash.in_(hashes),
        ).update({
            "status": "upload_pending",
            "error_code": marker,
            "started_at": datetime.utcnow(),
        }, synchronize_session=False)
        job.status = "provider_processing"
        # This commit is the durability boundary that must precede the POST.
        self.db.commit()
        self.db.refresh(operation)
        return operation

    @staticmethod
    def _upload_intent_marker(operation: ContactVerificationOperation) -> str:
        token = str((operation.request_summary or {}).get("upload_intent_token") or "")
        return f"bulk_upload:{token}"

    def _upload_intent_items(
        self,
        operation: ContactVerificationOperation,
    ) -> List[ContactVerificationItem]:
        return self.db.query(ContactVerificationItem).filter(
            ContactVerificationItem.job_id == operation.job_id,
            ContactVerificationItem.status == "upload_pending",
            ContactVerificationItem.error_code == self._upload_intent_marker(operation),
        ).order_by(ContactVerificationItem.id).all()

    def _upload_intent_rows(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
    ) -> List[Tuple[str, str]]:
        grouped: Dict[str, List[ContactVerificationItem]] = defaultdict(list)
        for item in self._upload_intent_items(operation):
            grouped[item.identifier_hash].append(item)
        rows: List[Tuple[str, str]] = []
        for identifier_hash, items in grouped.items():
            _user, identifier = self._current_identifier_for_group(job, identifier_hash, items)
            if not identifier:
                self._skip_items(items, "identifier_changed")
                continue
            rows.append((identifier_hash, identifier))
        return rows

    async def _dispatch_bulk_upload_intent(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
        adapter,
        *,
        rows: Optional[List[Tuple[str, str]]] = None,
    ) -> bool:
        rows = rows if rows is not None else self._upload_intent_rows(job, operation)
        if not rows:
            operation.status = "completed"
            operation.finished_at = datetime.utcnow()
            operation.response_summary = {
                **(operation.response_summary or {}),
                "reconciliation_state": "no_current_identifiers",
                "remote_file_created": False,
            }
            self._resolve_upload_reconciliation_alert(job, operation)
            self.db.commit()
            return True
        filename = str((operation.request_summary or {}).get("upload_filename") or "")
        if not filename:
            self._block_upload_reconciliation(
                job, operation, "Persisted upload intent has no deterministic filename",
                status="upload_reconciliation_unsupported",
                severity="critical",
            )
            self.db.commit()
            return False
        operation.status = "upload_intent"
        operation.response_summary = {
            **(operation.response_summary or {}),
            "reconciliation_state": "request_dispatched",
            "request_dispatched_at": datetime.utcnow().isoformat(),
        }
        self.db.commit()
        try:
            data = await adapter.upload_bulk(rows, filename=filename)
        except ProviderRequestError as exc:
            if bool(getattr(exc, "outcome_unknown", False)):
                self._block_upload_reconciliation(
                    job, operation, str(exc),
                    status="upload_reconciling",
                    severity="warning",
                )
                self.db.commit()
                return False
            operation.status = "failed"
            operation.error_message = str(exc)
            operation.finished_at = datetime.utcnow()
            operation.response_summary = {
                **(operation.response_summary or {}),
                "reconciliation_state": "provider_rejected_upload",
                "provider_rejection_code": exc.code,
            }
            self._record_group_error(self._upload_intent_items(operation), exc)
            self.db.commit()
            if exc.transient:
                job.status = "queued"
                self.db.commit()
                return False
            raise
        operation.provider_operation_id = str(data["file_id"])
        operation.status = "provider_processing"
        operation.error_message = None
        operation.response_summary = {
            "percent": data.get("percent", 0),
            "status": data.get("status"),
            "reconciliation_state": "upload_acknowledged",
            "provider_file_name": data.get("file_name"),
        }
        self._mark_upload_items_provider_processing(operation)
        self._resolve_upload_reconciliation_alert(job, operation)
        job.status = "provider_processing"
        self.db.commit()
        return True

    def _mark_upload_items_provider_processing(
        self,
        operation: ContactVerificationOperation,
    ) -> None:
        self.db.query(ContactVerificationItem).filter(
            ContactVerificationItem.job_id == operation.job_id,
            ContactVerificationItem.status == "upload_pending",
            ContactVerificationItem.error_code == self._upload_intent_marker(operation),
        ).update({
            "status": "provider_processing",
            "error_code": None,
            "error_message": None,
        }, synchronize_session=False)

    async def _reconcile_bulk_upload_intent(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
        adapter,
    ) -> bool:
        summary = dict(operation.response_summary or {})
        next_attempt_raw = summary.get("reconciliation_next_attempt_at")
        if next_attempt_raw:
            try:
                if datetime.fromisoformat(str(next_attempt_raw)) > datetime.utcnow():
                    job.status = "cancel_requested" if job.cancel_requested else "reconciliation_required"
                    return False
            except (TypeError, ValueError):
                pass
        finder = getattr(adapter, "find_bulk_files", None)
        if not bool(getattr(adapter, "supports_bulk_reconciliation", False)) or not callable(finder):
            self._block_upload_reconciliation(
                job, operation,
                "Provider does not support safe bulk-upload reconciliation",
                status="upload_reconciliation_unsupported",
                severity="critical",
            )
            return False
        filename = str((operation.request_summary or {}).get("upload_filename") or "")
        if not filename:
            self._block_upload_reconciliation(
                job, operation,
                "Persisted upload intent has no deterministic filename",
                status="upload_reconciliation_unsupported",
                severity="critical",
            )
            return False
        createdate_from = (
            (operation.started_at or datetime.utcnow()) - timedelta(minutes=5)
        ).strftime("%Y-%m-%d %H:%M:%S")
        try:
            matches = await finder(filename=filename, createdate_from=createdate_from)
        except Exception as exc:
            self._record_upload_reconciliation_miss(job, operation, str(exc), severity="warning")
            return False

        valid_matches = []
        count_mismatches = []
        for match in matches:
            file_id = str(match.get("file_id") or "")
            if not file_id:
                continue
            raw_total = match.get("total_rows")
            try:
                total_rows = int(raw_total) if raw_total is not None else operation.item_count
            except (TypeError, ValueError):
                total_rows = -1
            if total_rows != int(operation.item_count or 0):
                count_mismatches.append(file_id)
                continue
            valid_matches.append(match)
        if len(valid_matches) == 1 and not count_mismatches:
            match = valid_matches[0]
            operation.provider_operation_id = str(match["file_id"])
            operation.status = "provider_processing"
            operation.error_message = None
            operation.response_summary = {
                **summary,
                "status": match.get("status"),
                "percent": match.get("percent"),
                "reconciliation_state": "provider_file_adopted",
                "reconciliation_confirmed_at": datetime.utcnow().isoformat(),
                "reconciliation_next_attempt_at": None,
            }
            self._mark_upload_items_provider_processing(operation)
            self._resolve_upload_reconciliation_alert(job, operation)
            job.status = "cancel_requested" if job.cancel_requested else "provider_processing"
            return True
        if valid_matches or count_mismatches:
            discovered_ids = sorted({
                str(match.get("file_id")) for match in matches if match.get("file_id")
            })
            operation.response_summary = {
                **summary,
                "reconciliation_state": "conflict",
                "discovered_provider_file_ids": discovered_ids,
            }
            self._block_upload_reconciliation(
                job, operation,
                "Provider reconciliation returned multiple or count-mismatched files",
                status="upload_reconciliation_conflict",
                severity="critical",
            )
            return False

        attempts = int(summary.get("reconciliation_attempts") or 0) + 1
        age = datetime.utcnow() - (operation.started_at or datetime.utcnow())
        if (
            attempts >= UPLOAD_RECONCILIATION_MIN_ATTEMPTS
            and age >= UPLOAD_RECONCILIATION_CONSISTENCY_DELAY
        ):
            operation.status = "upload_retry_ready"
            operation.error_message = None
            operation.response_summary = {
                **summary,
                "reconciliation_state": "provider_absence_confirmed",
                "reconciliation_attempts": attempts,
                "reconciliation_confirmed_at": datetime.utcnow().isoformat(),
                "reconciliation_next_attempt_at": None,
            }
            self._resolve_upload_reconciliation_alert(job, operation)
            job.status = "cancel_requested" if job.cancel_requested else "provider_processing"
            return True
        self._record_upload_reconciliation_miss(
            job, operation,
            "Provider file is not visible yet; upload remains blocked",
            severity="warning",
            attempts=attempts,
        )
        return False

    def _record_upload_reconciliation_miss(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
        message: str,
        *,
        severity: str,
        attempts: Optional[int] = None,
    ) -> None:
        summary = dict(operation.response_summary or {})
        attempt = attempts if attempts is not None else int(summary.get("reconciliation_attempts") or 0) + 1
        delay = min(300, 15 * (2 ** min(max(attempt - 1, 0), 4)))
        operation.status = "upload_reconciling"
        operation.error_message = message
        operation.response_summary = {
            **summary,
            "reconciliation_state": "pending",
            "reconciliation_attempts": attempt,
            "reconciliation_last_attempt_at": datetime.utcnow().isoformat(),
            "reconciliation_next_attempt_at": (
                datetime.utcnow() + timedelta(seconds=delay)
            ).isoformat(),
        }
        job.status = "cancel_requested" if job.cancel_requested else "reconciliation_required"
        job.finished_at = None
        self._open_upload_reconciliation_alert(job, operation, message, severity)

    def _block_upload_reconciliation(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
        message: str,
        *,
        status: str,
        severity: str,
    ) -> None:
        summary = dict(operation.response_summary or {})
        attempts = int(summary.get("reconciliation_attempts") or 0)
        retry_delay = 15 if status == "upload_reconciling" else 3600
        operation.status = status
        operation.error_message = message
        operation.finished_at = None
        operation.response_summary = {
            **summary,
            "reconciliation_state": status,
            "reconciliation_blocked_at": datetime.utcnow().isoformat(),
            "reconciliation_attempts": attempts,
            "reconciliation_next_attempt_at": (
                datetime.utcnow() + timedelta(seconds=retry_delay)
            ).isoformat(),
        }
        job.status = "cancel_requested" if job.cancel_requested else "reconciliation_required"
        job.finished_at = None
        self._open_upload_reconciliation_alert(job, operation, message, severity)

    def _open_upload_reconciliation_alert(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
        message: str,
        severity: str,
    ) -> None:
        upsert_operational_alert(
            self.db,
            project_id=job.project_id,
            dedupe_key=f"verification:bulk-upload-reconcile:{job.id}:{operation.id}",
            alert_type="verification_bulk_upload_reconciliation_pending",
            title="Bulk verification upload requires provider reconciliation",
            message=message,
            severity=severity,
            context={
                "job_id": job.id,
                "operation_id": operation.id,
                "provider": operation.provider,
                "state": operation.status,
            },
        )

    def _resolve_upload_reconciliation_alert(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
    ) -> None:
        now = datetime.utcnow()
        self.db.query(OperationalAlert).filter(
            OperationalAlert.project_id == job.project_id,
            OperationalAlert.dedupe_key == f"verification:bulk-upload-reconcile:{job.id}:{operation.id}",
            OperationalAlert.status.in_(["open", "acknowledged"]),
        ).update({
            OperationalAlert.status: "resolved",
            OperationalAlert.resolved_at: now,
            OperationalAlert.last_seen_at: now,
        }, synchronize_session=False)

    def _record_bulk_transport_failure(
        self,
        job: ContactVerificationJob,
        provider_operation: ContactVerificationOperation,
        key_source: str,
        operation_type: str,
        exc: Exception,
    ) -> int:
        attempt = self.db.query(ContactVerificationOperation).filter(
            ContactVerificationOperation.job_id == job.id,
            ContactVerificationOperation.operation_type == operation_type,
            ContactVerificationOperation.provider_operation_id == provider_operation.provider_operation_id,
        ).count() + 1
        self.db.add(ContactVerificationOperation(
            project_id=job.project_id,
            job_id=job.id,
            provider=job.provider,
            provider_key_source=key_source,
            operation_type=operation_type,
            provider_operation_id=provider_operation.provider_operation_id,
            status="failed",
            item_count=provider_operation.item_count,
            billing_disposition=job.billing_disposition,
            request_summary={"attempt": attempt},
            error_message=str(exc),
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        ))
        return attempt

    async def sweep_remote_cleanup(self, limit: int = 10) -> int:
        """Reconcile uploads and retry deletion independently of job state.

        Both an upload with an unknown outcome and a provider file awaiting
        deletion can outlive a failed/cancelled job. They remain durable until
        the provider confirms what happened to the PII-bearing file.
        """
        self._recover_legacy_cleanup_rows(max(limit * 10, 100))
        attempted = 0
        last_id = 0
        page_size = max(50, limit * 5)

        while attempted < limit:
            upload_operations = self.db.query(ContactVerificationOperation).filter(
                ContactVerificationOperation.id > last_id,
                ContactVerificationOperation.operation_type == "bulk_file",
                ContactVerificationOperation.status.in_(UPLOAD_RECONCILIATION_STATUSES),
                ContactVerificationOperation.provider_operation_id.is_(None),
            ).order_by(ContactVerificationOperation.id).with_for_update(
                skip_locked=True,
            ).limit(page_size).all()
            if not upload_operations:
                break
            last_id = int(upload_operations[-1].id)
            for operation in upload_operations:
                if attempted >= limit:
                    break
                job = self.db.query(ContactVerificationJob).filter(
                    ContactVerificationJob.id == operation.job_id,
                    ContactVerificationJob.project_id == operation.project_id,
                ).first()
                if not job or not self._reconciliation_retry_due(operation):
                    continue
                attempted += 1
                if operation.status == "upload_retry_ready":
                    job.status = "cancel_requested" if job.cancel_requested else "provider_processing"
                    continue
                try:
                    adapter = provider_registry.email(operation.provider)
                except Exception as exc:
                    self._record_upload_reconciliation_miss(
                        job, operation, str(exc), severity="critical",
                    )
                    continue
                await self._reconcile_bulk_upload_intent(job, operation, adapter)

        last_id = 0
        while attempted < limit:
            operations = self.db.query(ContactVerificationOperation).filter(
                ContactVerificationOperation.id > last_id,
                ContactVerificationOperation.operation_type == "bulk_file",
                ContactVerificationOperation.status == "provider_deleting",
                ContactVerificationOperation.provider_operation_id.isnot(None),
            ).order_by(ContactVerificationOperation.id).with_for_update(
                skip_locked=True,
            ).limit(page_size).all()
            if not operations:
                break
            last_id = int(operations[-1].id)
            for operation in operations:
                if attempted >= limit:
                    break
                job = self.db.query(ContactVerificationJob).filter(
                    ContactVerificationJob.id == operation.job_id,
                    ContactVerificationJob.project_id == operation.project_id,
                ).first()
                if not job or not self._cleanup_retry_due(operation):
                    continue
                attempted += 1
                try:
                    adapter = provider_registry.email(operation.provider)
                except Exception as exc:
                    self._record_cleanup_failure(job, operation, None, exc)
                    continue
                await self._delete_bulk_file(job, operation, adapter)
        if attempted:
            self.db.commit()
        else:
            self.db.rollback()
        return attempted

    @staticmethod
    def _reconciliation_retry_due(operation: ContactVerificationOperation) -> bool:
        raw = (operation.response_summary or {}).get("reconciliation_next_attempt_at")
        if not raw:
            return True
        try:
            return datetime.fromisoformat(str(raw)) <= datetime.utcnow()
        except (TypeError, ValueError):
            return True

    async def _delete_bulk_file(self, job, operation, adapter) -> bool:
        if not self._cleanup_retry_due(operation):
            job.status = "cancel_requested" if job.cancel_requested else "provider_processing"
            return False
        try:
            await adapter.delete_bulk(str(operation.provider_operation_id))
        except Exception as exc:
            if self._provider_confirms_file_absent(exc):
                self._mark_bulk_file_deleted(job, operation, "provider_absent")
                return True
            self._record_cleanup_failure(job, operation, adapter, exc)
            return False
        self._mark_bulk_file_deleted(job, operation, "provider_deleted")
        return True

    def _mark_bulk_file_deleted(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
        confirmation: str,
    ) -> None:
        operation.status = "completed"
        operation.finished_at = datetime.utcnow()
        operation.error_message = None
        operation.response_summary = {
            **(operation.response_summary or {}),
            "remote_file_deleted": True,
            "cleanup_pending": False,
            "cleanup_confirmation": confirmation,
            "cleanup_confirmed_at": datetime.utcnow().isoformat(),
        }
        self._resolve_cleanup_alert(job, operation)

    @staticmethod
    def _provider_confirms_file_absent(exc: Exception) -> bool:
        code = str(getattr(exc, "code", "") or "").lower().replace("-", "_")
        message = str(exc).lower()
        return (
            code in {"http_404", "file_not_found", "not_found"}
            or "file not found" in message
            or ("not found" in message and "file" in message)
            or "does not exist" in message
            or "already deleted" in message
        )

    def _recover_legacy_cleanup_rows(self, limit: int) -> int:
        """Adopt pre-sweeper terminal rows whose remote deletion is unknown."""
        rows = self.db.query(ContactVerificationOperation).filter(
            ContactVerificationOperation.operation_type == "bulk_file",
            ContactVerificationOperation.status.in_(["failed", "cancelled"]),
            ContactVerificationOperation.provider_operation_id.isnot(None),
        ).order_by(ContactVerificationOperation.id).limit(limit).all()
        recovered = 0
        now = datetime.utcnow()
        for operation in rows:
            if (operation.response_summary or {}).get("remote_file_deleted") is True:
                continue
            job = self.db.query(ContactVerificationJob).filter(
                ContactVerificationJob.id == operation.job_id,
                ContactVerificationJob.project_id == operation.project_id,
            ).first()
            if not job:
                continue
            was_cancelled = operation.status == "cancelled" or bool(job.cancel_requested)
            operation.status = "provider_deleting"
            operation.finished_at = None
            operation.response_summary = {
                **(operation.response_summary or {}),
                "cleanup_pending": True,
                "cleanup_recovered_at": now.isoformat(),
            }
            if was_cancelled:
                job.cancel_requested = True
                job.status = "cancel_requested"
            else:
                job.status = "provider_processing"
            job.finished_at = None
            recovered += 1
        return recovered

    @staticmethod
    def _cleanup_retry_due(operation: ContactVerificationOperation) -> bool:
        raw = (operation.response_summary or {}).get("cleanup_next_attempt_at")
        if not raw:
            return True
        try:
            return datetime.fromisoformat(str(raw)) <= datetime.utcnow()
        except (TypeError, ValueError):
            return True

    def _record_cleanup_failure(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
        adapter,
        exc: Exception,
    ) -> None:
        key_source = getattr(adapter, "key_source", operation.provider_key_source or "platform")
        attempt = self._record_bulk_transport_failure(
            job, operation, key_source, "bulk_delete", exc,
        )
        now = datetime.utcnow()
        delay_seconds = min(86_400, 60 * (2 ** min(max(attempt - 1, 0), 10)))
        operation.status = "provider_deleting"
        operation.error_message = f"Provider file cleanup pending: {exc}"
        operation.finished_at = None
        operation.response_summary = {
            **(operation.response_summary or {}),
            "remote_file_deleted": False,
            "cleanup_pending": True,
            "cleanup_attempts": attempt,
            "cleanup_last_attempt_at": now.isoformat(),
            "cleanup_next_attempt_at": (now + timedelta(seconds=delay_seconds)).isoformat(),
        }
        job.status = "cancel_requested" if job.cancel_requested else "provider_processing"
        job.finished_at = None
        upsert_operational_alert(
            self.db,
            project_id=job.project_id,
            dedupe_key=f"verification:bulk-cleanup:{job.id}:{operation.id}",
            alert_type="verification_bulk_cleanup_pending",
            title="Remote verification file deletion is pending",
            message=str(exc),
            severity="critical" if attempt >= 3 else "warning",
            context={
                "job_id": job.id,
                "operation_id": operation.id,
                "provider": operation.provider,
                "attempt": attempt,
                "next_attempt_at": operation.response_summary["cleanup_next_attempt_at"],
            },
        )

    def _resolve_cleanup_alert(
        self,
        job: ContactVerificationJob,
        operation: ContactVerificationOperation,
    ) -> None:
        now = datetime.utcnow()
        self.db.query(OperationalAlert).filter(
            OperationalAlert.project_id == job.project_id,
            OperationalAlert.dedupe_key == f"verification:bulk-cleanup:{job.id}:{operation.id}",
            OperationalAlert.status.in_(["open", "acknowledged"]),
        ).update({
            OperationalAlert.status: "resolved",
            OperationalAlert.resolved_at: now,
            OperationalAlert.last_seen_at: now,
        }, synchronize_session=False)
        other_pending = self.db.query(ContactVerificationOperation.id).filter(
            ContactVerificationOperation.job_id == job.id,
            ContactVerificationOperation.id != operation.id,
            ContactVerificationOperation.operation_type == "bulk_file",
            ContactVerificationOperation.status.in_(["provider_processing", "provider_deleting"]),
        ).first()
        if not other_pending:
            self.db.query(OperationalAlert).filter(
                OperationalAlert.project_id == job.project_id,
                OperationalAlert.dedupe_key == f"verification:bulk-cleanup:{job.id}",
                OperationalAlert.status.in_(["open", "acknowledged"]),
            ).update({
                OperationalAlert.status: "resolved",
                OperationalAlert.resolved_at: now,
                OperationalAlert.last_seen_at: now,
            }, synchronize_session=False)

    def _remote_cleanup_pending(self, job_id: int) -> bool:
        return self.db.query(ContactVerificationOperation.id).filter(
            ContactVerificationOperation.job_id == job_id,
            ContactVerificationOperation.operation_type == "bulk_file",
            ContactVerificationOperation.status.in_(
                {"provider_processing", "provider_deleting"} | UPLOAD_RECONCILIATION_STATUSES
            ),
        ).first() is not None

    def _defer_job_for_remote_cleanup(
        self,
        job: ContactVerificationJob,
        exc: Exception,
    ) -> None:
        self.db.query(ContactVerificationOperation).filter(
            ContactVerificationOperation.job_id == job.id,
            ContactVerificationOperation.operation_type == "bulk_file",
            ContactVerificationOperation.status == "provider_processing",
        ).update({
            ContactVerificationOperation.status: "provider_deleting",
        }, synchronize_session=False)
        upload_reconciliation_pending = self.db.query(ContactVerificationOperation.id).filter(
            ContactVerificationOperation.job_id == job.id,
            ContactVerificationOperation.operation_type == "bulk_file",
            ContactVerificationOperation.status.in_(UPLOAD_RECONCILIATION_STATUSES),
        ).first() is not None
        job.status = (
            "cancel_requested"
            if job.cancel_requested
            else ("reconciliation_required" if upload_reconciliation_pending else "provider_processing")
        )
        job.error_message = f"Remote provider-file reconciliation or cleanup pending: {exc}"
        job.finished_at = None
        upsert_operational_alert(
            self.db,
            project_id=job.project_id,
            dedupe_key=f"verification:bulk-cleanup:{job.id}",
            alert_type="verification_bulk_cleanup_pending",
            title="Remote verification file reconciliation or deletion is pending",
            message=str(exc),
            severity="critical",
            context={"job_id": job.id, "provider": job.provider},
        )
        self.db.commit()

    async def _process_whatsapp(self, job: ContactVerificationJob) -> None:
        settings = self.get_or_create_settings(job.project_id)
        if not settings.whatsapp_instance_id:
            raise ValueError("Select an Evolution instance before verifying WhatsApp numbers")
        instance = self.db.query(WhatsAppInstance).filter(
            WhatsAppInstance.id == settings.whatsapp_instance_id,
            WhatsAppInstance.project_id == job.project_id,
            WhatsAppInstance.provider_type == "evolution_api",
            WhatsAppInstance.is_active == True,
            WhatsAppInstance.connection_status.in_(["connected", "open"]),
        ).first()
        if not instance:
            raise ValueError("The configured Evolution instance is not connected")
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        used_today = self.db.query(func.coalesce(func.sum(ContactVerificationOperation.item_count), 0)).filter(
            ContactVerificationOperation.operation_type == "whatsapp_lookup",
            ContactVerificationOperation.created_at >= today,
            ContactVerificationOperation.request_summary["instance_id"].astext == str(instance.id),
        ).scalar() or 0
        if used_today >= WHATSAPP_DAILY_LIMIT:
            job.status = "queued"
            return
        last_op = self.db.query(ContactVerificationOperation).filter(
            ContactVerificationOperation.operation_type == "whatsapp_lookup",
            ContactVerificationOperation.request_summary["instance_id"].astext == str(instance.id),
        ).order_by(ContactVerificationOperation.created_at.desc()).first()
        if last_op and last_op.created_at and datetime.utcnow() - last_op.created_at < timedelta(seconds=WHATSAPP_INTERVAL_SECONDS):
            job.status = "queued"
            return
        group = self._next_hash_group(job.id)
        if not group:
            return
        identifier_hash, items = group
        _user, identifier = self._current_identifier_for_group(job, identifier_hash, items)
        if not identifier:
            self._skip_items(items, "identifier_changed")
            return
        adapter = provider_registry.whatsapp(job.provider)
        operation = ContactVerificationOperation(
            project_id=job.project_id, job_id=job.id, provider=job.provider,
            provider_key_source=adapter.key_source,
            operation_type="whatsapp_lookup", status="running", item_count=1,
            billing_disposition=job.billing_disposition,
            request_summary={"instance_id": str(instance.id)}, started_at=datetime.utcnow(),
        )
        self.db.add(operation)
        self.db.flush()
        try:
            result = await adapter.verify(instance, self.db, identifier)
            operation.status = "completed"
            operation.customer_units = 1
            operation.response_summary = {"provider_status": result.provider_status}
            operation.finished_at = datetime.utcnow()
            self._apply_result(
                job, identifier_hash, items, result,
                adapter.provider_code, adapter.provider_version, adapter.key_source,
            )
        except ProviderRequestError as exc:
            operation.status = "failed"
            operation.error_message = str(exc)
            operation.finished_at = datetime.utcnow()
            self._record_group_error(items, exc)
            recent_operations = self.db.query(ContactVerificationOperation.status).filter(
                ContactVerificationOperation.job_id == job.id,
                ContactVerificationOperation.operation_type == "whatsapp_lookup",
            ).order_by(ContactVerificationOperation.id.desc()).limit(3).all()
            if len(recent_operations) == 3 and all(row[0] == "failed" for row in recent_operations):
                raise ValueError("WhatsApp verification paused after three provider failures")

    def _selected_users(self, project_id: int, verification_type: str, selection: Dict[str, Any]) -> List[MessagingUser]:
        if verification_type not in {"email", "whatsapp"}:
            raise ValueError("verification_type must be email or whatsapp")
        scope = str(selection.get("scope") or "all")
        if scope not in VALID_SCOPES:
            raise ValueError("Invalid verification scope")
        query = self.db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.status == "active",
            MessagingUser.is_sandbox == False,
        )
        if verification_type == "email":
            query = query.filter(MessagingUser.email.isnot(None), MessagingUser.email != "")
        else:
            query = query.filter(
                (MessagingUser.phone_e164.isnot(None)) | (MessagingUser.phone.isnot(None)),
            )
        if scope == "recent":
            days = max(1, min(int(selection.get("days") or 30), 3650))
            field_name = str(selection.get("recent_field") or "created_at")
            if field_name not in VALID_RECENT_FIELDS:
                raise ValueError("Invalid recent field")
            query = query.filter(getattr(MessagingUser, field_name) >= datetime.utcnow() - timedelta(days=days))
        users = query.order_by(MessagingUser.id).all()
        if scope in {"never_verified", "stale"} and users:
            states = self.db.query(ContactVerificationState).filter(
                ContactVerificationState.project_id == project_id,
                ContactVerificationState.verification_type == verification_type,
                ContactVerificationState.user_id.in_([u.id for u in users]),
            ).all()
            by_user_hash = {
                (state.user_id, state.identifier_hash): state for state in states
            }
            if scope == "never_verified":
                users = [
                    user for user in users
                    if (user.id, self._identifier_and_hash(
                        project_id, user, verification_type,
                    )[1]) not in by_user_hash
                ]
            else:
                days = max(1, min(int(selection.get("days") or 60), 3650))
                cutoff = datetime.utcnow() - timedelta(days=days)
                users = [
                    user for user in users
                    if (
                        (user.id, self._identifier_and_hash(
                            project_id, user, verification_type,
                        )[1]) not in by_user_hash
                        or by_user_hash[(user.id, self._identifier_and_hash(
                            project_id, user, verification_type,
                        )[1])].checked_at <= cutoff
                    )
                ]
        return users

    def _identifier_and_hash(
        self,
        project_id: int,
        user: MessagingUser,
        verification_type: str,
        endpoint_id: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return None, None
        salt = project.pii_salt or pii_hasher.get_or_create_project_salt(self.db, project_id)
        if endpoint_id is not None:
            expected_type = "email" if verification_type == "email" else "phone"
            endpoint = self.db.query(ContactEndpoint).filter(
                ContactEndpoint.id == endpoint_id,
                ContactEndpoint.project_id == project_id,
                ContactEndpoint.user_id == user.id,
                ContactEndpoint.endpoint_type == expected_type,
                ContactEndpoint.status == "active",
            ).first()
            if not endpoint:
                return None, None
            identifier = endpoint.normalized_value or endpoint.value
            normalized = (
                pii_hasher.normalize_email(identifier)
                if verification_type == "email"
                else pii_hasher.normalize_phone(identifier)
            )
            current_hash = (
                pii_hasher.hash_email(normalized, salt)
                if verification_type == "email"
                else pii_hasher.hash_phone(normalized, salt)
            ) if normalized else None
            # Endpoint values are immutable snapshots for verification. A
            # mismatching stored hash means the endpoint was edited in place.
            if current_hash != endpoint.value_hash:
                return None, None
            return normalized or None, current_hash
        if verification_type == "email":
            identifier = pii_hasher.normalize_email(user.email or "")
            return (identifier or None, pii_hasher.hash_email(identifier, salt) if identifier else None)
        identifier = pii_hasher.normalize_phone(user.phone_e164 or user.phone or "")
        return (identifier or None, pii_hasher.hash_phone(identifier, salt) if identifier else None)

    def _queued_groups(self, job_id: int) -> Dict[str, List[ContactVerificationItem]]:
        groups: Dict[str, List[ContactVerificationItem]] = defaultdict(list)
        for item in self.db.query(ContactVerificationItem).filter(
            ContactVerificationItem.job_id == job_id,
            ContactVerificationItem.status == "queued",
        ).order_by(ContactVerificationItem.id).all():
            groups[item.identifier_hash].append(item)
        return groups

    def _next_hash_group(self, job_id: int) -> Optional[Tuple[str, List[ContactVerificationItem]]]:
        groups = self._queued_groups(job_id)
        if not groups:
            return None
        key = next(iter(groups))
        return key, groups[key]

    def _current_identifier_for_group(
        self, job: ContactVerificationJob, identifier_hash: str, items: Iterable[ContactVerificationItem],
    ) -> Tuple[Optional[MessagingUser], Optional[str]]:
        for item in items:
            if not item.user_id:
                continue
            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == item.user_id,
                MessagingUser.project_id == job.project_id,
                MessagingUser.status == "active",
            ).first()
            if not user:
                continue
            identifier, current_hash = self._identifier_and_hash(
                job.project_id, user, job.verification_type, endpoint_id=item.endpoint_id,
            )
            if identifier and current_hash == identifier_hash:
                return user, identifier
        return None, None

    def _apply_result(
        self, job: ContactVerificationJob, identifier_hash: str,
        items: Iterable[ContactVerificationItem], result: VerificationResult,
        provider: str, provider_version: str, provider_key_source: str,
    ) -> None:
        settings = self.get_or_create_settings(job.project_id)
        days = settings.email_recheck_days if job.verification_type == "email" else settings.whatsapp_recheck_days
        now = datetime.utcnow()
        for item in items:
            if not item.user_id:
                item.status = "skipped"
                item.error_code = "missing_contact"
                item.finished_at = now
                continue
            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == item.user_id,
                MessagingUser.project_id == job.project_id,
            ).first()
            if not user:
                item.status = "skipped"
                item.error_code = "missing_contact"
                item.finished_at = now
                continue
            _identifier, current_hash = self._identifier_and_hash(
                job.project_id, user, job.verification_type, endpoint_id=item.endpoint_id,
            )
            if current_hash != identifier_hash:
                item.status = "skipped"
                item.error_code = "identifier_changed"
                item.finished_at = now
                continue
            state_query = self.db.query(ContactVerificationState).filter(
                ContactVerificationState.project_id == job.project_id,
                ContactVerificationState.user_id == user.id,
                ContactVerificationState.verification_type == job.verification_type,
            )
            state_query = (
                state_query.filter(ContactVerificationState.endpoint_id == item.endpoint_id)
                if item.endpoint_id is not None
                else state_query.filter(ContactVerificationState.endpoint_id.is_(None))
            )
            state = state_query.first()
            if not state:
                state = ContactVerificationState(
                    project_id=job.project_id, user_id=user.id,
                    endpoint_id=item.endpoint_id,
                    verification_type=job.verification_type,
                    identifier_hash=identifier_hash, provider=provider,
                    provider_key_source=provider_key_source,
                    canonical_status=result.canonical_status,
                    checked_at=now, expires_at=now + timedelta(days=days),
                    last_attempt_at=now,
                )
                self.db.add(state)
            state.identifier_hash = identifier_hash
            state.provider = provider
            state.provider_key_source = provider_key_source
            state.provider_version = provider_version
            state.canonical_status = result.canonical_status
            state.provider_status = result.provider_status
            state.provider_metadata = result.metadata or None
            state.checked_at = now
            state.expires_at = now + timedelta(days=days)
            state.last_attempt_status = "succeeded"
            state.last_attempt_at = now
            state.last_error_code = None
            endpoint_is_primary = item.endpoint_id is None
            if item.endpoint_id is not None:
                endpoint_is_primary = bool(self.db.query(ContactEndpoint.is_primary).filter(
                    ContactEndpoint.id == item.endpoint_id,
                    ContactEndpoint.project_id == job.project_id,
                ).scalar())
            if job.verification_type == "email" and endpoint_is_primary:
                user.email_hash = identifier_hash
            elif job.verification_type == "whatsapp" and endpoint_is_primary:
                user.phone_hash = identifier_hash
            item.status = "succeeded"
            item.canonical_status = result.canonical_status
            item.provider_status = result.provider_status
            item.provider_metadata = result.metadata or None
            item.finished_at = now
            if job.verification_type == "whatsapp" and endpoint_is_primary:
                user.whatsapp_status = result.canonical_status
                user.whatsapp_checked_at = now

    def _record_group_error(self, items: Iterable[ContactVerificationItem], exc: ProviderRequestError) -> None:
        now = datetime.utcnow()
        for item in items:
            item.attempt_count = (item.attempt_count or 0) + 1
            item.error_code = exc.code
            item.error_message = str(exc)
            if exc.transient and item.attempt_count < 3:
                item.status = "queued"
            else:
                item.status = "failed"
                item.finished_at = now
            if item.user_id:
                state_query = self.db.query(ContactVerificationState).filter(
                    ContactVerificationState.project_id == item.project_id,
                    ContactVerificationState.user_id == item.user_id,
                    ContactVerificationState.verification_type == item.verification_type,
                    ContactVerificationState.identifier_hash == item.identifier_hash,
                )
                state_query = (
                    state_query.filter(ContactVerificationState.endpoint_id == item.endpoint_id)
                    if item.endpoint_id is not None
                    else state_query.filter(ContactVerificationState.endpoint_id.is_(None))
                )
                state = state_query.first()
                if state:
                    state.last_attempt_status = "failed"
                    state.last_attempt_at = now
                    state.last_error_code = exc.code

    @staticmethod
    def _skip_items(items: Iterable[ContactVerificationItem], code: str) -> None:
        now = datetime.utcnow()
        for item in items:
            item.status = "skipped"
            item.error_code = code
            item.finished_at = now

    def _cancel_provider_operations(self, job: ContactVerificationJob):
        async def _cancel() -> bool:
            if job.verification_type != "email":
                return True
            adapter = provider_registry.email(job.provider)
            operations = self.db.query(ContactVerificationOperation).filter(
                ContactVerificationOperation.job_id == job.id,
                ContactVerificationOperation.operation_type == "bulk_file",
                ContactVerificationOperation.status.in_(
                    {"provider_processing", "provider_deleting"} | UPLOAD_RECONCILIATION_STATUSES
                ),
            ).all()
            complete = True
            for operation in operations:
                if operation.status in UPLOAD_RECONCILIATION_STATUSES:
                    if operation.status != "upload_retry_ready":
                        await self._reconcile_bulk_upload_intent(job, operation, adapter)
                    if operation.status == "upload_retry_ready":
                        operation.status = "completed"
                        operation.finished_at = datetime.utcnow()
                        operation.error_message = None
                        operation.response_summary = {
                            **(operation.response_summary or {}),
                            "remote_file_created": False,
                            "reconciliation_state": "provider_absence_confirmed_on_cancel",
                        }
                        self._resolve_upload_reconciliation_alert(job, operation)
                        continue
                    if operation.status != "provider_processing":
                        complete = False
                        continue
                if operation.status == "provider_processing":
                    try:
                        await adapter.stop_bulk(str(operation.provider_operation_id))
                    except Exception:
                        # Deletion is still attempted: providers commonly allow
                        # deleting a completed file even when stop is rejected.
                        logger.warning(
                            "Could not stop provider file %s before deletion",
                            operation.provider_operation_id,
                            exc_info=True,
                        )
                    operation.status = "provider_deleting"
                if not await self._delete_bulk_file(job, operation, adapter):
                    complete = False
            return complete
        return _cancel()

    def _refresh_counts(self, job: ContactVerificationJob) -> None:
        items = self.db.query(ContactVerificationItem).filter(ContactVerificationItem.job_id == job.id).all()
        job.processed_count = sum(1 for item in items if item.status in TERMINAL_ITEM_STATUSES)
        job.valid_count = sum(1 for item in items if item.canonical_status == "valid")
        job.invalid_count = sum(1 for item in items if item.canonical_status == "invalid")
        job.risky_count = sum(1 for item in items if item.canonical_status == "risky")
        job.skipped_count = sum(1 for item in items if item.status in {"skipped", "cancelled"})
        job.failed_count = sum(1 for item in items if item.status == "failed")
        job.provider_units = self.db.query(func.coalesce(func.sum(ContactVerificationOperation.provider_units), 0)).filter(
            ContactVerificationOperation.job_id == job.id,
        ).scalar() or 0

    def _finalize_if_done(self, job: ContactVerificationJob) -> None:
        self._refresh_counts(job)
        remaining = self.db.query(ContactVerificationItem).filter(
            ContactVerificationItem.job_id == job.id,
            ~ContactVerificationItem.status.in_(TERMINAL_ITEM_STATUSES),
        ).count()
        active_operations = self.db.query(ContactVerificationOperation).filter(
            ContactVerificationOperation.job_id == job.id,
            ContactVerificationOperation.status.in_(ACTIVE_OPERATION_STATUSES),
        ).count()
        if remaining == 0 and active_operations == 0:
            job.status = "completed_with_errors" if job.failed_count else "completed"
            job.finished_at = datetime.utcnow()
            self.access.settle(job.billing_reservation_id, int(job.provider_units or 0))
