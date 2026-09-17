"""
WhatsApp 24-Hour Window Tracker

Tracks the 24-hour customer-service messaging window required by the
Meta Cloud API.  Evolution API instances skip window checks entirely.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import WhatsAppContactWindow

logger = logging.getLogger(__name__)


class WhatsAppWindowService:
    def __init__(self, db: Session):
        self.db = db

    def update_window(self, instance_id: int, contact_phone: str) -> WhatsAppContactWindow:
        """Upsert a contact window — sets expires_at to now + 24 h."""
        now = datetime.utcnow()
        expires = now + timedelta(hours=24)

        window = (
            self.db.query(WhatsAppContactWindow)
            .filter(
                and_(
                    WhatsAppContactWindow.instance_id == instance_id,
                    WhatsAppContactWindow.contact_phone == contact_phone,
                )
            )
            .first()
        )
        if window:
            window.window_opens_at = now
            window.window_expires_at = expires
            window.updated_at = now
        else:
            window = WhatsAppContactWindow(
                instance_id=instance_id,
                contact_phone=contact_phone,
                window_opens_at=now,
                window_expires_at=expires,
            )
            self.db.add(window)

        self.db.commit()
        self.db.refresh(window)
        return window

    def is_window_open(self, instance_id: int, contact_phone: str) -> bool:
        """Return True if a valid 24-h window exists for this contact."""
        now = datetime.utcnow()
        return (
            self.db.query(WhatsAppContactWindow)
            .filter(
                and_(
                    WhatsAppContactWindow.instance_id == instance_id,
                    WhatsAppContactWindow.contact_phone == contact_phone,
                    WhatsAppContactWindow.window_expires_at > now,
                )
            )
            .first()
            is not None
        )

    def get_window(self, instance_id: int, contact_phone: str) -> Optional[WhatsAppContactWindow]:
        """Return the contact window row (or None)."""
        return (
            self.db.query(WhatsAppContactWindow)
            .filter(
                and_(
                    WhatsAppContactWindow.instance_id == instance_id,
                    WhatsAppContactWindow.contact_phone == contact_phone,
                )
            )
            .first()
        )

    def cleanup_expired(self, max_age_hours: int = 48) -> int:
        """Delete windows older than *max_age_hours*. Returns count deleted."""
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        count = (
            self.db.query(WhatsAppContactWindow)
            .filter(WhatsAppContactWindow.window_expires_at < cutoff)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return count
