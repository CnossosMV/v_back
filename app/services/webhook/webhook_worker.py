"""
Webhook Worker — background worker for processing queued webhook ingests.

Polls every 5 seconds for queued/retryable WebhookIngest records and
processes up to 50 per cycle via WebhookProcessor.
"""
import logging
import asyncio
from datetime import datetime
from typing import Optional

from sqlalchemy import text, or_

from app.database import SessionLocal

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 738001  # unique ID for webhook worker


class WebhookWorker:
    """Background worker that processes queued webhook ingests."""

    def __init__(self, poll_interval: int = 5, batch_size: int = 50):
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Webhook worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Webhook worker started (poll=%ds, batch=%d)", self.poll_interval, self.batch_size)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Webhook worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = self._process_cycle(db)
                    if processed > 0:
                        logger.info("Webhook worker processed %d ingests", processed)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Error in webhook worker: %s", e)

            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> int:
        """Run one processing cycle with advisory lock to prevent duplication."""
        acquired = db.execute(
            text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")
        ).scalar()
        if not acquired:
            return 0

        try:
            from app.models import WebhookIngest
            from app.services.webhook.webhook_processor import WebhookProcessor

            now = datetime.utcnow()

            # Fetch queued records: status=queued AND (next_retry_at IS NULL OR next_retry_at <= now)
            ingests = db.query(WebhookIngest).filter(
                WebhookIngest.processing_status == "queued",
                or_(
                    WebhookIngest.next_retry_at.is_(None),
                    WebhookIngest.next_retry_at <= now,
                ),
            ).order_by(
                WebhookIngest.received_at.asc()
            ).limit(self.batch_size).all()

            if not ingests:
                return 0

            processor = WebhookProcessor(db)
            processed = 0
            for ingest in ingests:
                try:
                    processor.process_ingest(ingest)
                    processed += 1
                except Exception as e:
                    logger.error("Failed to process ingest %d: %s", ingest.id, e)
                    processed += 1  # Count attempted

            return processed
        finally:
            db.execute(
                text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})")
            )


# Singleton instance
webhook_worker = WebhookWorker()
