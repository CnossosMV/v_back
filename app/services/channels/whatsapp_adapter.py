"""
WhatsApp Channel Adapter — wraps existing WhatsAppSender.
"""
import logging
import re
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import WhatsAppInstance
from app.services.channels.base import (
    ChannelAdapter, ChannelConstraints, OutboundContent, SendResult,
)

logger = logging.getLogger(__name__)


class WhatsAppAdapter(ChannelAdapter):

    @property
    def channel_name(self) -> str:
        return "whatsapp"

    async def send(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        content: OutboundContent,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        from app.services.whatsapp_sender import WhatsAppSender

        instance = self._resolve_instance(db, project_id, instance_config)
        if not instance:
            return SendResult(success=False, error="No active WhatsApp instance found")

        sender = WhatsAppSender(db)

        try:
            if content.content_type == "template" and content.template_name:
                result = await sender.send_template_message(
                    instance=instance,
                    to_number=recipient,
                    template_name=content.template_name,
                    language_code=content.template_language or "en_US",
                    components=content.template_components,
                )
            elif content.content_type == "media" and content.media_url:
                result = await sender.send_media_message(
                    instance=instance,
                    to_number=recipient,
                    media_url=content.media_url,
                    media_type=content.media_type or "image",
                    caption=content.media_caption,
                    project_id=project_id,
                )
            else:
                # text or rich (downgraded to text)
                template_fallback = None
                if content.template_name:
                    template_fallback = {
                        "template_name": content.template_name,
                        "language": content.template_language or "en_US",
                        "components": content.template_components,
                    }
                result = await sender.send_message(
                    instance=instance,
                    to_number=recipient,
                    message=content.text or "",
                    template_fallback=template_fallback,
                    project_id=project_id,
                )
        except Exception as e:
            logger.error(f"WhatsApp send error: {e}", exc_info=True)
            return SendResult(success=False, error=str(e), instance_id=instance.id if instance else None)

        # Map WhatsAppSender response to SendResult
        provider_msg_id = result.get("message_id") or result.get("messages", [{}])[0].get("id") if isinstance(result.get("messages"), list) else result.get("message_id")
        if not provider_msg_id and isinstance(result.get("key"), dict):
            provider_msg_id = result["key"].get("id")

        return SendResult(
            success=result.get("success", False),
            provider_message_id=provider_msg_id,
            error=result.get("error"),
            rate_limited=result.get("rate_limited", False),
            window_closed=result.get("window_closed", False),
            provider_response=result,
            instance_id=instance.id,
        )

    def check_constraints(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> ChannelConstraints:
        instance = self._resolve_instance(db, project_id, instance_config)
        if not instance:
            return ChannelConstraints(available=False, reason="No active WhatsApp instance")

        if instance.connection_status not in ("connected", "open"):
            return ChannelConstraints(available=False, reason=f"Instance status: {instance.connection_status}")

        # Check 24h window for Meta
        window_open = None
        if instance.provider_type == "meta_cloud_api":
            from app.services.whatsapp_window_service import WhatsAppWindowService
            from app.utils.phone import normalize_phone
            ws = WhatsAppWindowService(db)
            clean = normalize_phone(recipient, instance.default_country_code)
            window_open = ws.is_window_open(instance.id, clean)

        return ChannelConstraints(
            available=True,
            session_window_open=window_open,
        )

    def negotiate_content(
        self,
        content: OutboundContent,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> OutboundContent:
        # Truncate text to 4096
        if content.text and len(content.text) > 4096:
            content.text = content.text[:4093] + "..."

        # Strip HTML
        if content.content_type == "rich" and content.html and not content.text:
            content.text = re.sub(r"<[^>]+>", "", content.html)
            if len(content.text) > 4096:
                content.text = content.text[:4093] + "..."
            content.content_type = "text"

        # Degrade buttons > 3 to text list
        if content.buttons and len(content.buttons) > 3:
            btn_text = "\n".join(
                f"- {b.get('text', b.get('title', ''))}" for b in content.buttons
            )
            content.text = (content.text or "") + "\n\n" + btn_text
            content.buttons = None

        return content

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_instance(
        db: Session,
        project_id: int,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[WhatsAppInstance]:
        """Find the WhatsApp instance to use.

        Every resolution validates project ownership: an explicit instance_id
        must belong to the sending project (the NULL-project + same-workspace
        arm is a transition allowance for legacy Evolution rows only)."""
        from app.models import Project
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return None

        legacy_arm = (
            (WhatsAppInstance.project_id.is_(None))
            & (WhatsAppInstance.workspace_id == project.workspace_id)
            & (WhatsAppInstance.provider_type == "evolution_api")
        )

        if instance_config and instance_config.get("instance_id"):
            instance = db.query(WhatsAppInstance).filter(
                WhatsAppInstance.id == instance_config["instance_id"],
                WhatsAppInstance.is_active == True,
                (WhatsAppInstance.project_id == project_id) | legacy_arm,
            ).first()
            if not instance:
                logger.warning(
                    f"WhatsApp send: explicit instance_id "
                    f"{instance_config['instance_id']} not usable for project "
                    f"{project_id} (inactive or belongs to another project)"
                )
            elif instance.project_id is None:
                logger.warning(
                    f"WhatsApp send: legacy NULL-project instance {instance.id} "
                    f"used by project {project_id} — assign it to a project"
                )
            return instance

        # Fallback: prefer the project's own instance, then legacy
        # workspace-scoped NULL-project Evolution rows
        instance = db.query(WhatsAppInstance).filter(
            WhatsAppInstance.project_id == project_id,
            WhatsAppInstance.is_active == True,
        ).first()
        if instance:
            return instance
        instance = db.query(WhatsAppInstance).filter(
            legacy_arm,
            WhatsAppInstance.is_active == True,
        ).first()
        if instance:
            logger.warning(
                f"WhatsApp send: fallback to legacy NULL-project instance "
                f"{instance.id} for project {project_id} — assign it to a project"
            )
        return instance
