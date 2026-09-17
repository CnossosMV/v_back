"""
HandlerChannelLinkService — CRUD for handler-to-channel linkages.

Replaces scattered whatsapp_instance_id FKs on chatbots/agent_teams with a
generic, multi-channel link table.
"""

import logging
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import HandlerChannelLink

logger = logging.getLogger(__name__)


class HandlerChannelLinkService:
    def __init__(self, db: Session):
        self.db = db

    def get_links(self, handler_type: str, handler_id: int) -> List[HandlerChannelLink]:
        return (
            self.db.query(HandlerChannelLink)
            .filter(
                HandlerChannelLink.handler_type == handler_type,
                HandlerChannelLink.handler_id == handler_id,
            )
            .all()
        )

    def get_link(
        self, handler_type: str, handler_id: int, channel: str
    ) -> Optional[HandlerChannelLink]:
        return (
            self.db.query(HandlerChannelLink)
            .filter(
                HandlerChannelLink.handler_type == handler_type,
                HandlerChannelLink.handler_id == handler_id,
                HandlerChannelLink.channel == channel,
            )
            .first()
        )

    def set_link(
        self,
        handler_type: str,
        handler_id: int,
        channel: str,
        instance_id: Optional[int] = None,
        config: Optional[Dict] = None,
        is_primary: bool = True,
    ) -> HandlerChannelLink:
        """Upsert a handler-channel link."""
        link = self.get_link(handler_type, handler_id, channel)
        if link:
            link.instance_id = instance_id
            link.config = config
            link.is_primary = is_primary
        else:
            link = HandlerChannelLink(
                handler_type=handler_type,
                handler_id=handler_id,
                channel=channel,
                instance_id=instance_id,
                config=config,
                is_primary=is_primary,
            )
            self.db.add(link)
        self.db.commit()
        self.db.refresh(link)
        return link

    def remove_link(self, handler_type: str, handler_id: int, channel: str) -> bool:
        link = self.get_link(handler_type, handler_id, channel)
        if not link:
            return False
        self.db.delete(link)
        self.db.commit()
        return True

    def find_handler_by_instance(
        self, channel: str, instance_id: int
    ) -> Optional[Dict]:
        """Find a handler linked to a specific channel instance.

        Returns dict with handler_type and handler_id, or None.
        """
        link = (
            self.db.query(HandlerChannelLink)
            .filter(
                HandlerChannelLink.channel == channel,
                HandlerChannelLink.instance_id == instance_id,
            )
            .first()
        )
        if not link:
            return None
        return {"handler_type": link.handler_type, "handler_id": link.handler_id}

    def find_handlers_by_instance(
        self, channel: str, instance_id: int
    ) -> List[Dict]:
        """Find all handlers linked to a specific channel instance."""
        links = (
            self.db.query(HandlerChannelLink)
            .filter(
                HandlerChannelLink.channel == channel,
                HandlerChannelLink.instance_id == instance_id,
            )
            .all()
        )
        return [
            {"handler_type": l.handler_type, "handler_id": l.handler_id}
            for l in links
        ]

    def remove_by_instance(self, channel: str, instance_id: int) -> int:
        """Remove all links for a given channel instance. Returns count deleted."""
        count = (
            self.db.query(HandlerChannelLink)
            .filter(
                HandlerChannelLink.channel == channel,
                HandlerChannelLink.instance_id == instance_id,
            )
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return count
