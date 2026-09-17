"""
Routing State Service — manages ContactRoutingState records.

Multi-row model:
- Non-funnel handlers (chatbot, agent_team, human, idle): single row per contact+channel
- Funnel handlers (funnel_wait, funnel_whatsapp): one row per enrollment_id

Provides fast lookup of which handler(s) currently own a conversation for a given contact+channel.
"""

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models import ContactRoutingState

logger = logging.getLogger(__name__)

# Priority map for handler types
HANDLER_PRIORITIES = {
    "idle": 0,
    "chatbot": 10,
    "agent_team": 50,
    "funnel_wait": 80,
    "funnel_send": 80,
    "human": 100,
}

FUNNEL_HANDLER_TYPES = {"funnel_wait", "funnel_send"}


class RoutingStateService:
    """CRUD + priority-aware updates for ContactRoutingState (multi-row)."""

    def __init__(self, db: Session):
        self.db = db

    # ── Read ──────────────────────────────────────────────────────────

    def get_state(
        self,
        project_id: int,
        identifier: str,
        channel: str,
    ) -> Optional[ContactRoutingState]:
        """Return the highest-priority active routing state for a contact.

        Backward-compatible single-state API.  Auto-deletes expired rows.
        """
        states = self.get_all_states(project_id, identifier, channel)
        if not states:
            return None
        # states are sorted priority desc — first is highest
        return states[0]

    def get_all_states(
        self,
        project_id: int,
        identifier: str,
        channel: str,
    ) -> List[ContactRoutingState]:
        """Return ALL active routing state rows, sorted by priority descending.

        Expired rows are auto-deleted.
        """
        rows = (
            self.db.query(ContactRoutingState)
            .filter(
                ContactRoutingState.project_id == project_id,
                ContactRoutingState.contact_identifier == identifier,
                ContactRoutingState.channel == channel,
            )
            .order_by(ContactRoutingState.handler_priority.desc())
            .all()
        )

        now = datetime.utcnow()
        active = []
        dirty = False

        for row in rows:
            if row.expires_at and row.expires_at < now:
                self.db.delete(row)
                dirty = True
                continue
            if row.handler_type == "idle":
                # Clean up stale idle rows
                self.db.delete(row)
                dirty = True
                continue
            active.append(row)

        if dirty:
            self.db.commit()

        return active

    # ── Write ─────────────────────────────────────────────────────────

    def set_state(
        self,
        project_id: int,
        identifier: str,
        channel: str,
        handler_type: str,
        handler_id: Optional[int] = None,
        session_id: Optional[int] = None,
        enrollment_id: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        metadata: Optional[dict] = None,
    ) -> ContactRoutingState:
        """Create or upsert routing state (ignores priority).

        For funnel types (funnel_wait, funnel_whatsapp):
            Upsert by (project_id, identifier, channel, enrollment_id).
        For non-funnel types:
            Upsert by (project_id, identifier, channel) where handler_type NOT IN funnel types.
        """
        priority = HANDLER_PRIORITIES.get(handler_type, 0)

        if handler_type in FUNNEL_HANDLER_TYPES and enrollment_id is not None:
            # Funnel row — upsert by enrollment
            state = self.db.query(ContactRoutingState).filter(
                ContactRoutingState.project_id == project_id,
                ContactRoutingState.contact_identifier == identifier,
                ContactRoutingState.channel == channel,
                ContactRoutingState.enrollment_id == enrollment_id,
            ).first()
        else:
            # Non-funnel row — upsert by contact+channel excluding funnel rows
            state = self.db.query(ContactRoutingState).filter(
                ContactRoutingState.project_id == project_id,
                ContactRoutingState.contact_identifier == identifier,
                ContactRoutingState.channel == channel,
                ContactRoutingState.handler_type.notin_(FUNNEL_HANDLER_TYPES),
            ).first()

        if state:
            state.handler_type = handler_type
            state.handler_id = handler_id
            state.handler_priority = priority
            state.session_id = session_id
            state.enrollment_id = enrollment_id
            state.assigned_at = datetime.utcnow()
            state.expires_at = expires_at
            if metadata is not None:
                state.routing_metadata = metadata
        else:
            state = ContactRoutingState(
                project_id=project_id,
                contact_identifier=identifier,
                channel=channel,
                handler_type=handler_type,
                handler_id=handler_id,
                handler_priority=priority,
                session_id=session_id,
                enrollment_id=enrollment_id,
                expires_at=expires_at,
                routing_metadata=metadata,
            )
            self.db.add(state)

        self.db.commit()
        self.db.refresh(state)
        return state

    def set_handler(
        self,
        project_id: int,
        identifier: str,
        channel: str,
        handler_type: str,
        handler_id: Optional[int] = None,
        session_id: Optional[int] = None,
        expires_at: Optional[datetime] = None,
    ) -> Optional[ContactRoutingState]:
        """Set non-funnel handler only if new priority >= max current priority.

        Funnel handlers are managed separately via set_state with enrollment_id.
        """
        new_priority = HANDLER_PRIORITIES.get(handler_type, 0)

        # Compare against max priority across ALL rows
        all_states = self.get_all_states(project_id, identifier, channel)
        if all_states:
            max_priority = max(s.handler_priority for s in all_states)
            if new_priority < max_priority:
                logger.debug(
                    f"Skipping handler override: {handler_type}({new_priority}) < "
                    f"current max({max_priority})"
                )
                return all_states[0]  # Return highest priority state

        return self.set_state(
            project_id=project_id,
            identifier=identifier,
            channel=channel,
            handler_type=handler_type,
            handler_id=handler_id,
            session_id=session_id,
        )

    def update_inbound_context(
        self,
        project_id: int,
        identifier: str,
        channel: str,
        inbound_context: dict,
    ) -> int:
        """Merge inbound channel context into routing_metadata on ALL active rows.

        Preserves any handler-specific metadata already present.
        """
        rows = (
            self.db.query(ContactRoutingState)
            .filter(
                ContactRoutingState.project_id == project_id,
                ContactRoutingState.contact_identifier == identifier,
                ContactRoutingState.channel == channel,
            )
            .all()
        )
        updated = 0
        for row in rows:
            meta = dict(row.routing_metadata or {})
            meta.update(inbound_context)
            row.routing_metadata = meta
            updated += 1
        if updated:
            self.db.commit()
        return updated

    # ── Delete ────────────────────────────────────────────────────────

    def clear_state(
        self,
        project_id: int,
        identifier: str,
        channel: str,
        enrollment_id: Optional[int] = None,
    ) -> None:
        """Delete a specific routing state row.

        If enrollment_id is provided: delete that specific funnel row.
        If not: delete the non-funnel row.
        """
        if enrollment_id is not None:
            # Delete specific funnel row
            row = self.db.query(ContactRoutingState).filter(
                ContactRoutingState.project_id == project_id,
                ContactRoutingState.contact_identifier == identifier,
                ContactRoutingState.channel == channel,
                ContactRoutingState.enrollment_id == enrollment_id,
            ).first()
        else:
            # Delete non-funnel row
            row = self.db.query(ContactRoutingState).filter(
                ContactRoutingState.project_id == project_id,
                ContactRoutingState.contact_identifier == identifier,
                ContactRoutingState.channel == channel,
                ContactRoutingState.handler_type.notin_(FUNNEL_HANDLER_TYPES),
            ).first()

        if row:
            self.db.delete(row)
            self.db.commit()

    def clear_all_states(
        self,
        project_id: int,
        identifier: str,
        channel: str,
    ) -> int:
        """Delete ALL routing state rows for this contact+channel.

        Returns the number of rows deleted.
        """
        count = self.db.query(ContactRoutingState).filter(
            ContactRoutingState.project_id == project_id,
            ContactRoutingState.contact_identifier == identifier,
            ContactRoutingState.channel == channel,
        ).delete(synchronize_session="fetch")
        self.db.commit()
        return count
