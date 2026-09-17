"""
Intervention backfill — creates MessagingEvent rows for historical SendLog entries
that predate the dual-write in SendService._emit_channel_event().
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.models.journey import JourneyBackfillJob
from app.models.messaging import MessagingEvent

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 90
BATCH_SIZE = 1000


def backfill_interventions(db: Session, project_id: int, job_id: int) -> int:
    """
    For each historical SendLog row (sent/delivered) without a matching
    messaging_event, create one with source='send_service'.
    Idempotent — existence check prevents duplicates.
    """
    job = db.query(JourneyBackfillJob).get(job_id)
    if not job:
        return 0

    job.status = "running"
    job.started_at = datetime.utcnow()
    db.commit()

    try:
        cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)

        # Find SendLog rows without a matching messaging_event
        sql = text("""
            SELECT sl.id, sl.project_id, sl.user_id, sl.channel, sl.template_id,
                   sl.source_type, sl.sent_at
            FROM send_logs sl
            WHERE sl.project_id = :pid
              AND sl.status IN ('sent', 'delivered')
              AND sl.sent_at >= :cutoff
              AND sl.user_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM messaging_events me
                  WHERE me.project_id = sl.project_id
                    AND me.user_id = sl.user_id
                    AND me.source = 'send_service'
                    AND me.created_at BETWEEN sl.sent_at - interval '2 seconds'
                                          AND sl.sent_at + interval '2 seconds'
              )
            ORDER BY sl.sent_at ASC
        """)

        rows = db.execute(sql, {"pid": project_id, "cutoff": cutoff}).fetchall()

        job.total_rows = len(rows)
        db.commit()

        created = 0
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            for row in batch:
                sl_id, pid, uid, channel, tpl_id, source_type, sent_at = row
                channel = channel or "unknown"
                arm_id = f"{channel}:{source_type or 'unknown'}:{tpl_id or 'none'}"

                event = MessagingEvent(
                    project_id=pid,
                    user_id=uid,
                    event_name=f"channel.{channel}.sent",
                    source="send_service",
                    properties={
                        "send_log_id": sl_id,
                        "channel": channel,
                        "template_id": tpl_id,
                        "source_type": source_type,
                        "arm_id": arm_id,
                        "backfilled": True,
                    },
                    created_at=sent_at,
                )
                db.add(event)
                created += 1

            db.commit()
            job.processed_rows = min(i + BATCH_SIZE, len(rows))
            db.commit()

        job.status = "completed"
        job.completed_at = datetime.utcnow()
        job.processed_rows = len(rows)
        db.commit()

        logger.info("Intervention backfill completed for project %d: %d events created",
                     project_id, created)
        return created

    except Exception as e:
        logger.error("Intervention backfill failed for project %d: %s", project_id, e)
        db.rollback()
        job.status = "failed"
        job.error_message = str(e)[:500]
        job.completed_at = datetime.utcnow()
        db.commit()
        return 0
