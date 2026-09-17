"""
Contact automation pause — temporarily suppress automated messages for a
contact while an operator handles them manually.

Pause is orthogonal to block: block drops inbound and prevents all contact;
pause only suppresses automated outbound (funnels, event actions, templates,
chatbot/agent-team replies). Manual sends and inbound processing continue.

Modes:
- hold: freeze in place. Parked SendLogs stay queued (the scheduled send
  worker excludes them while paused) and funnel enrollments stop advancing
  (the scheduler processors filter paused contacts). On resume everything
  continues where it stopped.
- skip: destructive. Active funnel enrollments are exited and parked
  automation SendLogs are marked skipped. On resume nothing catches up.
"""
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import or_, desc
from sqlalchemy.orm import Session

from app.models.messaging import MessagingUser

logger = logging.getLogger(__name__)

PAUSE_MODES = ("hold", "skip")

# source_types whose sends are considered automation (mirrors the Lane
# taxonomy in app/services/channels/lanes.py: PROMOTIONAL + CONVERSATIONAL).
AUTOMATION_SOURCE_TYPES = (
    "template", "funnel", "event_action", "campaign",
    "chatbot", "agent_team", "support",
)

# SendLog statuses that represent a parked/not-yet-dispatched send.
PARKED_SEND_STATUSES = ("delayed", "deferred", "candidate")


class ContactPauseService:
    def __init__(self, db: Session):
        self.db = db

    def _get_contact(self, project_id: int, user_id: int) -> MessagingUser:
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()
        if not user:
            raise ValueError(f"Contact {user_id} not found in project {project_id}")
        return user

    def pause_contact(
        self,
        project_id: int,
        user_id: int,
        mode: str,
        reason: Optional[str] = None,
    ) -> MessagingUser:
        """Pause automations for a contact. Idempotent if already paused."""
        if mode not in PAUSE_MODES:
            raise ValueError(f"Invalid pause mode '{mode}' (expected one of {PAUSE_MODES})")

        user = self._get_contact(project_id, user_id)
        if user.automations_paused:
            return user

        user.automations_paused = True
        user.automations_paused_at = datetime.utcnow()
        user.automations_paused_reason = reason
        user.automations_pause_mode = mode

        if mode == "skip":
            self._skip_sweep(project_id, user)

        self.db.commit()
        self.db.refresh(user)
        logger.info(
            "[pause] contact %s (project %s) automations paused mode=%s reason=%r",
            user_id, project_id, mode, reason,
        )
        return user

    def _skip_sweep(self, project_id: int, user: MessagingUser) -> None:
        """skip mode: exit active funnel enrollments + skip parked automation sends."""
        from app.models import FunnelEnrollment, SendLog, ContactRoutingState
        from app.services.funnel_engine import FunnelEngine

        # 1) Exit active enrollments via the engine's terminal-transition path
        #    (logging, exit tags, exit events, pending-send supersession).
        enrollments = self.db.query(FunnelEnrollment).filter(
            FunnelEnrollment.user_id == user.id,
            FunnelEnrollment.status == "active",
        ).all()
        if enrollments:
            engine = FunnelEngine(self.db)
            for enrollment in enrollments:
                engine._exit_enrollment(enrollment, "automations_paused_skip")
                self.db.query(ContactRoutingState).filter(
                    ContactRoutingState.enrollment_id == enrollment.id,
                ).delete(synchronize_session=False)

        # 2) Mark remaining parked automation sends as skipped. Status-guarded
        #    UPDATE so it's atomic against the dispatch worker.
        now = datetime.utcnow()
        skipped = self.db.query(SendLog).filter(
            SendLog.project_id == project_id,
            SendLog.user_id == user.id,
            SendLog.status.in_(PARKED_SEND_STATUSES),
            SendLog.source_type.in_(AUTOMATION_SOURCE_TYPES),
        ).update({
            SendLog.status: "skipped",
            SendLog.failed_at: now,
            SendLog.error_message: "Automations paused (skip)",
        }, synchronize_session=False)
        logger.info(
            "[pause][skip] contact %s: exited %d enrollment(s), skipped %d parked send(s)",
            user.id, len(enrollments), skipped,
        )

    def resume_contact(self, project_id: int, user_id: int) -> MessagingUser:
        """Release a contact back to automations. Held sends/enrollments flow
        naturally on the next worker/scheduler cycles."""
        user = self._get_contact(project_id, user_id)
        if user.automations_paused:
            user.automations_paused = False
            user.automations_paused_at = None
            user.automations_paused_reason = None
            user.automations_pause_mode = None
            self.db.commit()
            self.db.refresh(user)
            logger.info("[pause] contact %s (project %s) automations resumed", user_id, project_id)
        return user

    def get_paused_contacts(
        self,
        project_id: int,
        page: int = 1,
        page_size: int = 50,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Paginated list of paused contacts (mirrors get_blocked_contacts)."""
        query = self.db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.automations_paused == True,  # noqa: E712
        )

        if search:
            search_filter = f"%{search}%"
            query = query.filter(
                or_(
                    MessagingUser.name.ilike(search_filter),
                    MessagingUser.external_id.ilike(search_filter),
                    MessagingUser.phone.ilike(search_filter),
                    MessagingUser.email.ilike(search_filter),
                )
            )

        total = query.count()
        total_pages = max(1, (total + page_size - 1) // page_size)
        contacts = query.order_by(desc(MessagingUser.automations_paused_at)).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        return {
            "contacts": [
                {
                    "id": c.id,
                    "project_id": c.project_id,
                    "external_id": c.external_id,
                    "name": c.name,
                    "phone": c.phone,
                    "email": c.email,
                    "automations_paused_at": c.automations_paused_at.isoformat() if c.automations_paused_at else None,
                    "automations_paused_reason": c.automations_paused_reason,
                    "automations_pause_mode": c.automations_pause_mode,
                }
                for c in contacts
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }


def is_contact_paused(db: Session, project_id: int, user_id: Optional[int]) -> Optional[MessagingUser]:
    """Cheap gate lookup: returns the contact if it is paused, else None."""
    if not user_id:
        return None
    return db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id,
        MessagingUser.automations_paused == True,  # noqa: E712
    ).first()
