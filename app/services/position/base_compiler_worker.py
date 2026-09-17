"""
Base compiler (Phase 3 engine) — compiles authored Base cells into standing
candidates per contact position.

For each active BaseCellMessage, find contacts whose computed position matches
the cell (Type exact-or-default, Stage/Age null = wildcard) and emit ONE
standing candidate (SendLog status='candidate') keyed on the cell's stable
slot_id — idempotent (NOT EXISTS guard: skip if a live candidate/deferred row
already exists for that contact+slot). The selection pass then arbitrates these
standing candidates against episode sends; when one is consumed or expires, the
next sweep re-emits it (hence "standing").

Set-based per project (never per-contact Python). Flag SEND_BASE_COMPILE
(off|shadow|enforce, default off): shadow logs the would-emit count, enforce
inserts. Additive — only creates candidate rows the selection/worker already
understand.
"""
import asyncio
import logging

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 739004
TTL_MINUTES = 60

# Match only the active lifecycle model. When wildcards overlap, one authored
# Base wins per contact/channel by specificity: exact Type > Stage > Age, then
# stable row id as the deterministic tie-break.
_RANKED = """
WITH ranked AS (
  SELECT bm.*, cp.user_id,
         CASE WHEN COALESCE(bm.channel, 'whatsapp') = 'email'
              THEN mu.email ELSE COALESCE(mu.phone_e164, mu.phone) END AS resolved_recipient,
         row_number() OVER (
           PARTITION BY cp.user_id, COALESCE(bm.channel, 'whatsapp')
           ORDER BY
             ((CASE WHEN bm.type = cp.type THEN 4 ELSE 0 END) +
              (CASE WHEN bm.stage IS NOT NULL THEN 2 ELSE 0 END) +
              (CASE WHEN bm.age_bucket IS NOT NULL THEN 1 ELSE 0 END)) DESC,
             bm.id ASC
         ) AS specificity_rank
  FROM lifecycle_models lm
  JOIN contact_positions cp
    ON cp.project_id = lm.project_id AND cp.lifecycle_model_id = lm.id
  JOIN base_cell_messages bm
    ON bm.project_id = lm.project_id AND bm.lifecycle_model_id = lm.id
   AND (bm.type = cp.type OR bm.type = 'default')
   AND (bm.stage IS NULL OR bm.stage = cp.stage)
   AND (bm.age_bucket IS NULL OR bm.age_bucket = cp.age_bucket)
  JOIN messaging_users mu ON mu.id = cp.user_id
  WHERE lm.project_id = :pid AND lm.status = 'active' AND bm.is_active = true
    AND (CASE WHEN COALESCE(bm.channel, 'whatsapp') = 'email'
              THEN mu.email ELSE COALESCE(mu.phone_e164, mu.phone) END) IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM send_logs sl
      WHERE sl.user_id = cp.user_id AND sl.slot_id = bm.slot_id
        AND sl.status IN ('candidate', 'deferred', 'delayed')
    )
)
"""

_COUNT = text(_RANKED + "SELECT count(*) FROM ranked WHERE specificity_rank = 1")

_INSERT = text(_RANKED + f"""
INSERT INTO send_logs
  (project_id, user_id, channel, recipient, content_type, content_summary,
   content_payload, source_type, status, slot_id, scheduled_at, expires_at,
   intent_class, intent_tier, instance_id, fallback_attempt, open_count, queued_at)
SELECT ranked.project_id, ranked.user_id, COALESCE(ranked.channel, 'whatsapp'),
       ranked.resolved_recipient,
       CASE WHEN ranked.content_mode = 'template' THEN 'template' ELSE 'text' END,
       COALESCE(ranked.content_text, ranked.template_name),
       CASE WHEN ranked.content_mode = 'template'
            THEN jsonb_build_object('content_type', 'template',
                                    'template_name', ranked.template_name,
                                    'template_language', COALESCE(ranked.template_language, 'en_US'),
                                    'template_components', ranked.template_components)
            ELSE jsonb_build_object('content_type', 'text', 'text', ranked.content_text,
                                    'subject', ranked.content_subject) END,
       'base', 'candidate', ranked.slot_id, now(),
       now() + (:ttl || ' minutes')::interval,
       'promotional', 10, ranked.instance_id, 0, 0, now()
FROM ranked
WHERE ranked.specificity_rank = 1
""")


class BaseCompilerWorker:
    def __init__(self, poll_interval: int = 300):
        self.poll_interval = poll_interval
        self._running = False
        self._task = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            return
        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Base compiler worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Base compiler worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    n = self._process_cycle(db)
                    if n:
                        logger.info("[base-compile] %d standing candidates", n)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Error in base compiler worker: %s", e)
            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> int:
        acquired = db.execute(
            text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")
        ).scalar()
        if not acquired:
            return 0
        try:
            pids = [r[0] for r in db.execute(
                text("SELECT DISTINCT project_id FROM base_cell_messages WHERE is_active = true")
            ).all()]
            total = 0
            for pid in pids:
                try:
                    from app.services.engine_rollout_service import effective_mode
                    mode = effective_mode(db, pid, "base_compile")
                    if mode == "off":
                        continue
                    if mode == "shadow":
                        total += db.execute(_COUNT, {"pid": pid}).scalar() or 0
                    else:
                        res = db.execute(_INSERT, {"pid": pid, "ttl": TTL_MINUTES})
                        total += res.rowcount or 0
                        db.commit()
                except Exception as e:
                    db.rollback()
                    logger.error("Base compile failed for project %s: %s", pid, e)
            return total
        finally:
            try:
                db.execute(text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})"))
            except Exception:
                db.rollback()
                db.execute(text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})"))


base_compiler_worker = BaseCompilerWorker()
