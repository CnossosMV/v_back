"""Record an already-completed external or compatibility interaction.

Provider delivery must use :class:`SendService`. This helper exists for
out-of-band touches and narrowly-scoped historical compatibility records; it
must never become an automation delivery shortcut.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import SendLog
from app.services.channels.lanes import (
    AttentionParticipation,
    require_source_contract,
)
from app.services.channels.source_contract import resolve_dispatch_source

logger = logging.getLogger(__name__)


def record_direct_send(
    db: Session,
    project_id: Optional[int],
    channel: str,
    recipient: str,
    content_summary: str,
    source_type: str,
    source_id: Optional[int] = None,
    status: str = "sent",
    provider_message_id: Optional[str] = None,
    instance_id: Optional[int] = None,
    template_id: Optional[int] = None,
    content_payload: Optional[dict] = None,
    error_message: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Optional[SendLog]:
    """Record an interaction that occurred outside ``SendService``.

    Creates a SendLog row with timestamps and a minimal decision_trace.
    Flushes but does NOT commit — caller is responsible for committing.

    Returns the SendLog or None if project_id is missing.
    """
    if not project_id:
        logger.debug("record_direct_send skipped: no project_id for %s/%s", source_type, channel)
        return None

    contract = require_source_contract(source_type)
    if contract.attention_participation != AttentionParticipation.FORBIDDEN:
        raise ValueError(
            f"Automated source {source_type} cannot use record_direct_send; use SendService"
        )
    resolution = resolve_dispatch_source(
        db, project_id, source_type, source_id,
        require_runtime_modes=False,
    )

    now = datetime.utcnow()
    log = SendLog(
        project_id=project_id,
        user_id=user_id,
        channel=channel,
        recipient=recipient[:255] if recipient else "",
        content_type="text",
        content_summary=(content_summary[:500] if content_summary else None),
        content_payload=content_payload,
        template_id=template_id,
        source_type=source_type,
        source_id=source_id,
        **resolution.intent_fields,
        decision_trace=[{"step": "direct_send", "bypass_reason": source_type, "ts": now.isoformat()}],
        status=status,
        provider_message_id=provider_message_id,
        error_message=error_message,
        queued_at=now,
        instance_id=instance_id,
    )

    if status == "failed":
        log.failed_at = now
    else:
        log.sent_at = now

    try:
        db.add(log)
        db.flush()
    except Exception:
        logger.exception("Failed to record direct send log")
        return None

    return log
