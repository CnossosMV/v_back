"""
Position Sweep Worker (Phase 3) — maintains each contact's (Type, Stage, Age)
position in SHADOW.

Set-based per project (the journey-materializer pattern, NOT a per-contact
Python loop): derive Type from the exclusive segment, Stage from
lifecycle_stage, and Age from the Type-entry timestamp. Age is recomputed
every cycle so time-driven bucket crossings (first_days -> first_weeks ...)
advance even for silent contacts. Type/Stage changes are appended to the
transition log so a contact's position is queryable over time.

SHADOW: this only computes and stores position; nothing reads it to drive
sends yet (Phase 3 is additive per decision-log ruling 1). Exit criterion:
a contact's Position is queryable + historical.
"""
import asyncio
import logging

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 739002  # unique ID for the position sweep worker


def _emit_transitions_enabled(db, project_id: int) -> bool:
    """When true, position type/stage changes ALSO emit MessagingEvents
    (position.type_changed / position.stage_changed) that the EventDrainWorker
    picks up so funnels/event-actions can trigger on them. Default false keeps
    Position fully shadow (transitions only logged to history). Flip
    POSITION_EMIT_TRANSITIONS at the additive cutover (ruling 1)."""
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "position") == "enforce"

# Age buckets from time-in-Type. User-configurable per Type later; sane
# defaults now. Applied as a SQL CASE over position_entered_at.
_AGE_CASE = """CASE
  WHEN position_entered_at IS NULL THEN NULL
  WHEN position_entered_at > now() - interval '1 day'   THEN 'first_hours'
  WHEN position_entered_at > now() - interval '7 days'  THEN 'first_days'
  WHEN position_entered_at > now() - interval '30 days' THEN 'first_weeks'
  WHEN position_entered_at > now() - interval '365 days' THEN 'settled'
  ELSE 'veteran' END"""

_LOG_TRANSITIONS = text("""
INSERT INTO contact_position_transitions
  (project_id, user_id, lifecycle_model_id, from_type, to_type, from_stage, to_stage, reason, occurred_at, provenance)
SELECT cp.project_id, cp.user_id, cp.lifecycle_model_id, cp.type, COALESCE(mu.segment_name, 'default'),
       cp.stage, mu.lifecycle_stage,
       CASE WHEN cp.type IS DISTINCT FROM COALESCE(mu.segment_name, 'default')
            THEN 'type_change' ELSE 'stage_change' END,
       now(), jsonb_build_object('model_id', cp.lifecycle_model_id, 'legacy_compatibility', true)
FROM contact_positions cp
JOIN messaging_users mu ON mu.id = cp.user_id
WHERE cp.project_id = :pid
  AND cp.lifecycle_model_id = :mid
  AND (cp.type IS DISTINCT FROM COALESCE(mu.segment_name, 'default')
       OR cp.stage IS DISTINCT FROM mu.lifecycle_stage)
""")

# RHS of DO UPDATE uses the OLD contact_positions row values (Postgres
# semantics), so the type-change comparison is correct even though we also
# set type=EXCLUDED.type in the same statement.
_UPSERT_POSITIONS = text("""
INSERT INTO contact_positions
  (project_id, user_id, lifecycle_model_id, type, stage, position_entered_at, type_entered_at, computed_at, provenance, explanation)
SELECT mu.project_id, mu.id, :mid, COALESCE(mu.segment_name, 'default'), mu.lifecycle_stage,
       COALESCE(mu.segment_updated_at, mu.first_seen_at, mu.created_at),
       COALESCE(mu.segment_updated_at, mu.first_seen_at, mu.created_at),
       now(), jsonb_build_object('model_id', :mid, 'legacy_compatibility', true),
       jsonb_build_object('type_rule', 'legacy.segment_name', 'stage_rule', 'legacy.lifecycle_stage')
FROM messaging_users mu
WHERE mu.project_id = :pid
ON CONFLICT (project_id, user_id, lifecycle_model_id) DO UPDATE SET
  stage = EXCLUDED.stage,
  computed_at = now(),
  position_entered_at = CASE
    WHEN contact_positions.type IS DISTINCT FROM EXCLUDED.type
    THEN EXCLUDED.position_entered_at ELSE contact_positions.position_entered_at END,
  type_entered_at = CASE
    WHEN contact_positions.type IS DISTINCT FROM EXCLUDED.type
    THEN EXCLUDED.type_entered_at ELSE contact_positions.type_entered_at END,
  type = EXCLUDED.type
""")

_REFRESH_AGE = text(f"UPDATE contact_positions SET age_bucket = {_AGE_CASE} WHERE project_id = :pid AND lifecycle_model_id = :mid")

# Emit MessagingEvents for changed contacts so the EventDrainWorker runs them
# through process_event (funnel triggers / event actions). Same comparison +
# timing as the transition log (before the upsert, against the stored row).
_EMIT_TRANSITION_EVENTS = text("""
INSERT INTO messaging_events
  (project_id, user_id, event_name, properties, source, processed, created_at, occurred_at, processing_mode)
SELECT cp.project_id, cp.user_id,
       CASE WHEN cp.type IS DISTINCT FROM COALESCE(mu.segment_name, 'default')
            THEN 'position.type_changed' ELSE 'position.stage_changed' END,
       jsonb_build_object(
         'from_type', cp.type, 'to_type', COALESCE(mu.segment_name, 'default'),
         'from_stage', cp.stage, 'to_stage', mu.lifecycle_stage,
         'lifecycle_model_id', cp.lifecycle_model_id),
       'system', false, now(), now(), 'live'
FROM contact_positions cp
JOIN messaging_users mu ON mu.id = cp.user_id
WHERE cp.project_id = :pid
  AND cp.lifecycle_model_id = :mid
  AND (cp.type IS DISTINCT FROM COALESCE(mu.segment_name, 'default')
       OR cp.stage IS DISTINCT FROM mu.lifecycle_stage)
""")


class PositionSweepWorker:
    """Background worker that maintains contact positions (shadow)."""

    def __init__(self, poll_interval: int = 300):
        self.poll_interval = poll_interval
        self._running = False
        self._task = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Position sweep worker already running")
            return
        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Position sweep worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Position sweep worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    n = self._process_cycle(db)
                    if n:
                        logger.info("Position sweep maintained %d contact positions", n)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Error in position sweep worker: %s", e)
            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> int:
        acquired = db.execute(
            text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")
        ).scalar()
        if not acquired:
            return 0
        try:
            project_ids = [
                r[0] for r in db.execute(
                    text("SELECT DISTINCT project_id FROM messaging_users")
                ).all()
            ]
            total = 0
            for pid in project_ids:
                from app.models.project_import import LifecycleModel
                from app.services.lifecycle_model_service import LifecycleModelService

                lifecycle = LifecycleModelService(db)
                lifecycle.ensure_legacy_model(pid)
                db.flush()
                models = db.query(LifecycleModel).filter(
                    LifecycleModel.project_id == pid,
                    LifecycleModel.status.in_(["active", "shadow"]),
                ).order_by(LifecycleModel.status).all()
                for model in models:
                    if (model.definition or {}).get("key") == "legacy-v1":
                        params = {"pid": pid, "mid": model.id}
                        # Order matters: compare against the stored row before
                        # upserting the new legacy-derived position.
                        db.execute(_LOG_TRANSITIONS, params)
                        if model.status == "active" and _emit_transitions_enabled(db, pid):
                            db.execute(_EMIT_TRANSITION_EVENTS, params)
                        db.execute(_UPSERT_POSITIONS, params)
                        res = db.execute(_REFRESH_AGE, params)
                        total += res.rowcount or 0
                    else:
                        outcome = lifecycle.materialize(
                            model,
                            emit_transitions=True,
                            emit_events=model.status == "active" and _emit_transitions_enabled(db, pid),
                        )
                        total += outcome["classified"]
                    db.commit()
            return total
        finally:
            db.execute(text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})"))


position_sweep_worker = PositionSweepWorker()
