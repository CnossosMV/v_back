"""
Contact Lifecycle Events
Emits lifecycle events as MessagingEvents for webhook dispatch.
"""
from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session

from app.models.messaging import MessagingEvent, MessagingUser


class ContactLifecycleEmitter:
    """
    Emits contact lifecycle events into the MessagingEvent pipeline.
    Events: contact.created, contact.identified, contact.merged,
    contact.updated, contact.opted_out, contact.deleted, merge.suggested
    """

    def emit(self, db: Session, project_id: int, event_name: str,
             user_id: Optional[int] = None, properties: Optional[Dict[str, Any]] = None):
        """Emit a lifecycle event."""
        event = MessagingEvent(
            project_id=project_id,
            user_id=user_id,
            event_name=event_name,
            properties=properties or {},
            source="system"
        )
        db.add(event)
        db.commit()
        return event

    def contact_created(self, db: Session, project_id: int, user: MessagingUser,
                        created_via: str = "api"):
        return self.emit(db, project_id, "contact.created", user.id, {
            "external_id": user.external_id,
            "email": user.email,
            "created_via": created_via,
        })

    def contact_identified(self, db: Session, project_id: int, user: MessagingUser,
                           anonymous_id: Optional[str] = None):
        return self.emit(db, project_id, "contact.identified", user.id, {
            "external_id": user.external_id,
            "anonymous_id": anonymous_id,
        })

    def contact_merged(self, db: Session, project_id: int,
                       winner_id: int, loser_id: int, triggered_by: str):
        return self.emit(db, project_id, "contact.merged", winner_id, {
            "winner_id": winner_id,
            "loser_id": loser_id,
            "triggered_by": triggered_by,
        })

    def contact_updated(self, db: Session, project_id: int, user: MessagingUser,
                        updated_fields: list):
        return self.emit(db, project_id, "contact.updated", user.id, {
            "external_id": user.external_id,
            "updated_fields": updated_fields,
        })

    def contact_opted_out(self, db: Session, project_id: int, user: MessagingUser,
                          channel: str):
        return self.emit(db, project_id, "contact.opted_out", user.id, {
            "external_id": user.external_id,
            "channel": channel,
        })

    def contact_deleted(self, db: Session, project_id: int, user_id: int,
                        external_id: Optional[str] = None):
        return self.emit(db, project_id, "contact.deleted", None, {
            "deleted_user_id": user_id,
            "external_id": external_id,
        })

    def merge_suggested(self, db: Session, project_id: int,
                        contact_a_id: int, contact_b_id: int,
                        match_reason: str):
        return self.emit(db, project_id, "merge.suggested", None, {
            "contact_a_id": contact_a_id,
            "contact_b_id": contact_b_id,
            "match_reason": match_reason,
        })


# Singleton
lifecycle_emitter = ContactLifecycleEmitter()
