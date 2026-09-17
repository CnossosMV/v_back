"""Background worker for auto-refresh audience syncs."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models.messaging import MessagingAudience, MessagingAudienceDestination
from app.services.messaging.audience_sync import audience_sync_service

logger = logging.getLogger(__name__)


class AudienceSyncWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._session_factory: Optional[Callable[[], Session]] = None

    async def start(self, session_factory: Callable[[], Session]) -> None:
        if self._task and not self._task.done():
            return
        self._session_factory = session_factory
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="audience-sync-worker")
        logger.info("Audience sync worker started")

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Audience sync worker stopped")

    async def _run(self) -> None:
        interval = int(os.getenv("AUDIENCE_SYNC_INTERVAL_SECONDS", "300"))
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                self._process_cycle()
            except Exception:
                logger.exception("Audience sync worker cycle failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _due_since(self, refresh_mode: str) -> datetime:
        if refresh_mode == "daily":
            return datetime.utcnow() - timedelta(hours=24)
        return datetime.utcnow() - timedelta(hours=1)

    def _process_cycle(self) -> None:
        if not self._session_factory:
            return
        db = self._session_factory()
        try:
            audiences = db.query(MessagingAudience).filter(
                MessagingAudience.status == "active",
                MessagingAudience.refresh_mode.in_(["hourly", "daily"]),
            ).limit(50).all()
            for audience in audiences:
                due_since = self._due_since(audience.refresh_mode)
                if audience.last_evaluated_at and audience.last_evaluated_at > due_since:
                    continue
                destinations = db.query(MessagingAudienceDestination).filter(
                    MessagingAudienceDestination.project_id == audience.project_id,
                    MessagingAudienceDestination.audience_id == audience.id,
                    MessagingAudienceDestination.is_active == True,
                ).all()
                if not destinations:
                    audience_sync_service.evaluate_memberships(db, audience)
                    continue
                for destination in destinations:
                    try:
                        audience_sync_service.run_sync(db, audience, destination, dry_run=False)
                    except Exception:
                        logger.exception("Audience auto-sync failed for audience=%s destination=%s", audience.id, destination.id)
        finally:
            db.close()


audience_sync_worker = AudienceSyncWorker()
