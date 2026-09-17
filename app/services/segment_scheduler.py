"""
Segment Scheduler — background worker for periodic batch evaluation.

Polls every 600 seconds (10 min) for projects with active segment rules,
then batch-evaluates all contacts per project.
"""
import logging
import asyncio
from typing import Optional

from app.database import SessionLocal

logger = logging.getLogger(__name__)


class SegmentSchedulerWorker:
    """Background worker that periodically re-evaluates segment membership."""

    def __init__(self, poll_interval: int = 600):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Segment scheduler worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Segment scheduler worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Segment scheduler worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = self._process_cycle(db)
                    if processed > 0:
                        logger.info(f"Segment scheduler evaluated {processed} projects")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Error in segment scheduler: {e}")

            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> int:
        """Find projects with active segment rules and batch-evaluate."""
        from app.models import SegmentRule
        from app.services.segment_engine import SegmentEngine

        project_ids = (
            db.query(SegmentRule.project_id)
            .filter(SegmentRule.is_active == True)
            .distinct()
            .all()
        )
        project_ids = [pid[0] for pid in project_ids]

        if not project_ids:
            return 0

        engine = SegmentEngine(db)
        for pid in project_ids:
            try:
                engine.batch_evaluate_project(pid)
            except Exception as e:
                logger.error(f"Error evaluating segments for project {pid}: {e}")

        return len(project_ids)


# Singleton instance
segment_scheduler_worker = SegmentSchedulerWorker()
