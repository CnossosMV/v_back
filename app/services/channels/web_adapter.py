"""
Web Chat Channel Adapter — no-op sender.

Web chat messages are delivered via WebSocket/polling; the message is already
stored in chat_messages by the time the Send Layer is invoked.
"""
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.services.channels.base import (
    ChannelAdapter, ChannelConstraints, OutboundContent, SendResult,
)

logger = logging.getLogger(__name__)


class WebAdapter(ChannelAdapter):

    @property
    def channel_name(self) -> str:
        return "web"

    async def send(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        content: OutboundContent,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # No-op: message is already stored in chat_messages and delivered via
        # WebSocket/polling by the chat widget.
        return SendResult(success=True, provider_message_id=None)

    def check_constraints(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> ChannelConstraints:
        return ChannelConstraints(available=True)

    def negotiate_content(
        self,
        content: OutboundContent,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> OutboundContent:
        return content
