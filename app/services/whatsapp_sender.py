"""
WhatsApp Sender — Unified abstraction layer

Routes outbound messages to the correct provider (Evolution API or Meta Cloud API)
and enforces 24-h window rules for Meta.
"""

import logging
import os
from typing import Dict, Any, Optional, List

from sqlalchemy.orm import Session
from cryptography.fernet import Fernet

from app.models import WhatsAppInstance
from app.services.evolution_api_service import evolution_api_service
from app.services.meta_cloud_api_service import meta_cloud_api_service
from app.services.whatsapp_window_service import WhatsAppWindowService
from app.utils.phone import normalize_phone

logger = logging.getLogger(__name__)


def infer_media_type(url: str) -> str:
    """Infer WhatsApp media type from URL file extension."""
    lower = url.lower().split("?")[0]  # strip query params
    if any(lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return "image"
    if any(lower.endswith(ext) for ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx")):
        return "document"
    if any(lower.endswith(ext) for ext in (".mp4", ".avi", ".mov")):
        return "video"
    if any(lower.endswith(ext) for ext in (".mp3", ".ogg", ".wav", ".aac")):
        return "audio"
    return "image"


class WhatsAppSender:
    def __init__(self, db: Session):
        self.db = db
        self.window_service = WhatsAppWindowService(db)

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _decrypt(value: str) -> str:
        """Decrypt a Fernet-encrypted string."""
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            return value  # unencrypted fallback
        f = Fernet(key.encode())
        return f.decrypt(value.encode()).decode()

    def _get_instance_token(self, instance: WhatsAppInstance) -> Optional[str]:
        """Get the decrypted Evolution API instance token."""
        try:
            if not instance.instance_key:
                return None
            return self._decrypt(instance.instance_key)
        except Exception:
            # Fallback: use raw value if decryption fails (dev/demo)
            if instance.instance_key and len(instance.instance_key) > 32:
                return instance.instance_key
            return None

    def _get_meta_access_token(self, instance: WhatsAppInstance) -> Optional[str]:
        """Get the decrypted Meta access token."""
        if not instance.meta_access_token_enc:
            return None
        try:
            return self._decrypt(instance.meta_access_token_enc)
        except Exception as e:
            logger.error(f"Error decrypting Meta access token: {e}")
            return None

    @staticmethod
    def _normalize_phone(to_number: str, instance: WhatsAppInstance) -> str:
        """Canonical E.164 digits for *to_number* (see app.utils.phone).

        Only prepends ``default_country_code`` to local/national numbers; a
        number already in international format (leading "+"/"00") is left as-is
        so foreign recipients aren't mangled and match their inbound window key.
        """
        return normalize_phone(to_number, instance.default_country_code)

    def _check_window(self, instance: WhatsAppInstance, to_number: str) -> Optional[bool]:
        """Check 24h window state. Returns True/False for Meta, None for Evolution."""
        if instance.provider_type != "meta_cloud_api":
            return None  # Evolution API has no window concept
        clean = self._normalize_phone(to_number, instance)
        return self.window_service.is_window_open(instance.id, clean)

    # ── rate limiting (G14) ──────────────────────────────────────────────

    def check_rate_limit(
        self, project_id: int, contact_identifier: str, channel: str = "whatsapp",
        max_messages: int = 10, window_minutes: int = 5,
    ) -> bool:
        """Check if sending another message would exceed the rate limit.

        Returns True if rate-limited (should NOT send), False if OK to send.
        Also increments the counter for this window.
        """
        from datetime import datetime, timedelta
        from app.models import ContactRateWindow

        now = datetime.utcnow()
        window_start_cutoff = now - timedelta(minutes=window_minutes)

        # Find or create rate window
        window = self.db.query(ContactRateWindow).filter(
            ContactRateWindow.project_id == project_id,
            ContactRateWindow.contact_identifier == contact_identifier,
            ContactRateWindow.channel == channel,
            ContactRateWindow.window_start > window_start_cutoff,
        ).first()

        if window:
            if window.message_count >= max_messages:
                logger.warning(
                    f"Rate limit hit: {contact_identifier} on {channel} "
                    f"({window.message_count}/{max_messages} in {window_minutes}min)"
                )
                return True
            window.message_count += 1
            self.db.commit()
            return False

        # No active window — create one
        new_window = ContactRateWindow(
            project_id=project_id,
            contact_identifier=contact_identifier,
            channel=channel,
            window_start=now,
            message_count=1,
        )
        self.db.add(new_window)
        self.db.commit()
        return False

    # ── public API ──────────────────────────────────────────────────────

    async def send_message(
        self,
        instance: WhatsAppInstance,
        to_number: str,
        message: str,
        template_fallback: Optional[Dict[str, Any]] = None,
        quoted_message_key: Optional[Dict] = None,
        project_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Send a WhatsApp message through the correct provider.

        For Evolution API: direct send (no window restriction).
        For Meta Cloud API: checks 24-h window; falls back to template if closed.

        template_fallback example:
            {"template_name": "hello_world", "language": "en_US", "components": [...]}
        """
        # Rate limit check (G14)
        pid = project_id or (instance.workspace_id if hasattr(instance, 'workspace_id') else None)
        if pid:
            if self.check_rate_limit(pid, to_number, "whatsapp"):
                return {"success": False, "error": "rate_limited", "rate_limited": True}

        window_open = self._check_window(instance, to_number)

        if instance.provider_type == "evolution_api":
            result = await self._send_via_evolution(instance, to_number, message, quoted_message_key)
        elif instance.provider_type == "meta_cloud_api":
            result = await self._send_via_meta(instance, to_number, message, template_fallback)
        else:
            result = {"success": False, "error": f"Unknown provider_type: {instance.provider_type}"}

        result["window_open"] = window_open
        return result

    async def send_media_message(
        self,
        instance: WhatsAppInstance,
        to_number: str,
        media_url: str,
        media_type: str = "image",
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        template_fallback: Optional[Dict[str, Any]] = None,
        project_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Send a media message through the correct provider.

        For Evolution API: direct send.
        For Meta Cloud API: checks 24-h window; falls back to template if closed.
        """
        # Resolve relative URLs to absolute (providers fetch externally)
        public_base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_BASE_URL", "")
        if media_url and media_url.startswith("/"):
            media_url = f"{public_base}{media_url}"

        # Safety net: replace internal hostnames that can't be reached externally
        if media_url:
            for internal in ("http://customer-backend:8001", "http://localhost:8001"):
                if media_url.startswith(internal):
                    media_url = media_url.replace(internal, public_base, 1)
                    break

        # Rate limit check (G14)
        pid = project_id or (instance.workspace_id if hasattr(instance, 'workspace_id') else None)
        if pid:
            if self.check_rate_limit(pid, to_number, "whatsapp"):
                return {"success": False, "error": "rate_limited", "rate_limited": True}

        window_open = self._check_window(instance, to_number)

        if instance.provider_type == "evolution_api":
            result = await self._send_media_via_evolution(
                instance, to_number, media_url, media_type, caption, filename
            )
        elif instance.provider_type == "meta_cloud_api":
            result = await self._send_media_via_meta(
                instance, to_number, media_url, media_type, caption, filename, template_fallback
            )
        else:
            result = {"success": False, "error": f"Unknown provider_type: {instance.provider_type}"}

        result["window_open"] = window_open
        return result

    async def send_template_message(
        self,
        instance: WhatsAppInstance,
        to_number: str,
        template_name: str,
        language_code: str = "en_US",
        components: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Send a pre-approved WhatsApp template message through the correct provider.

        For Meta Cloud API: uses send_template_message directly.
        For Evolution API: sends the template name as a text message (Evolution manages templates).
        """
        window_open = self._check_window(instance, to_number)

        if instance.provider_type == "meta_cloud_api":
            access_token = self._get_meta_access_token(instance)
            phone_number_id = instance.meta_phone_number_id
            if not access_token or not phone_number_id:
                return {"success": False, "error": "Meta Cloud API credentials not configured", "window_open": window_open}
            clean_number = self._normalize_phone(to_number, instance)
            result = await meta_cloud_api_service.send_template_message(
                phone_number_id=phone_number_id,
                access_token=access_token,
                to=clean_number,
                template_name=template_name,
                language_code=language_code,
                components=components,
            )
        elif instance.provider_type == "evolution_api":
            # Evolution API: send template via their template endpoint or as text
            instance_token = self._get_instance_token(instance)
            if not instance_token:
                return {"success": False, "error": "Could not retrieve Evolution API instance token", "window_open": window_open}
            result = await evolution_api_service.send_message(
                instance_name=instance.instance_name,
                instance_token=instance_token,
                to_number=to_number,
                message=f"[Template: {template_name}]",
            )
        else:
            result = {"success": False, "error": f"Unknown provider_type: {instance.provider_type}"}

        result["window_open"] = window_open
        return result

    # ── Evolution API ───────────────────────────────────────────────────

    async def _send_via_evolution(
        self,
        instance: WhatsAppInstance,
        to_number: str,
        message: str,
        quoted_message_key: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        instance_token = self._get_instance_token(instance)
        if not instance_token:
            return {"success": False, "error": "Could not retrieve Evolution API instance token"}
        return await evolution_api_service.send_message(
            instance_name=instance.instance_name,
            instance_token=instance_token,
            to_number=to_number,
            message=message,
            quoted_message_key=quoted_message_key,
        )

    # ── Meta Cloud API ──────────────────────────────────────────────────

    async def _send_via_meta(
        self,
        instance: WhatsAppInstance,
        to_number: str,
        message: str,
        template_fallback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        access_token = self._get_meta_access_token(instance)
        phone_number_id = instance.meta_phone_number_id

        if not access_token or not phone_number_id:
            return {"success": False, "error": "Meta Cloud API credentials not configured"}

        clean_number = self._normalize_phone(to_number, instance)

        window_open = self.window_service.is_window_open(instance.id, clean_number)

        if window_open:
            result = await meta_cloud_api_service.send_text_message(
                phone_number_id=phone_number_id,
                access_token=access_token,
                to=clean_number,
                text=message,
            )
            return result

        # Window closed — try template fallback
        if template_fallback:
            result = await meta_cloud_api_service.send_template_message(
                phone_number_id=phone_number_id,
                access_token=access_token,
                to=clean_number,
                template_name=template_fallback["template_name"],
                language_code=template_fallback.get("language", "en_US"),
                components=template_fallback.get("components"),
            )
            return result

        return {
            "success": False,
            "window_closed": True,
            "error": "24-hour messaging window is closed and no template fallback provided",
        }

    # ── Media helpers ────────────────────────────────────────────────────

    async def _send_media_via_evolution(
        self,
        instance: WhatsAppInstance,
        to_number: str,
        media_url: str,
        media_type: str,
        caption: Optional[str],
        filename: Optional[str],
    ) -> Dict[str, Any]:
        instance_token = self._get_instance_token(instance)
        if not instance_token:
            return {"success": False, "error": "Could not retrieve Evolution API instance token"}
        return await evolution_api_service.send_media_message(
            instance_name=instance.instance_name,
            instance_token=instance_token,
            to_number=to_number,
            media_url=media_url,
            media_type=media_type,
            caption=caption,
            filename=filename,
        )

    async def _send_media_via_meta(
        self,
        instance: WhatsAppInstance,
        to_number: str,
        media_url: str,
        media_type: str,
        caption: Optional[str],
        filename: Optional[str],
        template_fallback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        access_token = self._get_meta_access_token(instance)
        phone_number_id = instance.meta_phone_number_id

        if not access_token or not phone_number_id:
            return {"success": False, "error": "Meta Cloud API credentials not configured"}

        clean_number = self._normalize_phone(to_number, instance)
        window_open = self.window_service.is_window_open(instance.id, clean_number)

        if window_open:
            return await meta_cloud_api_service.send_media_message(
                phone_number_id=phone_number_id,
                access_token=access_token,
                to=clean_number,
                media_url=media_url,
                media_type=media_type,
                caption=caption,
                filename=filename,
            )

        if template_fallback:
            return await meta_cloud_api_service.send_template_message(
                phone_number_id=phone_number_id,
                access_token=access_token,
                to=clean_number,
                template_name=template_fallback["template_name"],
                language_code=template_fallback.get("language", "en_US"),
                components=template_fallback.get("components"),
            )

        return {
            "success": False,
            "window_closed": True,
            "error": "24-hour messaging window is closed and no template fallback provided",
        }
