"""
SMS Channel Adapter — wraps TwilioService.
"""
import logging
import re
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.services.channels.base import (
    ChannelAdapter, ChannelConstraints, OutboundContent, SendResult,
)

logger = logging.getLogger(__name__)


class SmsAdapter(ChannelAdapter):

    @property
    def channel_name(self) -> str:
        return "sms"

    async def send(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        content: OutboundContent,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        from app.services.twilio_service import twilio_service

        provider = self._resolve_provider(db, project_id, instance_config)
        if not provider:
            return SendResult(success=False, error="No SMS provider configured")

        try:
            credentials = self._decrypt_credentials(provider)
            # Ensure E.164 format (Twilio requires + prefix)
            to_number = recipient.strip()
            if to_number and not to_number.startswith("+"):
                to_number = f"+{to_number}"
            result = await twilio_service.send_sms(
                to=to_number,
                body=content.text or "",
                credentials=credentials,
            )
            return SendResult(
                success=result.get("success", False),
                provider_message_id=result.get("message_sid"),
                error=result.get("error"),
                provider_response=result,
            )
        except Exception as e:
            logger.error(f"SMS send error: {e}", exc_info=True)
            return SendResult(success=False, error=str(e))

    def check_constraints(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> ChannelConstraints:
        if not recipient or not re.match(r"^\+?\d{7,15}$", recipient.replace(" ", "")):
            return ChannelConstraints(available=False, reason="Invalid phone number for SMS")

        provider = self._resolve_provider(db, project_id, instance_config)
        if not provider:
            return ChannelConstraints(available=False, reason="No SMS provider configured")

        return ChannelConstraints(available=True)

    def negotiate_content(
        self,
        content: OutboundContent,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> OutboundContent:
        # Strip HTML
        if content.html and not content.text:
            content.text = re.sub(r"<[^>]+>", "", content.html)

        # Truncate to 160 chars
        if content.text and len(content.text) > 160:
            content.text = content.text[:157] + "..."

        # Discard media/buttons (not supported)
        content.media_url = None
        content.media_type = None
        content.media_caption = None
        content.buttons = None
        content.html = None
        content.content_type = "text"

        return content

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_provider(
        db: Session,
        project_id: int,
        instance_config: Optional[Dict[str, Any]] = None,
    ):
        from app.models import MessagingProvider

        if instance_config and instance_config.get("provider_id"):
            return db.query(MessagingProvider).filter(
                MessagingProvider.id == instance_config["provider_id"],
            ).first()

        return db.query(MessagingProvider).filter(
            MessagingProvider.project_id == project_id,
            MessagingProvider.provider_type == "twilio_sms",
        ).first()

    @staticmethod
    def _decrypt_credentials(provider) -> Dict[str, Any]:
        import json
        import os
        from cryptography.fernet import Fernet

        raw = provider.credentials_encrypted
        if not raw:
            return {}

        enc_key = os.getenv("ENCRYPTION_KEY")
        if enc_key:
            try:
                f = Fernet(enc_key.encode())
                decrypted = f.decrypt(raw.encode()).decode()
                return json.loads(decrypted)
            except Exception:
                pass

        try:
            return json.loads(raw)
        except Exception:
            return {}
