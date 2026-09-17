"""Durable Project Import application worker for agent-triggered migrations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging

from app.database import SessionLocal
from app.models.project_import import ProjectImport
from app.services.project_import_service import ProjectImportService


logger = logging.getLogger(__name__)


class ProjectImportWorker:
    """Claim one approved import at a time and finish it outside the MCP call."""

    def __init__(self, poll_interval: int = 5, stale_after_minutes: int = 120):
        self.poll_interval = poll_interval
        self.stale_after_minutes = stale_after_minutes
        self._running = False
        self._task = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Project Import worker already running")
            return
        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Project Import worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Project Import worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                # A large bundle can take minutes. Keep the main event loop and
                # the other scheduler workers responsive while it is applied.
                await asyncio.to_thread(self._process_cycle, db_session_factory)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Project Import worker cycle failed")
            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db_session_factory=SessionLocal) -> bool:
        db = db_session_factory()
        try:
            stale_before = datetime.utcnow() - timedelta(minutes=self.stale_after_minutes)
            stale = (
                db.query(ProjectImport)
                .filter(
                    ProjectImport.status == "applying",
                    ProjectImport.applied_at < stale_before,
                )
                .all()
            )
            for row in stale:
                row.status = "queued"
                row.error_message = "Recovered after an interrupted Project Import worker"
            if stale:
                db.commit()

            row = (
                db.query(ProjectImport)
                .filter(ProjectImport.status == "queued")
                .order_by(ProjectImport.apply_requested_at, ProjectImport.created_at)
                .with_for_update(skip_locked=True)
                .first()
            )
            if not row:
                return False

            row.status = "applying"
            row.applied_at = datetime.utcnow()
            actor_user_id = row.applied_by_user_id
            import_id = row.id
            project_id = row.project_id
            db.commit()

            logger.info("Applying queued Project Import %s for project %s", import_id, project_id)
            service = ProjectImportService(db)
            claimed = service.get(project_id, import_id)
            service.apply(claimed, actor_user_id, already_claimed=True)
            logger.info("Completed queued Project Import %s", import_id)
            return True
        except Exception:
            db.rollback()
            logger.exception("Queued Project Import failed")
            return False
        finally:
            db.close()


project_import_worker = ProjectImportWorker()
