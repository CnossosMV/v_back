"""
Funnel Scheduler — background worker for processing wait steps.

Polls every 60 seconds for enrollments on wait steps whose duration has elapsed,
then advances them through the funnel.
"""
import logging
import asyncio
from typing import Optional

from app.database import SessionLocal

logger = logging.getLogger(__name__)


class FunnelSchedulerWorker:
    """Background worker that advances funnel enrollments past due wait steps."""

    def __init__(self, poll_interval: int = 60):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Funnel scheduler worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Funnel scheduler worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Funnel scheduler worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = self._process_cycle(db)
                    if processed > 0:
                        logger.info(f"Funnel scheduler processed {processed} enrollments")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Error in funnel scheduler: {e}", exc_info=True)

            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> int:
        """Run one scheduler cycle with advisory lock to prevent multi-worker duplication."""
        # pg_try_advisory_lock returns True if lock acquired, False if another worker holds it
        LOCK_ID = 737001  # unique ID for funnel scheduler
        acquired = db.execute(
            __import__('sqlalchemy').text(f"SELECT pg_try_advisory_lock({LOCK_ID})")
        ).scalar()
        if not acquired:
            return 0

        try:
            from app.services.funnel_engine import FunnelEngine
            engine = FunnelEngine(db)

            total = 0
            total += engine.process_wait_steps()
            total += engine.process_condition_steps()
            total += engine.process_wait_for_reply_steps()
            total += engine.process_wait_until_steps()
            total += engine.process_forked_wait_steps()
            total += engine.process_send_handoff_steps()
            total += engine.process_segment_triggers()
            total += engine.check_global_exit_rules()
            total += engine.process_paused_enrollment_expiry()
            return total
        finally:
            db.execute(
                __import__('sqlalchemy').text(f"SELECT pg_advisory_unlock({LOCK_ID})")
            )


# Singleton instance
funnel_scheduler_worker = FunnelSchedulerWorker()
