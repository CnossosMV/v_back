"""
Session backfill — assigns synthetic session_ids to historical events
using a 30-minute inactivity window, partitioned by (project_id, user_id).
"""
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.models.journey import JourneyBackfillJob
from app.models.messaging import MessagingEvent

logger = logging.getLogger(__name__)

SESSION_GAP_SECONDS = 30 * 60  # 30 minutes
LOOKBACK_DAYS = 90
BATCH_SIZE = 5000


def backfill_sessions(db: Session, project_id: int, job_id: int) -> int:
    """
    Assign synthetic session_ids to historical events using 30-min inactivity window.
    Processes one project. Idempotent — skips rows where session_id IS NOT NULL.
    """
    job = db.query(JourneyBackfillJob).get(job_id)
    if not job:
        return 0

    job.status = "running"
    job.started_at = datetime.utcnow()
    db.commit()

    try:
        cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)

        # Get distinct user_ids with NULL session_id in the lookback window
        user_ids = [
            r[0] for r in db.execute(text("""
                SELECT DISTINCT user_id FROM messaging_events
                WHERE project_id = :pid
                  AND user_id IS NOT NULL
                  AND session_id IS NULL
                  AND created_at >= :cutoff
            """), {"pid": project_id, "cutoff": cutoff}).fetchall()
        ]

        job.total_rows = len(user_ids)
        db.commit()

        total_events_processed = 0

        for i, uid in enumerate(user_ids):
            # Fetch events for this user ordered by time
            events = db.execute(text("""
                SELECT id, created_at FROM messaging_events
                WHERE project_id = :pid
                  AND user_id = :uid
                  AND session_id IS NULL
                  AND created_at >= :cutoff
                ORDER BY created_at ASC
            """), {"pid": project_id, "uid": uid, "cutoff": cutoff}).fetchall()

            if not events:
                continue

            # Walk events and assign sessions
            updates = []  # list of (event_id, session_id)
            session_start = events[0][1]
            session_id = _deterministic_session_id(project_id, uid, session_start)
            prev_ts = events[0][1]

            for eid, ts in events:
                gap = (ts - prev_ts).total_seconds()
                if gap > SESSION_GAP_SECONDS:
                    session_start = ts
                    session_id = _deterministic_session_id(project_id, uid, session_start)
                updates.append((eid, session_id))
                prev_ts = ts

            # Batch update
            for batch_start in range(0, len(updates), BATCH_SIZE):
                batch = updates[batch_start:batch_start + BATCH_SIZE]
                # Build CASE expression
                cases = " ".join(
                    f"WHEN id = {eid} THEN '{sid}'" for eid, sid in batch
                )
                ids = ",".join(str(eid) for eid, _ in batch)
                db.execute(text(f"""
                    UPDATE messaging_events
                    SET session_id = CASE {cases} END
                    WHERE id IN ({ids})
                """))
                db.commit()

            total_events_processed += len(updates)

            # Update progress periodically
            if (i + 1) % 100 == 0:
                job.processed_rows = i + 1
                db.commit()

        job.processed_rows = len(user_ids)
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        db.commit()

        logger.info("Session backfill completed for project %d: %d users, %d events",
                     project_id, len(user_ids), total_events_processed)
        return total_events_processed

    except Exception as e:
        logger.error("Session backfill failed for project %d: %s", project_id, e)
        job.status = "failed"
        job.error_message = str(e)[:500]
        job.completed_at = datetime.utcnow()
        db.commit()
        return 0


def _deterministic_session_id(project_id: int, user_id: int, session_start: datetime) -> str:
    """Generate a deterministic session ID for idempotent backfill."""
    namespace = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
    name = f"{project_id}:{user_id}:{session_start.isoformat()}"
    return str(uuid.uuid5(namespace, name))
