"""
Scoring Scheduler — background worker for periodic score recalculation.

Polls every 120 seconds for active score definitions due for recalculation,
then batch-refreshes features and recalculates scores.
"""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from app.database import SessionLocal

logger = logging.getLogger(__name__)


class ScoringSchedulerWorker:
    """Background worker that periodically recalculates scores."""

    def __init__(self, poll_interval: int = 120):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Scoring scheduler worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Scoring scheduler worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scoring scheduler worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = self._process_cycle(db)
                    if processed > 0:
                        logger.info(f"Scoring scheduler recalculated {processed} definitions")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Error in scoring scheduler: {e}")

            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> int:
        """Find active definitions due for periodic recalc and batch recalculate."""
        from app.models import ScoreDefinition
        from app.services.scoring.scoring_engine import ScoringEngine
        from app.services.scoring.feature_store_service import FeatureStoreService

        now = datetime.utcnow()

        definitions = db.query(ScoreDefinition).filter(
            ScoreDefinition.status == "active",
            ScoreDefinition.recalc_interval_minutes > 0,
        ).all()

        processed = 0
        refreshed_projects = set()

        for defn in definitions:
            # Check if due for recalculation
            if defn.last_recalc_at:
                next_due = defn.last_recalc_at + timedelta(minutes=defn.recalc_interval_minutes)
                if now < next_due:
                    continue

            # Refresh feature store window counts once per project
            if defn.project_id not in refreshed_projects:
                try:
                    fs = FeatureStoreService(db)
                    fs.batch_refresh_project(defn.project_id)
                    refreshed_projects.add(defn.project_id)
                except Exception as e:
                    logger.error(f"Error refreshing features for project {defn.project_id}: {e}")

            # Batch recalculate
            try:
                engine = ScoringEngine(db)
                engine.batch_recalculate(defn.id)
                processed += 1
            except Exception as e:
                logger.error(f"Error batch recalculating definition {defn.id}: {e}")

        return processed


# Singleton instance
scoring_scheduler_worker = ScoringSchedulerWorker()
