"""
External touch — log an out-of-band interaction with a contact (phone call,
in-person visit, message from a personal phone, ...) so it is visible in the
contact's history AND, when counted as a message, charged against the same
pacing machinery as platform sends (guardian attention budget, contact caps,
channel cooldowns, priority windows via ContactLedger).

Deliberately NOT gated by pause/block/opt-out: this records something that
already happened outside the system — the primary use case is exactly the
operator who is handling a contact manually.
"""
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.messaging import MessagingUser, MessagingEvent
from app.schemas.messaging import EXTERNAL_TOUCH_CHANNEL_MAP

logger = logging.getLogger(__name__)

EXTERNAL_TOUCH_SOURCE_TYPE = "external_touch"
EXTERNAL_TOUCH_EVENT_NAME = "contact.external_touch"


class ExternalTouchService:
    def __init__(self, db: Session):
        self.db = db

    def log_touch(
        self,
        project_id: int,
        user_id: int,
        touch_type: str,
        note: str,
        occurred_at: Optional[datetime] = None,
        count_as_message: bool = True,
    ) -> Dict[str, Any]:
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
            MessagingUser.status == "active",
        ).first()
        if not user:
            raise ValueError(f"Contact {user_id} not found in project {project_id}")

        occurred_at = occurred_at or datetime.utcnow()
        channel = EXTERNAL_TOUCH_CHANNEL_MAP[touch_type]

        if channel in ("whatsapp", "sms", "phone"):
            recipient = user.phone_e164 or user.phone or ""
        elif channel == "email":
            recipient = user.email or ""
        else:
            recipient = user.external_id or ""

        # SendLog so the touch shows in the messages tab / message tracker.
        from app.services.channels.send_log_helper import record_direct_send
        log = record_direct_send(
            self.db,
            project_id=project_id,
            channel=channel,
            recipient=recipient,
            content_summary=note,
            source_type=EXTERNAL_TOUCH_SOURCE_TYPE,
            status="sent",
            user_id=user_id,
            content_payload={
                "note": note,
                "touch_type": touch_type,
                "counted_as_message": count_as_message,
            },
        )
        if log is None:
            raise ValueError("Failed to record external touch send log")
        # Helper stamps now(); honor the (possibly backdated) occurrence time.
        log.sent_at = occurred_at
        log.queued_at = occurred_at

        # Ledger row = what makes the touch count for guardian budget, caps,
        # cooldowns and priority windows. Skipped for pure notes.
        if count_as_message:
            from app.services.scoring.policy_service import PolicyService
            PolicyService(self.db).record_contact(
                project_id, user_id, channel,
                source=EXTERNAL_TOUCH_SOURCE_TYPE,
                source_id=log.id,
                sent_at=occurred_at,
            )

        # Timeline event. Distinct event_name (never channel.*.sent) so no
        # send-event automation fires accidentally; source is NOT
        # "send_service" and properties carry NO arm_id, keeping the touch
        # out of the journey/bandit intervention materializer.
        event = MessagingEvent(
            project_id=project_id,
            user_id=user_id,
            event_name=EXTERNAL_TOUCH_EVENT_NAME,
            source="operator",
            properties={
                "touch_type": touch_type,
                "channel": channel,
                "note": note,
                "counted_as_message": count_as_message,
                "send_log_id": log.id,
            },
            created_at=occurred_at,
        )
        self.db.add(event)
        self.db.flush()

        self.db.commit()
        logger.info(
            "[external-touch] contact %s (project %s) type=%s channel=%s counted=%s occurred=%s",
            user_id, project_id, touch_type, channel, count_as_message,
            occurred_at.isoformat(),
        )
        return {
            "send_log_id": log.id,
            "event_id": event.id,
            "channel": channel,
            "counted_as_message": count_as_message,
            "occurred_at": occurred_at,
        }
