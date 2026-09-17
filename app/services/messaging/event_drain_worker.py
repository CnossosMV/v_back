"""
Event Drain Worker — background consumer for system-emitted MessagingEvents.

Events ingested at the public/webhook endpoints are processed inline and
marked processed=True. But events emitted *internally* — most importantly
`segment_changed` from SegmentEngine, and `contact.unsubscribed` from the
one-click unsubscribe route — are written with processed=False and had no
consumer: nothing ran them through EventProcessor, so funnel triggers and
event actions keyed on those events never fired.

This worker drains them: poll for processed=False events and run each
through EventProcessor.process_event (which marks processed + commits, or
rolls back on error leaving the row for a later retry).

Watermark: persisted in worker_state (key "event_drain_watermark") and
advanced after each drained batch, so a restart resumes where the worker
left off — events written during a short downtime are drained, and their
downstream effects (MES stale-marking, funnel triggers) still fire.
The resume lookback is capped at MAX_CATCHUP_HOURS: replaying a huge
historical backlog as live sends is explicitly out of scope.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 738003  # unique ID for the event drain worker

WATERMARK_KEY = "event_drain_watermark"

# Never resume more than this far back — protects against replaying a huge
# stale backlog (e.g. after the watermark row was deleted or very long downtime).
MAX_CATCHUP_HOURS = 24


class EventDrainWorker:
    """Background worker that drains internally-emitted unprocessed events."""

    def __init__(self, poll_interval: int = 15, batch_size: int = 100):
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._watermark: Optional[datetime] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Event drain worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._watermark = self._load_watermark(factory)
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info(
            "Event drain worker started (poll=%ds, batch=%d, watermark=%s)",
            self.poll_interval, self.batch_size, self._watermark.isoformat(),
        )

    def _load_watermark(self, db_session_factory) -> datetime:
        """Resume from the persisted watermark, capped at MAX_CATCHUP_HOURS back."""
        now = datetime.utcnow()
        floor = now - timedelta(hours=MAX_CATCHUP_HOURS)
        try:
            db = db_session_factory()
            try:
                from app.models import WorkerState
                row = db.query(WorkerState).filter(WorkerState.key == WATERMARK_KEY).first()
                if row and row.value and row.value.get("watermark"):
                    persisted = datetime.fromisoformat(row.value["watermark"])
                    return max(persisted, floor)
            finally:
                db.close()
        except Exception as e:
            logger.warning("Could not load drain watermark (starting from now): %s", e)
        return now

    def _persist_watermark(self, db) -> None:
        try:
            from app.models import WorkerState
            row = db.query(WorkerState).filter(WorkerState.key == WATERMARK_KEY).first()
            value = {"watermark": self._watermark.isoformat()}
            if row:
                row.value = value
            else:
                db.add(WorkerState(key=WATERMARK_KEY, value=value))
            db.commit()
        except Exception as e:
            logger.warning("Could not persist drain watermark: %s", e)
            db.rollback()

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Event drain worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = await self._process_cycle(db)
                    if processed > 0:
                        logger.info("Event drain worker processed %d events", processed)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Error in event drain worker: %s", e)

            await asyncio.sleep(self.poll_interval)

    async def _process_cycle(self, db) -> int:
        """Run one drain cycle with advisory lock (single runner across processes)."""
        acquired = db.execute(
            text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")
        ).scalar()
        if not acquired:
            return 0

        try:
            from app.models.messaging import MessagingEvent
            from app.services.messaging.event_processor import event_processor

            events = db.query(MessagingEvent).filter(
                MessagingEvent.processed == False,  # noqa: E712
                MessagingEvent.processing_mode == "live",
                MessagingEvent.created_at >= self._watermark,
            ).order_by(
                MessagingEvent.created_at.asc(),
            ).limit(self.batch_size).with_for_update(skip_locked=True).all()

            if not events:
                return 0

            processed = 0
            max_seen = None
            earliest_failed = None
            for event in events:
                event_created = event.created_at
                try:
                    # process_event marks processed=True + commits, or rolls
                    # back on error (row stays for a later retry).
                    await event_processor.process_event(db, event)
                    processed += 1
                    if max_seen is None or event_created > max_seen:
                        max_seen = event_created
                except Exception as e:
                    logger.error("Failed to drain event %d: %s", event.id, e)
                    db.rollback()
                    if earliest_failed is None or event_created < earliest_failed:
                        earliest_failed = event_created

            # Advance + persist the watermark. On failures, hold it at the
            # earliest failed event so the row is retried next cycle
            # (created_at >= watermark keeps it in scope).
            candidate = earliest_failed if earliest_failed is not None else max_seen
            if candidate is not None and candidate > self._watermark:
                self._watermark = candidate
                self._persist_watermark(db)

            return processed
        finally:
            db.execute(
                text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})")
            )


# Module-level singleton, started/stopped from app startup (see app/main.py).
event_drain_worker = EventDrainWorker()
