"""Background planner for CampaignRecipe episodes; never dispatches sends."""

from __future__ import annotations

import asyncio
import logging

from app.database import SessionLocal
from app.services.campaigns.recipes import CampaignRecipeService


logger = logging.getLogger(__name__)


class CampaignRecipeWorker:
    def __init__(self, poll_interval: int = 300):
        self.poll_interval = poll_interval
        self._running = False
        self._task = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(db_session_factory or SessionLocal),
            name="campaign-recipe-worker",
        )
        logger.info("Campaign recipe worker started (poll=%ss)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Campaign recipe worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    result = CampaignRecipeService(db).sync_all()
                    if result["created"] or result["drafts_materialized"] or result["expired"]:
                        logger.info("Campaign recipe planning cycle: %s", result)
                finally:
                    db.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Campaign recipe planning cycle failed")
            await asyncio.sleep(self.poll_interval)


campaign_recipe_worker = CampaignRecipeWorker()
