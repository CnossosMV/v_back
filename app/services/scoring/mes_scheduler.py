"""
MES Scheduler — background worker for periodic message effectiveness scoring.

Polls every 60 seconds for projects with stale MES records,
then batch-computes scores.
"""
import logging
import asyncio
from typing import Optional

from app.database import SessionLocal

logger = logging.getLogger(__name__)


class MESSchedulerWorker:
    """Background worker that periodically computes message effectiveness scores."""

    def __init__(self, poll_interval: int = 60):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("MES scheduler worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("MES scheduler worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MES scheduler worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = self._process_cycle(db)
                    if processed > 0:
                        logger.info(f"MES scheduler computed {processed} scores")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Error in MES scheduler: {e}")

            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> int:
        """Find projects with stale/unsettled MES records and batch-compute."""
        from app.models import MessageEffectivenessScore, MESConfig
        from app.services.scoring.mes_engine import (
            ALGORITHM_VERSION,
            MESEngine,
            TERMINAL_FAIL_STATUSES,
        )
        from sqlalchemy import func as sqlfunc, and_, or_

        # Find distinct project_ids with stale records
        project_ids = (
            db.query(MessageEffectivenessScore.project_id)
            .filter(MessageEffectivenessScore.stale == True)
            .distinct()
            .all()
        )

        # Also find projects that have SendLogs without MES records
        from app.models import SendLog
        from sqlalchemy.orm import aliased
        mes_alias = aliased(MessageEffectivenessScore)
        new_project_ids = (
            db.query(SendLog.project_id)
            .outerjoin(mes_alias, mes_alias.send_log_id == SendLog.id)
            .filter(
                SendLog.status.notin_(["queued"]),
                mes_alias.id.is_(None),
            )
            .distinct()
            .limit(20)
            .all()
        )

        # Also find projects with computed-but-unsettled scores whose window
        # has closed — they need one final "settle" recompute even if no new
        # event or status change ever marks them stale.
        settle_project_ids = (
            db.query(MessageEffectivenessScore.project_id)
            .join(SendLog, SendLog.id == MessageEffectivenessScore.send_log_id)
            .filter(
                MessageEffectivenessScore.settled == False,
                MessageEffectivenessScore.stale == False,
                MessageEffectivenessScore.computed_at.isnot(None),
                or_(
                    SendLog.status.in_(list(TERMINAL_FAIL_STATUSES)),
                    and_(
                        SendLog.sent_at.isnot(None),
                        SendLog.sent_at
                        <= sqlfunc.timezone("utc", sqlfunc.now())
                        - sqlfunc.make_interval(
                            0, 0, 0, 0,
                            MessageEffectivenessScore.attribution_window_h,
                        ),
                    ),
                ),
            )
            .distinct()
            .limit(20)
            .all()
        )

        # Algorithm upgrades are self-healing: settled rows from prior versions
        # are discovered and drained in batches without a one-off deployment job.
        outdated_project_ids = (
            db.query(MessageEffectivenessScore.project_id)
            .filter(MessageEffectivenessScore.algorithm_version != ALGORITHM_VERSION)
            .distinct()
            .limit(20)
            .all()
        )

        outdated_projects = {pid for (pid,) in outdated_project_ids}
        all_project_ids = set()
        for (pid,) in project_ids:
            all_project_ids.add(pid)
        for (pid,) in new_project_ids:
            all_project_ids.add(pid)
        for (pid,) in settle_project_ids:
            all_project_ids.add(pid)
        for (pid,) in outdated_project_ids:
            all_project_ids.add(pid)

        if not all_project_ids:
            return 0

        total_processed = 0
        for pid in all_project_ids:
            # Check if MES is enabled for this project
            config = db.query(MESConfig).filter(MESConfig.project_id == pid).first()
            if config and not config.enabled and pid not in outdated_projects:
                continue

            try:
                engine = MESEngine(db)
                count = engine.batch_compute(pid, limit=500)
                total_processed += count
            except Exception as e:
                logger.error(f"Error computing MES for project {pid}: {e}")

        return total_processed


# Singleton instance
mes_scheduler_worker = MESSchedulerWorker()
