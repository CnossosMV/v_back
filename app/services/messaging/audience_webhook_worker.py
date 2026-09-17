"""Background worker for outgoing audience sync webhooks."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.services.messaging.audience_webhook_delivery import audience_webhook_delivery_service

logger = logging.getLogger(__name__)


class AudienceWebhookWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._session_factory: Optional[Callable[[], Session]] = None

    async def start(self, session_factory: Callable[[], Session]) -> None:
        if self._task and not self._task.done():
            return
        self._session_factory = session_factory
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="audience-webhook-worker")
        logger.info("Audience webhook worker started")

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Audience webhook worker stopped")

    async def _run(self) -> None:
        interval = int(os.getenv("AUDIENCE_WEBHOOK_INTERVAL_SECONDS", "10"))
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                self._process_cycle()
            except Exception:
                logger.exception("Audience webhook worker cycle failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _process_cycle(self) -> None:
        if not self._session_factory:
            return
        db = self._session_factory()
        try:
            audience_webhook_delivery_service.deliver_due(db)
        finally:
            db.close()


audience_webhook_worker = AudienceWebhookWorker()
