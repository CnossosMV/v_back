"""
Meta Messaging 24-Hour Window Tracker

Tracks the 24-hour customer-service messaging window for Messenger and
Instagram DMs (per connection + platform + PSID/IGSID). Mirrors
whatsapp_window_service.py.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import MetaMessagingWindow

logger = logging.getLogger(__name__)


class MetaWindowService:
    def __init__(self, db: Session):
        self.db = db

    def update_window(self, connection_id: int, platform: str, contact_id: str) -> MetaMessagingWindow:
        """Upsert a contact window — sets expires_at to now + 24 h."""
        now = datetime.utcnow()
        expires = now + timedelta(hours=24)

        window = (
            self.db.query(MetaMessagingWindow)
            .filter(
                and_(
                    MetaMessagingWindow.connection_id == connection_id,
                    MetaMessagingWindow.platform == platform,
                    MetaMessagingWindow.contact_id == contact_id,
                )
            )
            .first()
        )
        if window:
            window.window_opens_at = now
            window.window_expires_at = expires
            window.updated_at = now
        else:
            window = MetaMessagingWindow(
                connection_id=connection_id,
                platform=platform,
                contact_id=contact_id,
                window_opens_at=now,
                window_expires_at=expires,
            )
            self.db.add(window)

        self.db.commit()
        self.db.refresh(window)
        return window

    def is_window_open(self, connection_id: int, platform: str, contact_id: str) -> bool:
        """Return True if a valid 24-h window exists for this contact."""
        now = datetime.utcnow()
        return (
            self.db.query(MetaMessagingWindow)
            .filter(
                and_(
                    MetaMessagingWindow.connection_id == connection_id,
                    MetaMessagingWindow.platform == platform,
                    MetaMessagingWindow.contact_id == contact_id,
                    MetaMessagingWindow.window_expires_at > now,
                )
            )
            .first()
            is not None
        )

    def get_window(self, connection_id: int, platform: str, contact_id: str) -> Optional[MetaMessagingWindow]:
        """Return the contact window row (or None)."""
        return (
            self.db.query(MetaMessagingWindow)
            .filter(
                and_(
                    MetaMessagingWindow.connection_id == connection_id,
                    MetaMessagingWindow.platform == platform,
                    MetaMessagingWindow.contact_id == contact_id,
                )
            )
            .first()
        )

    def cleanup_expired(self, max_age_hours: int = 48) -> int:
        """Delete windows older than *max_age_hours*. Returns count deleted."""
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        count = (
            self.db.query(MetaMessagingWindow)
            .filter(MetaMessagingWindow.window_expires_at < cutoff)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return count
