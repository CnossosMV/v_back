"""
Channel Registry — central adapter registry with capability caching.
"""
import logging
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.channels.base import ChannelAdapter

logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Thread-safe registry of channel adapters with DB-backed capability cache."""

    _adapters: Dict[str, ChannelAdapter] = {}
    _capabilities_cache: Dict[str, dict] = {}

    @classmethod
    def register(cls, adapter: ChannelAdapter) -> None:
        cls._adapters[adapter.channel_name] = adapter
        logger.info(f"Registered channel adapter: {adapter.channel_name}")

    @classmethod
    def get_adapter(cls, channel: str) -> Optional[ChannelAdapter]:
        return cls._adapters.get(channel)

    @classmethod
    def get_capabilities(cls, db: Session, channel: str) -> Optional[dict]:
        if channel in cls._capabilities_cache:
            return cls._capabilities_cache[channel]

        from app.models import ChannelCapability

        cap = db.query(ChannelCapability).filter(
            ChannelCapability.channel == channel,
        ).first()
        if cap:
            result = {
                "channel": cap.channel,
                "display_name": cap.display_name,
                "max_text_length": cap.max_text_length,
                "supports_media": cap.supports_media,
                "supported_media_types": cap.supported_media_types,
                "max_media_size_mb": cap.max_media_size_mb,
                "supports_buttons": cap.supports_buttons,
                "max_buttons": cap.max_buttons,
                "supports_templates": cap.supports_templates,
                "supports_rich_text": cap.supports_rich_text,
                "supports_reactions": cap.supports_reactions,
                "has_session_window": cap.has_session_window,
                "session_window_hours": cap.session_window_hours,
                "requires_opt_in": cap.requires_opt_in,
                "supports_read_receipts": cap.supports_read_receipts,
                "supported_statuses": cap.supported_statuses,
                "icon_hint": cap.icon_hint,
            }
            cls._capabilities_cache[channel] = result
            return result
        return None

    @classmethod
    def available_channels(cls) -> List[str]:
        return list(cls._adapters.keys())

    @classmethod
    def clear_cache(cls) -> None:
        cls._capabilities_cache.clear()


def init_channel_registry() -> None:
    """Register all built-in channel adapters. Call once at app startup."""
    from app.services.channels.whatsapp_adapter import WhatsAppAdapter
    from app.services.channels.email_adapter import EmailAdapter
    from app.services.channels.sms_adapter import SmsAdapter
    from app.services.channels.web_adapter import WebAdapter
    from app.services.channels.meta_messaging_adapter import MessengerAdapter, InstagramAdapter

    ChannelRegistry.register(WhatsAppAdapter())
    ChannelRegistry.register(EmailAdapter())
    ChannelRegistry.register(SmsAdapter())
    ChannelRegistry.register(WebAdapter())
    ChannelRegistry.register(MessengerAdapter())
    ChannelRegistry.register(InstagramAdapter())

    logger.info(
        f"Channel registry initialised with {len(ChannelRegistry.available_channels())} adapters: "
        f"{ChannelRegistry.available_channels()}"
    )
