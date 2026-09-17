"""
Reward Sweep Worker (Phase 4) — resolves send outcomes into ArmObservations
OFF the hot path.

For every delivered send that carries a slot_id and whose attribution window
has elapsed, write one append-once observation for the `channel` decision
class: arm = the realized channel, success = did the contact produce a
user-initiated event within the window after the send. Set-based, idempotent
(NOT EXISTS guard + the unique (send_log_id, decision_class) constraint), so
it never double-counts and never blocks a send.

The realized channel is an observational arm (not yet interventional) — a
legitimate way to populate the ledger before the bandit chooses channels.
Variant / send_time / tie_break arms come once selection records them.

Flag-gated BANDIT_REWARD_SWEEP (default off): additive, writes only
arm_observations. Complaints/opt-outs are NOT negative reward (they're
Governor suspension) — only genuine engagement counts as success.
"""
import asyncio
import logging

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 739003
ATTRIBUTION_HOURS = 24


# One set-based pass: resolve every attributable, window-elapsed send not yet
# observed for the channel class. success = a user-initiated event landed in
# (sent_at, sent_at + window].
_RESOLVE = text("""
INSERT INTO arm_observations
  (project_id, decision_class, scope_key, arm_key, send_log_id, success, observed_at)
SELECT sl.project_id, 'channel',
       'channel:' || sl.slot_id,
       COALESCE(sl.resolved_channel, sl.channel, 'unknown'),
       sl.id,
       EXISTS (
         SELECT 1 FROM messaging_events me
         WHERE me.project_id = sl.project_id AND me.user_id = sl.user_id
           AND me.created_at > sl.sent_at
           AND me.created_at <= sl.sent_at + (:hrs || ' hours')::interval
           AND me.source NOT IN ('send_service', 'delivery_tracker', 'system')
       ),
       now()
FROM send_logs sl
WHERE sl.slot_id IS NOT NULL
  AND sl.project_id = :pid
  AND sl.status = 'sent'
  AND sl.user_id IS NOT NULL
  AND sl.sent_at IS NOT NULL
  AND sl.sent_at < now() - (:hrs || ' hours')::interval
  AND NOT EXISTS (
    SELECT 1 FROM arm_observations ao
    WHERE ao.send_log_id = sl.id AND ao.decision_class = 'channel'
  )
""")


class RewardSweepWorker:
    def __init__(self, poll_interval: int = 600):
        self.poll_interval = poll_interval
        self._running = False
        self._task = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            return
        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Reward sweep worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Reward sweep worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    n = self._process_cycle(db)
                    if n:
                        logger.info("Reward sweep wrote %d arm observations", n)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Error in reward sweep worker: %s", e)
            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> int:
        acquired = db.execute(
            text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")
        ).scalar()
        if not acquired:
            return 0
        try:
            from app.services.engine_rollout_service import effective_mode
            pids = [row[0] for row in db.execute(text(
                "SELECT DISTINCT project_id FROM send_logs WHERE slot_id IS NOT NULL"
            )).all()]
            total = 0
            for pid in pids:
                if effective_mode(db, pid, "bandit_reward") != "enforce":
                    continue
                res = db.execute(_RESOLVE, {"hrs": ATTRIBUTION_HOURS, "pid": pid})
                total += res.rowcount or 0
            db.commit()
            return total
        finally:
            db.execute(text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})"))


reward_sweep_worker = RewardSweepWorker()
