"""
Meta OAuth Cleanup Worker — purges credentials from stale OAuth sessions.

MetaOAuthSession rows may hold an encrypted long-lived user token while a
connect flow is in progress. This worker guarantees the token never outlives
the flow: every cycle it

1. Sanitizes terminal or expired sessions (completed / error / cancelled /
   past expires_at) that still carry `user_token_enc`, nulling the token and
   marking still-open sessions as `expired`.
2. Deletes session rows more than 7 days past `expires_at`.

Idempotent — re-running a cycle on an already-clean table is a no-op.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.database import SessionLocal

logger = logging.getLogger(__name__)

SESSION_RETENTION_DAYS = 7
TERMINAL_STATUSES = ("completed", "error", "cancelled", "expired")


class MetaOAuthCleanupWorker:
    """Background worker that sanitizes and purges Meta OAuth sessions."""

    def __init__(self, poll_interval: int = 600):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Meta OAuth cleanup worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Meta OAuth cleanup worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Meta OAuth cleanup worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    sanitized, deleted = self._process_cycle(db)
                    if sanitized or deleted:
                        logger.info(
                            f"Meta OAuth cleanup: sanitized {sanitized}, deleted {deleted} session(s)"
                        )
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Meta OAuth cleanup worker error: {e}", exc_info=True)

            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db: Session) -> tuple:
        """One cleanup cycle. Returns (sanitized_count, deleted_count)."""
        from app.models import MetaOAuthSession

        now = datetime.utcnow()

        # 1) Null tokens on terminal or expired sessions
        stale = (
            db.query(MetaOAuthSession)
            .filter(
                and_(
                    MetaOAuthSession.user_token_enc.isnot(None),
                    or_(
                        MetaOAuthSession.status.in_(TERMINAL_STATUSES),
                        MetaOAuthSession.expires_at < now,
                    ),
                )
            )
            .all()
        )
        for session in stale:
            session.user_token_enc = None
            if session.status not in TERMINAL_STATUSES:
                session.status = "expired"
        sanitized = len(stale)
        if sanitized:
            db.commit()

        # 2) Delete long-dead rows
        cutoff = now - timedelta(days=SESSION_RETENTION_DAYS)
        deleted = (
            db.query(MetaOAuthSession)
            .filter(MetaOAuthSession.expires_at < cutoff)
            .delete(synchronize_session=False)
        )
        if deleted:
            db.commit()

        return sanitized, deleted


# Singleton instance
meta_oauth_cleanup_worker = MetaOAuthCleanupWorker(poll_interval=600)
