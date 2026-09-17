"""
Outbound Selection (Phase 2).

The core arbitration: given several candidate sends competing for one
contact's next attention slot, pick ONE winner. This is the piece that did
not exist anywhere before — every source fired independently and only the
pacing layer throttled, which is why same-contact collisions happened
(MES even discounts ±2h attribution conflicts to paper over it).

This module is the pure ranking core + the migration-mode switch. Phase 2
ships it SHADOW-first: the dispatch worker groups due sends by contact and
logs the contest (`[selection][shadow] ...`) without changing what sends, so
the selector is validated against real traffic before any cutover. The
durable candidate ledger (`status='candidate'`), set-based selection, and
the dispatch split come in later increments once shadow parity holds.

Ranking order (highest wins the slot):
1. intent_tier DESC  — declared importance (Phase 1 shadow tiers per lane:
   transactional 90 > conversational 70 > manual 50 > promotional 10), so
   protected lanes naturally outrank promotional and conversational replies
   are never starved.
2. perishability     — sooner expires_at wins (about to be lost).
3. age               — oldest scheduled_at wins (waited longest).
4. id                — stable final tiebreak.

The bandit tie-break (execution arms among co-equal candidates) plugs in at
step 4 in Phase 4 — NOT here; intent ordering is declared, never learned.
"""
import hashlib
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


# Rows in these states still compete for the contact's next attention slot.
# ``campaign_selected`` is a reservation, not a provider-I/O authorization:
# it must remain visible to a newer contender until CampaignWorker performs
# its final, locked hand-off to ``submitting``.
ATTENTION_CONTENDER_STATUSES = (
    "delayed",
    "deferred",
    "candidate",
    "campaign_selected",
)
CAMPAIGN_ATTENTION_STATUSES = ("candidate", "campaign_selected")


def selection_mode(db=None, project_id=None) -> str:
    """'off' | 'shadow' | 'enforce' from SEND_SELECTION_MODE. Default 'off'
    (no grouping, current per-row dispatch). 'shadow' logs contests only;
    'enforce' makes the winner dispatch and holds losers."""
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "selection")


def candidate_mode(db=None, project_id=None) -> str:
    """'off' | 'enforce' from SEND_CANDIDATE_MODE. Default 'off'.

    When 'enforce', a PROMOTIONAL immediate send() parks as a durable
    status='candidate' row instead of delivering inline, so it contests the
    contact's next slot against other candidates (true cross-time
    competition) rather than firing independently. The selection sweep
    dispatches the winner; unselected candidates expire after the TTL.
    """
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "candidate")


def candidate_ttl_minutes(db=None, project_id=None) -> int:
    """Validity window for a parked candidate. After this it expires unsent
    rather than competing forever. SEND_CANDIDATE_TTL_MIN, default 60."""
    try:
        if db is not None and project_id is not None:
            from app.models.engine_control import ProjectEngineRollout
            row = db.query(ProjectEngineRollout).filter(
                ProjectEngineRollout.project_id == project_id,
                ProjectEngineRollout.feature_key == "candidate",
            ).first()
            if row and row.config and row.config.get("ttl_minutes") is not None:
                return max(1, int(row.config["ttl_minutes"]))
        return max(1, int(os.getenv("SEND_CANDIDATE_TTL_MIN", "60")))
    except ValueError:
        return 60


_FUTURE_PLAN_DEFAULTS = {
    "horizon_minutes": 10080,
    "collision_window_minutes": 2880,
    "recheck_minutes": 30,
    "max_candidates_per_contact": 100,
}


def future_plan_mode(db=None, project_id=None) -> str:
    """Return the tenant-scoped future planning rollout mode."""
    from app.services.engine_rollout_service import effective_mode

    return effective_mode(db, project_id, "future_plan")


def future_plan_config(
    db=None,
    project_id=None,
    *,
    override_config: Dict[str, Any] | None = None,
) -> Dict[str, int]:
    """Resolve bounded planning controls from tenant config then env defaults.

    ``override_config`` previews a replacement tenant policy without reading
    the currently persisted row.  That mirrors EngineRolloutService.update,
    where ``config`` is replaced rather than merged.
    """
    values = dict(_FUTURE_PLAN_DEFAULTS)
    env_names = {
        "horizon_minutes": "SEND_FUTURE_PLAN_HORIZON_MIN",
        "collision_window_minutes": "SEND_FUTURE_PLAN_COLLISION_MIN",
        "recheck_minutes": "SEND_FUTURE_PLAN_RECHECK_MIN",
        "max_candidates_per_contact": "SEND_FUTURE_PLAN_MAX_CANDIDATES",
    }
    for key, env_name in env_names.items():
        try:
            values[key] = int(os.getenv(env_name, str(values[key])))
        except (TypeError, ValueError):
            pass
    if override_config is None and db is not None and project_id is not None:
        try:
            from app.models.engine_control import ProjectEngineRollout

            row = db.query(ProjectEngineRollout).filter(
                ProjectEngineRollout.project_id == project_id,
                ProjectEngineRollout.feature_key == "future_plan",
            ).first()
            for key, value in ((row.config or {}) if row else {}).items():
                if key in values:
                    values[key] = int(value)
        except (TypeError, ValueError):
            pass
    elif override_config is not None:
        for key, value in override_config.items():
            if key in values:
                try:
                    values[key] = int(value)
                except (TypeError, ValueError):
                    pass
    values["horizon_minutes"] = max(60, min(values["horizon_minutes"], 43200))
    values["collision_window_minutes"] = max(
        1,
        min(values["collision_window_minutes"], values["horizon_minutes"], 10080),
    )
    values["recheck_minutes"] = max(1, min(values["recheck_minutes"], 1440))
    values["max_candidates_per_contact"] = max(
        2, min(values["max_candidates_per_contact"], 1000)
    )
    return values


def attention_identity(
    project_id: int,
    user_id: Optional[int],
    recipient: Optional[str],
    attention_scope: Optional[str],
) -> str:
    """Return the canonical lock identity for one authored attention slot."""
    identity = f"u:{user_id}" if user_id is not None else f"r:{recipient or ''}"
    scope = attention_scope or "contact.promotional"
    return f"versya:attention:{project_id}:{identity}:{scope}"


def attention_lock_id(
    project_id: int,
    user_id: Optional[int],
    recipient: Optional[str],
    attention_scope: Optional[str],
) -> int:
    """Stable signed bigint accepted by PostgreSQL advisory-lock functions."""
    raw = attention_identity(project_id, user_id, recipient, attention_scope)
    return int.from_bytes(
        hashlib.blake2b(raw.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )


def acquire_attention_xact_lock(
    db,
    project_id: int,
    user_id: Optional[int],
    recipient: Optional[str],
    attention_scope: Optional[str],
) -> Optional[int]:
    """Serialize contender creation and the final selection boundary.

    The lock is transaction-scoped and therefore releases on commit/rollback.
    Non-PostgreSQL test/development databases keep the pure ordering contract
    but cannot provide cross-session serialization.
    """
    if db is None or db.get_bind().dialect.name != "postgresql":
        return None
    from sqlalchemy import text

    lock_id = attention_lock_id(
        project_id, user_id, recipient, attention_scope,
    )
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": lock_id},
    )
    return lock_id


def acquire_attention_session_lock(
    db,
    project_id: int,
    user_id: Optional[int],
    recipient: Optional[str],
    attention_scope: Optional[str],
) -> Optional[int]:
    """Hold one slot across ScheduledSendWorker's complete dispatch cycle."""
    if db is None or db.get_bind().dialect.name != "postgresql":
        return None
    from sqlalchemy import text

    lock_id = attention_lock_id(
        project_id, user_id, recipient, attention_scope,
    )
    db.execute(
        text("SELECT pg_advisory_lock(:lock_id)"),
        {"lock_id": lock_id},
    )
    return lock_id


def release_attention_session_lock(db, lock_id: Optional[int]) -> None:
    if lock_id is None or db is None or db.get_bind().dialect.name != "postgresql":
        return
    from sqlalchemy import text

    db.execute(
        text("SELECT pg_advisory_unlock(:lock_id)"),
        {"lock_id": lock_id},
    )


def due_attention_contest(db, anchor: Any, now: datetime, *, lock: bool = True) -> List[Any]:
    """Load the complete due contest for ``anchor`` under one slot lock.

    Campaign delayed/deferred rows remain CampaignWorker-owned and are not
    candidates by themselves. A campaign appears here only while waiting for
    Selection (``candidate``) or while holding a revocable reservation
    (``campaign_selected``).
    """
    from sqlalchemy import or_
    from app.models import SendLog

    scope = getattr(anchor, "attention_scope", None) or "contact.promotional"
    scope_filter = (
        or_(SendLog.attention_scope == scope, SendLog.attention_scope.is_(None))
        if scope == "contact.promotional"
        else SendLog.attention_scope == scope
    )
    user_id = getattr(anchor, "user_id", None)
    identity_filter = (
        SendLog.user_id == user_id
        if user_id is not None
        else SendLog.recipient == getattr(anchor, "recipient", None)
    )
    query = db.query(SendLog).filter(
        SendLog.project_id == getattr(anchor, "project_id"),
        identity_filter,
        scope_filter,
        SendLog.status.in_(ATTENTION_CONTENDER_STATUSES),
        SendLog.scheduled_at <= now,
        or_(
            SendLog.source_type != "campaign",
            SendLog.status.in_(CAMPAIGN_ATTENTION_STATUSES),
        ),
    ).order_by(
        SendLog.priority.desc(),
        SendLog.scheduled_at.asc(),
        SendLog.id.asc(),
    )
    if lock:
        query = query.with_for_update(of=SendLog)
    return query.all()


def future_attention_contest(
    db,
    anchor: Any,
    now: datetime,
    *,
    horizon_end: datetime | None = None,
    lock: bool = True,
    include_non_reserving: bool = False,
) -> List[Any]:
    """Load known future intents for the same contact/scope.

    A future row is evidence for planning, never provider authorization.  In
    the runtime gate only a row whose snapshotted tenant policy explicitly
    opts into ``future_reservation`` may hold an already-due row.
    """
    from sqlalchemy import or_
    from app.models import SendLog

    config = future_plan_config(db, getattr(anchor, "project_id", None))
    end = horizon_end or now + timedelta(minutes=config["horizon_minutes"])
    scope = getattr(anchor, "attention_scope", None) or "contact.promotional"
    scope_filter = (
        or_(SendLog.attention_scope == scope, SendLog.attention_scope.is_(None))
        if scope == "contact.promotional"
        else SendLog.attention_scope == scope
    )
    user_id = getattr(anchor, "user_id", None)
    identity_filter = (
        SendLog.user_id == user_id
        if user_id is not None
        else SendLog.recipient == getattr(anchor, "recipient", None)
    )
    query = db.query(SendLog).filter(
        SendLog.project_id == getattr(anchor, "project_id"),
        identity_filter,
        scope_filter,
        SendLog.status.in_(ATTENTION_CONTENDER_STATUSES),
        SendLog.scheduled_at > now,
        SendLog.scheduled_at <= end,
        SendLog.is_historical == False,  # noqa: E712
    ).order_by(SendLog.scheduled_at.asc(), SendLog.id.asc())
    if lock:
        query = query.with_for_update(of=SendLog)
    rows = query.limit(config["max_candidates_per_contact"]).all()
    if include_non_reserving:
        return rows
    return [row for row in rows if has_future_reservation(row)]


def has_future_reservation(candidate: Any) -> bool:
    policy = getattr(candidate, "attention_policy_snapshot", None) or {}
    return bool(isinstance(policy, dict) and policy.get("future_reservation"))


def arbitrate_due_attention(
    db,
    due_candidates: List[Any],
    now: datetime,
    *,
    lock_future: bool = True,
) -> Dict[str, Any]:
    """Compare the due winner with tenant-authorized future reservations.

    ``dispatch_winner`` is binding for this cycle.  A future projected winner
    can set it to ``None`` only in enforce mode; shadow mode records the same
    consequence while preserving the due-only behavior.
    """
    due = eligible_candidates(due_candidates, now)
    due_ranked = rank_candidates(due)
    if not due:
        return {
            "mode": "off",
            "due": due_ranked,
            "projected": due_ranked,
            "dispatch_winner": None,
            "attention_owner": None,
            "future_candidates": [],
            "hold_until": None,
            "consequence_at_gate": "no_due_candidate",
        }

    project_id = getattr(due[0], "project_id", None)
    mode = future_plan_mode(db, project_id)
    if mode == "off":
        return {
            "mode": mode,
            "due": due_ranked,
            "projected": due_ranked,
            "dispatch_winner": due_ranked["winner"],
            "attention_owner": due_ranked["winner"],
            "future_candidates": [],
            "hold_until": None,
            "consequence_at_gate": "dispatch_due_winner",
        }

    config = future_plan_config(db, project_id)
    collision_end = now + timedelta(minutes=config["collision_window_minutes"])
    future = future_attention_contest(
        db,
        due[0],
        now,
        horizon_end=collision_end,
        lock=lock_future,
        include_non_reserving=False,
    )
    # A row that expires before its own send time cannot reserve anything.
    future = [
        row for row in eligible_candidates(future, now)
        if not getattr(row, "expires_at", None)
        or row.expires_at > getattr(row, "scheduled_at", now)
    ]
    projected = rank_candidates([*due, *future])
    owner = projected["winner"] or due_ranked["winner"]
    owner_is_future = owner in future
    binding_hold = mode == "enforce" and owner_is_future
    hold_until = None
    if binding_hold:
        hold_until = min(
            owner.scheduled_at,
            now + timedelta(minutes=config["recheck_minutes"]),
        )
    return {
        "mode": mode,
        "config": config,
        "due": due_ranked,
        "projected": projected,
        "dispatch_winner": None if binding_hold else due_ranked["winner"],
        "attention_owner": owner,
        "future_candidates": future,
        "hold_until": hold_until,
        "consequence_at_gate": (
            "hold_due_for_future_reservation"
            if binding_hold
            else "would_hold_due_for_future_reservation"
            if owner_is_future
            else "dispatch_due_winner"
        ),
    }


def eligible_candidates(candidates: List[Any], now: datetime) -> List[Any]:
    """Exclude expired rows before ranking so they can never consume a slot."""
    return [
        candidate
        for candidate in candidates
        if not (
            getattr(candidate, "expires_at", None)
            and candidate.expires_at <= now
        )
    ]


# Higher intent_tier wins. When a row has no intent_tier yet (pre-Phase-1
# rows), fall back to this so it sorts as low-importance rather than crashing.
_FALLBACK_TIER = 0
_MAX_DT = datetime.max


def _sort_key(c: Any):
    tier = getattr(c, "intent_tier", None)
    tier = tier if tier is not None else _FALLBACK_TIER
    expires = getattr(c, "expires_at", None) or _MAX_DT
    scheduled = getattr(c, "scheduled_at", None) or _MAX_DT
    cid = getattr(c, "id", 0) or 0
    # Negate tier for DESC; everything else ASC.
    return (-tier, expires, scheduled, cid)


def rank_candidates(candidates: List[Any]) -> Dict[str, Any]:
    """Rank candidate sends for ONE contact's next slot.

    Returns {'winner', 'ordered', 'trace'}. `winner` is None for an empty
    input. Pure — reads attributes only, writes nothing.
    """
    if not candidates:
        return {"winner": None, "ordered": [], "trace": []}

    ordered = sorted(candidates, key=_sort_key)
    winner = ordered[0]

    def _entry(c: Any, rank: int) -> Dict[str, Any]:
        return {
            "rank": rank,
            "send_log_id": getattr(c, "id", None),
            "source_type": getattr(c, "source_type", None),
            "lane": getattr(c, "intent_class", None),
            "intent_tier": getattr(c, "intent_tier", None),
            "channel": getattr(c, "channel", None) or getattr(c, "preferred_channel", None),
            "expires_at": (getattr(c, "expires_at", None) or None)
            and c.expires_at.isoformat(),
        }

    trace = [_entry(c, i) for i, c in enumerate(ordered)]
    return {"winner": winner, "ordered": ordered, "trace": trace}


def group_by_contact(candidates: List[Any]) -> Dict[Any, List[Any]]:
    """Group by contact *and authored attention scope*.

    Legacy promotional candidates share ``contact.promotional``. Distinct
    scopes are independent by contract and therefore never suppress one
    another merely because they address the same person.
    """
    groups: Dict[Any, List[Any]] = {}
    for c in candidates:
        uid = getattr(c, "user_id", None)
        scope = getattr(c, "attention_scope", None) or "contact.promotional"
        identity = ("u", uid) if uid is not None else ("r", getattr(c, "recipient", None))
        key = (*identity, scope)
        groups.setdefault(key, []).append(c)
    return groups
