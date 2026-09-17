"""
Messenger + Instagram Channel Adapters — send DMs via the Meta Graph API
using per-project MetaPageConnection page tokens.

Both channels share one connection row (a Facebook Page); the Instagram
adapter requires the page's linked professional IG account.
"""
import logging
import re
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import MetaPageConnection
from app.services.channels.base import (
    ChannelAdapter, ChannelConstraints, OutboundContent, SendResult,
)

logger = logging.getLogger(__name__)

# Messenger/IG attachment types (document → file)
_MEDIA_TYPE_MAP = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "document": "file",
    "file": "file",
}


class _MetaMessagingAdapterBase(ChannelAdapter):
    """Shared implementation for Messenger and Instagram DM sends."""

    platform: str = ""  # "messenger" | "instagram"
    max_text_length: int = 2000

    async def send(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        content: OutboundContent,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        from app.services.meta_messaging_service import (
            meta_messaging_service, mark_connection_error,
        )
        from app.services.meta_window_service import MetaWindowService

        connection = self._resolve_connection(db, project_id, instance_config)
        if not connection:
            return SendResult(success=False, error=f"No active {self.platform} connection found")

        page_token = self._page_token(connection)
        if not page_token:
            return SendResult(
                success=False,
                error="Connection has no page access token",
                instance_id=connection.id,
            )

        window_open = MetaWindowService(db).is_window_open(
            connection.id, self.platform, recipient
        )

        # Outside the 24h window only HUMAN_AGENT-tagged sends (human replies,
        # up to 7 days, requires app-review feature) can go out.
        messaging_type = "RESPONSE"
        tag = None
        if not window_open:
            if content.metadata.get("human_agent"):
                messaging_type = "MESSAGE_TAG"
                tag = "HUMAN_AGENT"
            else:
                return SendResult(
                    success=False,
                    error="24h messaging window closed for this contact",
                    window_closed=True,
                    instance_id=connection.id,
                )

        attachment = None
        text = content.text
        if content.content_type == "media" and content.media_url:
            att_type = _MEDIA_TYPE_MAP.get(content.media_type or "image", "file")
            attachment = {
                "type": att_type,
                "payload": {"url": content.media_url, "is_reusable": True},
            }
            # Messenger/IG don't support captions on attachments — caption
            # (or text) goes as a separate follow-up-free text field only if
            # no attachment; send caption as the text of a second message.
            text = None

        try:
            result = await meta_messaging_service.send_message(
                page_id=connection.page_id,
                page_token=page_token,
                recipient_id=recipient,
                text=text,
                attachment=attachment,
                messaging_type=messaging_type,
                tag=tag,
            )
            # Follow-up text message for media captions
            caption = content.media_caption or (content.text if attachment else None)
            if result.get("success") and attachment and caption:
                await meta_messaging_service.send_message(
                    page_id=connection.page_id,
                    page_token=page_token,
                    recipient_id=recipient,
                    text=caption,
                    messaging_type=messaging_type,
                    tag=tag,
                )
        except Exception as e:
            logger.error(f"{self.platform} send error: {e}", exc_info=True)
            return SendResult(success=False, error=str(e), instance_id=connection.id)

        if not result.get("success"):
            mark_connection_error(db, connection, result)
            error_code = result.get("error_code")
            # Graph error 10 / subcode 2018278: outside allowed messaging window
            window_closed = error_code == 10
            return SendResult(
                success=False,
                error=result.get("error"),
                error_code=str(error_code) if error_code is not None else None,
                window_closed=window_closed,
                provider_response=result,
                instance_id=connection.id,
            )

        return SendResult(
            success=True,
            provider_message_id=result.get("message_id"),
            provider_response=result,
            instance_id=connection.id,
        )

    def check_constraints(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> ChannelConstraints:
        from app.services.meta_window_service import MetaWindowService

        connection = self._resolve_connection(db, project_id, instance_config)
        if not connection:
            return ChannelConstraints(
                available=False, reason=f"No active {self.platform} connection"
            )
        if connection.status != "connected":
            return ChannelConstraints(
                available=False, reason=f"Connection status: {connection.status}"
            )

        window_open = MetaWindowService(db).is_window_open(
            connection.id, self.platform, recipient
        )
        return ChannelConstraints(available=True, session_window_open=window_open)

    def negotiate_content(
        self,
        content: OutboundContent,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> OutboundContent:
        limit = self.max_text_length

        # Strip HTML for rich content
        if content.content_type == "rich" and content.html and not content.text:
            content.text = re.sub(r"<[^>]+>", "", content.html)
            content.content_type = "text"

        # Templates are WhatsApp-only — downgrade to text
        if content.content_type == "template":
            content.content_type = "text"

        if content.text and len(content.text) > limit:
            content.text = content.text[: limit - 3] + "..."

        content = self._negotiate_buttons(content)
        return content

    def _negotiate_buttons(self, content: OutboundContent) -> OutboundContent:
        # Instagram: no buttons — always degrade to a text list.
        # Messenger: max 3 quick-actions; SendService has no button send path
        # for this channel yet, so degrade to text list uniformly.
        if content.buttons:
            btn_text = "\n".join(
                f"- {b.get('text', b.get('title', ''))}" for b in content.buttons
            )
            content.text = ((content.text or "") + "\n\n" + btn_text).strip()
            if len(content.text) > self.max_text_length:
                content.text = content.text[: self.max_text_length - 3] + "..."
            content.buttons = None
        return content

    # ── helpers ──────────────────────────────────────────────────────────

    def _resolve_connection(
        self,
        db: Session,
        project_id: int,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[MetaPageConnection]:
        platform_filter = (
            MetaPageConnection.messenger_enabled == True
            if self.platform == "messenger"
            else MetaPageConnection.instagram_enabled == True
        )

        if instance_config and instance_config.get("instance_id"):
            conn = db.query(MetaPageConnection).filter(
                MetaPageConnection.id == instance_config["instance_id"],
                MetaPageConnection.project_id == project_id,
                MetaPageConnection.is_active == True,
                platform_filter,
            ).first()
            if not conn:
                logger.warning(
                    f"{self.platform} send: explicit instance_id "
                    f"{instance_config['instance_id']} not found in project "
                    f"{project_id} (inactive, disabled, or wrong project)"
                )
            return conn

        return db.query(MetaPageConnection).filter(
            MetaPageConnection.project_id == project_id,
            MetaPageConnection.is_active == True,
            platform_filter,
        ).first()

    @staticmethod
    def _page_token(connection: MetaPageConnection) -> Optional[str]:
        from app.services.encryption_service import decrypt_value_or_none
        token = decrypt_value_or_none(connection.page_access_token_enc)
        if connection.page_access_token_enc and token is None:
            logger.error(f"Failed to decrypt page token for connection {connection.id}")
        return token


class MessengerAdapter(_MetaMessagingAdapterBase):
    platform = "messenger"
    max_text_length = 2000

    @property
    def channel_name(self) -> str:
        return "messenger"


class InstagramAdapter(_MetaMessagingAdapterBase):
    platform = "instagram"
    max_text_length = 1000

    @property
    def channel_name(self) -> str:
        return "instagram"
