from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import WhatsAppInstance
from app.models.campaigns import ContactVerificationRequest, OperationalAlert
from app.models.messaging import ContactVerificationJob, ProjectVerificationSettings
from app.services.contact_verification.access import VerificationAccessError
from app.services.contact_verification.service import (
    ACTIVE_JOB_STATUSES,
    ContactVerificationService,
)
from app.services.contact_verification.providers import provider_registry
from app.services.operational_alert_service import OperationalAlertService

logger = logging.getLogger(__name__)

_REQUEST_CLAIM_TIMEOUT = timedelta(minutes=15)
_REQUEST_CLAIM_STATUSES = {"claiming", "processing_claimed"}


class ContactVerificationWorker:
    """Durable contact-verification worker run by the lifespan leader."""

    def __init__(self, poll_interval: int = 5, automatic_scan_interval: int = 60):
        self.poll_interval = poll_interval
        self.automatic_scan_interval = automatic_scan_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_automatic_scan: Optional[datetime] = None
        self._last_recovery_scan: Optional[datetime] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(db_session_factory or SessionLocal),
            name="contact-verification-worker",
        )
        logger.info("Contact verification worker started (poll=%ss)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Contact verification worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    await self._process_cycle(db)
                finally:
                    db.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Contact verification worker cycle failed")
            await asyncio.sleep(self.poll_interval)

    async def _process_cycle(self, db: Session) -> int:
        await self._recover_provider_credits_if_due(db)

        # Remote files contain clear-text identifiers. Their deletion is a
        # privacy obligation independent of whether the parent job succeeded,
        # failed or was cancelled, so the durable cleanup sweeper runs first.
        cleanup_attempts = await ContactVerificationService(db).sweep_remote_cleanup()
        if cleanup_attempts:
            return cleanup_attempts

        # First-seen/change requests always outrank stale sweeps and manual
        # bulk jobs. They are durable, so ingestion never waits on a provider.
        now = datetime.utcnow()
        request = db.query(ContactVerificationRequest).filter(
            or_(
                ContactVerificationRequest.status.in_(["queued", "processing"]),
                and_(
                    ContactVerificationRequest.status.in_(list(_REQUEST_CLAIM_STATUSES)),
                    or_(
                        ContactVerificationRequest.started_at.is_(None),
                        ContactVerificationRequest.started_at < now - _REQUEST_CLAIM_TIMEOUT,
                    ),
                ),
            ),
        ).order_by(
            ContactVerificationRequest.requested_at.asc(),
        ).with_for_update(skip_locked=True).first()
        if request:
            creating_job = request.job_id is None
            request.status = "claiming" if creating_job else "processing_claimed"
            # started_at doubles as a durable worker lease. A crashed claim is
            # recoverable, while another worker cannot duplicate the job or
            # provider call as soon as create_job() commits internally.
            request.started_at = now
            if creating_job:
                request.attempt_count = (request.attempt_count or 0) + 1
            db.commit()
            db.refresh(request)
            await self._process_request(db, request)
            return 1

        self._schedule_automatic_jobs_if_due(db)

        # The lifespan's global advisory lock elects a single scheduler leader;
        # SKIP LOCKED additionally protects this dequeue from administrative
        # cancellation and from any future dedicated worker processes.
        job = db.query(ContactVerificationJob).filter(
            ContactVerificationJob.status.in_(ACTIVE_JOB_STATUSES),
        ).order_by(
            ContactVerificationJob.created_at.asc(),
        ).with_for_update(skip_locked=True).first()
        if not job:
            return 0

        if job.status == "queued":
            job.status = "running"
            job.started_at = job.started_at or datetime.utcnow()
            db.commit()
            db.refresh(job)

        await ContactVerificationService(db).process_job(job)
        db.refresh(job)
        if job.status == "failed" and "insufficient" in str(job.error_message or "").lower():
            self._open_credit_alert(db, job.project_id, job.provider, job.error_message or "Provider credits depleted")
        return 1

    async def _process_request(self, db: Session, request: ContactVerificationRequest) -> None:
        service = ContactVerificationService(db)
        if request.status == "claiming":
            try:
                # Recover the exact job after a crash in the small window
                # between create_job() committing and linking request.job_id.
                job = self._job_for_request(db, request)
                if not job:
                    job = service.create_job(
                        request.project_id,
                        request.verification_type,
                        {
                            "scope": "contact",
                            "verification_request_id": int(request.id),
                        },
                        requested_by_user_id=None,
                        trigger_type="first_seen_or_change",
                        contact_id=request.user_id,
                        endpoint_id=request.endpoint_id,
                    )
            except (ValueError, VerificationAccessError) as exc:
                db.rollback()
                request = db.query(ContactVerificationRequest).filter(
                    ContactVerificationRequest.id == request.id,
                ).first()
                request.status = "failed"
                request.error_code = "not_authorized_or_ineligible"
                request.error_message = str(exc)
                request.finished_at = datetime.utcnow()
                db.commit()
                return
            request.job_id = job.id
            request.status = "processing_claimed"
            db.commit()
        else:
            job = db.query(ContactVerificationJob).filter(
                ContactVerificationJob.id == request.job_id,
                ContactVerificationJob.project_id == request.project_id,
            ).first()
            if not job:
                request.status = "queued"
                request.job_id = None
                request.started_at = None
                db.commit()
                return

        await service.process_job(job)
        db.refresh(job)
        db.refresh(request)
        item = next(iter(job.items), None)
        if job.status in {"completed", "completed_with_errors"} and item and item.status == "succeeded":
            request.status = "succeeded"
            request.canonical_status = item.canonical_status
            request.provider_status = item.provider_status
            request.finished_at = datetime.utcnow()
            request.error_code = None
            request.error_message = None
            db.commit()
            OperationalAlertService(db).resolve(
                request.project_id, f"verification:credits:{request.provider}:{request.project_id}",
            )
            return
        if job.status in {"completed", "completed_with_errors", "cancelled"}:
            item_status = str(getattr(item, "status", "") or "")
            if job.status == "cancelled" or item_status == "cancelled":
                request.status = "cancelled"
                request.error_code = getattr(item, "error_code", None) or "verification_cancelled"
                request.error_message = (
                    getattr(item, "error_message", None) or "Verification was cancelled"
                )
            elif item_status == "skipped":
                request.status = "skipped"
                request.error_code = getattr(item, "error_code", None) or "verification_skipped"
                request.error_message = getattr(item, "error_message", None)
            else:
                request.status = "failed"
                request.error_code = (
                    getattr(item, "error_code", None)
                    or ("missing_job_item" if item is None else "missing_provider_result")
                )
                request.error_message = (
                    getattr(item, "error_message", None)
                    or "Verification completed without a conclusive provider result"
                )
            request.finished_at = datetime.utcnow()
            db.commit()
            return
        if job.status == "failed":
            error_code = getattr(item, "error_code", None) if item else None
            if error_code == "insufficient_credits" or "insufficient" in str(job.error_message or "").lower():
                request.status = "paused_no_credits"
                request.error_code = "insufficient_credits"
                request.error_message = job.error_message
                request.job_id = None
                db.commit()
                self._open_credit_alert(
                    db, request.project_id, request.provider,
                    job.error_message or "Provider credits depleted",
                )
                return
            request.status = "failed"
            request.error_code = error_code or "provider_error"
            request.error_message = job.error_message
            request.finished_at = datetime.utcnow()
            db.commit()
            return

        # Provider work is still active. Release this worker lease, retaining
        # the durable job link for the next poll.
        request.status = "processing"
        db.commit()

    @staticmethod
    def _job_for_request(
        db: Session,
        request: ContactVerificationRequest,
    ) -> ContactVerificationJob | None:
        candidates = db.query(ContactVerificationJob).filter(
            ContactVerificationJob.project_id == request.project_id,
            ContactVerificationJob.verification_type == request.verification_type,
            ContactVerificationJob.trigger_type == "first_seen_or_change",
            ContactVerificationJob.endpoint_id == request.endpoint_id,
        ).order_by(ContactVerificationJob.id.desc()).all()
        request_id = int(request.id)
        for job in candidates:
            if int((job.selection or {}).get("verification_request_id") or 0) != request_id:
                continue
            # A no-credit request is intentionally retried as a new job after
            # the provider balance recovers; all other states are recovered.
            if job.status == "failed" and "insufficient" in str(job.error_message or "").lower():
                continue
            return job
        return None

    def _open_credit_alert(self, db: Session, project_id: int, provider: str, message: str) -> None:
        OperationalAlertService(db).open_or_touch(
            project_id=project_id,
            alert_type="verification_provider_no_credits",
            dedupe_key=f"verification:credits:{provider}:{project_id}",
            title="Contact verification paused: provider credits depleted",
            message=message,
            severity="critical",
            context={"provider": provider, "operation": "contact_verification"},
        )

    async def _recover_provider_credits_if_due(self, db: Session) -> None:
        now = datetime.utcnow()
        if self._last_recovery_scan and now - self._last_recovery_scan < timedelta(seconds=60):
            return
        self._last_recovery_scan = now
        alerts = db.query(OperationalAlert).filter(
            OperationalAlert.alert_type == "verification_provider_no_credits",
            OperationalAlert.status.in_(["open", "acknowledged"]),
            OperationalAlert.project_id.isnot(None),
        ).all()
        for alert in alerts:
            provider = str((alert.context or {}).get("provider") or "millionverifier")
            try:
                credits = await provider_registry.email(provider).credits()
            except Exception:
                continue
            if int(credits.get("credits") or 0) <= 0:
                continue
            db.query(ContactVerificationRequest).filter(
                ContactVerificationRequest.project_id == alert.project_id,
                ContactVerificationRequest.provider == provider,
                ContactVerificationRequest.status == "paused_no_credits",
            ).update({
                "status": "queued",
                "error_code": None,
                "error_message": None,
                "job_id": None,
            }, synchronize_session=False)
            db.commit()
            OperationalAlertService(db).resolve(alert.project_id, alert.dedupe_key)

    def _schedule_automatic_jobs_if_due(self, db: Session) -> None:
        now = datetime.utcnow()
        if (
            self._last_automatic_scan
            and now - self._last_automatic_scan < timedelta(seconds=self.automatic_scan_interval)
        ):
            return
        self._last_automatic_scan = now

        email_recheck_column = getattr(
            ProjectVerificationSettings, "email_recheck_enabled",
            ProjectVerificationSettings.email_auto_verify,
        )
        whatsapp_recheck_column = getattr(
            ProjectVerificationSettings, "whatsapp_recheck_enabled",
            ProjectVerificationSettings.whatsapp_auto_verify,
        )
        settings_rows = db.query(ProjectVerificationSettings).filter(
            (email_recheck_column == True)
            | (whatsapp_recheck_column == True),
        ).all()
        service = ContactVerificationService(db)
        for settings in settings_rows:
            for verification_type, enabled, days in (
                ("email", bool(getattr(settings, "email_recheck_enabled", settings.email_auto_verify)), settings.email_recheck_days),
                ("whatsapp", bool(getattr(settings, "whatsapp_recheck_enabled", settings.whatsapp_auto_verify)), settings.whatsapp_recheck_days),
            ):
                if not enabled:
                    continue
                provider_code = settings.email_provider if verification_type == "email" else settings.whatsapp_provider
                credit_alert = db.query(OperationalAlert.id).filter(
                    OperationalAlert.project_id == settings.project_id,
                    OperationalAlert.dedupe_key == f"verification:credits:{provider_code}:{settings.project_id}",
                    OperationalAlert.status.in_(["open", "acknowledged"]),
                ).first()
                if credit_alert:
                    continue
                if verification_type == "email" and not provider_registry.email(
                    settings.email_provider,
                ).configured:
                    continue
                if verification_type == "whatsapp":
                    connected_instance = db.query(WhatsAppInstance.id).filter(
                        WhatsAppInstance.id == settings.whatsapp_instance_id,
                        WhatsAppInstance.project_id == settings.project_id,
                        WhatsAppInstance.provider_type == "evolution_api",
                        WhatsAppInstance.is_active == True,
                        WhatsAppInstance.connection_status.in_(["connected", "open"]),
                    ).first()
                    if not connected_instance:
                        continue
                active = db.query(ContactVerificationJob.id).filter(
                    ContactVerificationJob.project_id == settings.project_id,
                    ContactVerificationJob.verification_type == verification_type,
                    ContactVerificationJob.status.in_(ACTIVE_JOB_STATUSES),
                ).first()
                if active:
                    continue
                try:
                    service.create_job(
                        settings.project_id,
                        verification_type,
                        {"scope": "stale", "days": days},
                        requested_by_user_id=None,
                        trigger_type="scheduled_recheck",
                    )
                except (ValueError, VerificationAccessError):
                    # No eligible contacts or no entitlement is an expected
                    # state; the next scan re-evaluates after new contact data.
                    db.rollback()
                except Exception:
                    db.rollback()
                    logger.exception(
                        "Could not schedule automatic %s verification for project %s",
                        verification_type,
                        settings.project_id,
                    )


contact_verification_worker = ContactVerificationWorker()
