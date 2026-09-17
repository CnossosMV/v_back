"""Compatibility entry point that enqueues durable WhatsApp verification."""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import WhatsAppInstance, Project
from app.models.messaging import (
    ContactVerificationItem,
    ContactVerificationJob,
    ContactVerificationState,
    MessagingUser,
    ProjectVerificationSettings,
)

logger = logging.getLogger(__name__)

class WhatsAppValidationService:

    async def validate_number(
        self,
        project_id: int,
        user_id: int,
        phone_e164: str,
    ) -> None:
        """Queue the lookup when the project explicitly enabled automation.

        Existing ingestion callers may still launch this method as an asyncio
        task, but no provider request occurs in that non-durable task. The
        lifespan worker owns every external operation.
        """
        from app.database import SessionLocal

        db: Session = SessionLocal()
        try:
            user = db.query(MessagingUser).filter(
                MessagingUser.id == user_id,
                MessagingUser.project_id == project_id,
            ).first()
            if not user:
                return

            settings = db.query(ProjectVerificationSettings).filter(
                ProjectVerificationSettings.project_id == project_id,
            ).first()
            if not settings or not settings.whatsapp_auto_verify:
                self._restore_legacy_cache(db, user)
                return
            instance = self._resolve_evolution_instance(db, project_id)
            if not instance or instance.connection_status not in {"connected", "open"}:
                self._restore_legacy_cache(db, user)
                return

            active = db.query(ContactVerificationItem.id).join(
                ContactVerificationJob,
                ContactVerificationJob.id == ContactVerificationItem.job_id,
            ).filter(
                ContactVerificationItem.project_id == project_id,
                ContactVerificationItem.user_id == user_id,
                ContactVerificationItem.verification_type == "whatsapp",
                ContactVerificationJob.status.in_(
                    ["queued", "running", "provider_processing", "cancel_requested"],
                ),
            ).first()
            if active:
                return

            from app.services.contact_verification.service import ContactVerificationService
            ContactVerificationService(db).create_job(
                project_id,
                "whatsapp",
                {"scope": "contact", "contact_id": user_id},
                requested_by_user_id=None,
                trigger_type="auto",
                contact_id=user_id,
            )

        except Exception as e:
            logger.error("WhatsApp validation enqueue error for user %d: %s", user_id, e)
            try:
                user = db.query(MessagingUser).filter(MessagingUser.id == user_id).first()
                if user:
                    self._restore_legacy_cache(db, user)
            except Exception:
                pass
        finally:
            db.close()

    @staticmethod
    def _restore_legacy_cache(db: Session, user: MessagingUser) -> None:
        state = db.query(ContactVerificationState).filter(
            ContactVerificationState.project_id == user.project_id,
            ContactVerificationState.user_id == user.id,
            ContactVerificationState.verification_type == "whatsapp",
        ).first()
        if state:
            user.whatsapp_status = state.canonical_status
            user.whatsapp_checked_at = state.checked_at
        elif user.whatsapp_status == "checking":
            user.whatsapp_status = "unverified"
            user.whatsapp_checked_at = None
        db.commit()

    @staticmethod
    def _resolve_evolution_instance(
        db: Session, project_id: int,
    ) -> Optional[WhatsAppInstance]:
        """Find the configured, project-linked Evolution API instance."""
        settings = db.query(ProjectVerificationSettings).filter(
            ProjectVerificationSettings.project_id == project_id,
        ).first()
        if not settings or not settings.whatsapp_instance_id:
            return None
        return db.query(WhatsAppInstance).filter(
            WhatsAppInstance.id == settings.whatsapp_instance_id,
            WhatsAppInstance.project_id == project_id,
            WhatsAppInstance.provider_type == "evolution_api",
            WhatsAppInstance.is_active == True,
        ).first()

    @staticmethod
    async def _get_instance_token(
        instance: WhatsAppInstance, db: Session,
    ) -> Optional[str]:
        """Decrypt the instance token."""
        if not instance.instance_key:
            return None
        try:
            import os
            import base64
            from cryptography.fernet import Fernet

            key = os.getenv("ENCRYPTION_KEY")
            if not key:
                return None
            fernet = Fernet(key.encode() if isinstance(key, str) else key)
            return fernet.decrypt(instance.instance_key.encode()).decode()
        except Exception:
            return None
