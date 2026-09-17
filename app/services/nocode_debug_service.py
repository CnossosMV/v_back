"""
NoCode Debug Service — stores and retrieves debug events from Chrome Extension.
"""
from datetime import datetime, timedelta
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models import NoCodeDebugEvent


class NoCodeDebugService:

    def __init__(self, db: Session):
        self.db = db

    def store_event(
        self, project_id: int, session_id: str, event_data: dict,
    ) -> NoCodeDebugEvent:
        """Insert a debug event."""
        event = NoCodeDebugEvent(
            project_id=project_id,
            session_id=session_id,
            mapping_uid=event_data.get("mapping_uid"),
            event_name=event_data.get("event_name"),
            payload=event_data.get("payload"),
            vef_resolution=event_data.get("vef_resolution"),
            status=event_data.get("status", "received"),
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event

    def get_events(
        self, session_id: str, since: Optional[datetime] = None,
    ) -> List[NoCodeDebugEvent]:
        """Get debug events for a session, optionally since a timestamp."""
        q = self.db.query(NoCodeDebugEvent).filter(
            NoCodeDebugEvent.session_id == session_id,
        )
        if since:
            q = q.filter(NoCodeDebugEvent.created_at > since)
        return q.order_by(NoCodeDebugEvent.created_at.asc()).limit(200).all()

    def cleanup_expired(self) -> int:
        """Delete debug events older than 1 hour."""
        cutoff = datetime.utcnow() - timedelta(hours=1)
        count = self.db.query(NoCodeDebugEvent).filter(
            NoCodeDebugEvent.created_at < cutoff,
        ).delete()
        self.db.commit()
        return count
