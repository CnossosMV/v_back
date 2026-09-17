"""
EventAction -> System Funnel compiler.

Each EventAction is mirrored as a hidden Funnel (source='event_action',
is_system=true). The funnel is the execution and observability unit.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import EventAction, Funnel, FunnelStep

logger = logging.getLogger(__name__)


def _ensure_action_slot_ids(event_action: EventAction) -> bool:
    """Stamp a stable slot_id onto each authored action that lacks one
    (slot-ID contract). Returns True if any were minted, so the caller
    persists the change — once stamped, every recompile reuses the SAME id.
    This is what makes compiled identity survive the delete+recreate."""
    mutated = False
    for action in event_action.actions or []:
        if isinstance(action, dict) and not action.get("slot_id"):
            action["slot_id"] = str(uuid.uuid4())
            mutated = True
    return mutated


def _build_trigger_config(event_action: EventAction) -> dict:
    return {
        "event_name": event_action.trigger_event,
        "conditions": list(event_action.conditions or []),
        "match_mode": "all",
        "priority": int(event_action.priority or 0),
    }


def _build_global_exit_config(event_action: EventAction) -> dict:
    stop_conditions = list(event_action.stop_conditions or [])
    rules = []
    for sc in stop_conditions:
        if not isinstance(sc, dict):
            continue
        evt = sc.get("event")
        window = sc.get("within_seconds")
        if not evt:
            continue
        rules.append({
            "type": "event_within_window",
            "event_name": evt,
            "within_seconds": int(window) if window else None,
        })
    cfg = {}
    if rules:
        cfg["rules"] = rules
    return cfg


def _build_steps_for_actions(actions: list) -> list[dict]:
    """Flatten EventAction.actions into step_configs.

    Each action with delay_seconds > 0 is preceded by a wait step.
    """
    steps: list[dict] = []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        slot = action.get("slot_id")
        delay = int(action.get("delay_seconds") or 0)
        if delay > 0:
            steps.append({
                "step_type": "wait",
                "step_config": {"duration_seconds": delay},
                # Structural wait derives a stable id from its action's slot.
                "slot_id": (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"{slot}:wait"))
                    if slot else None
                ),
            })
        steps.append({
            "step_type": "action",
            "step_config": {
                "action_type": action.get("type"),
                "config": action.get("config") or {},
            },
            "slot_id": slot,
        })
    return steps


def _replace_steps(db: Session, funnel: Funnel, step_defs: list[dict]) -> None:
    """Drop existing steps and re-create from step_defs."""
    for step in list(funnel.steps):
        db.delete(step)
    db.flush()

    for i, sd in enumerate(step_defs):
        # Propagate the authored slot_id (never mint here) so identity survives
        # this delete+recreate. Falls back to a fresh uuid only if the authored
        # object somehow lacks one (defensive — _ensure_action_slot_ids ran).
        db.add(FunnelStep(
            funnel_id=funnel.id,
            step_type=sd["step_type"],
            step_config=sd["step_config"],
            position=i,
            branch="main",
            slot_id=sd.get("slot_id") or str(uuid.uuid4()),
        ))
    db.flush()


def compile_event_action(db: Session, event_action: EventAction) -> Funnel:
    """Upsert the mirrored system Funnel for an EventAction.

    Idempotent: safe to call on every create/update/toggle.
    """
    funnel = (
        db.query(Funnel)
        .filter(Funnel.event_action_id == event_action.id)
        .first()
    )

    # Stamp stable slot_ids onto the authored actions FIRST so every recompile
    # propagates the same identity (slot-ID contract, ruling 14).
    if _ensure_action_slot_ids(event_action):
        flag_modified(event_action, "actions")

    trigger_config = _build_trigger_config(event_action)
    global_exit_config = _build_global_exit_config(event_action)
    status = "active" if event_action.is_active else "paused"

    if funnel is None:
        funnel = Funnel(
            project_id=event_action.project_id,
            name=f"[EventAction] {event_action.name}",
            description=event_action.description,
            status=status,
            trigger_type="event",
            trigger_config=trigger_config,
            global_exit_config=global_exit_config,
            purpose_key=event_action.purpose_key,
            attention_policy=event_action.attention_policy,
            source="event_action",
            is_system=True,
            event_action_id=event_action.id,
            cooldown_seconds=event_action.cooldown_seconds,
            react_to_delivery=event_action.react_to_delivery,
        )
        db.add(funnel)
        db.flush()
    else:
        funnel.name = f"[EventAction] {event_action.name}"
        funnel.description = event_action.description
        funnel.status = status
        funnel.trigger_config = trigger_config
        funnel.global_exit_config = global_exit_config
        funnel.purpose_key = event_action.purpose_key
        funnel.attention_policy = event_action.attention_policy
        funnel.cooldown_seconds = event_action.cooldown_seconds
        funnel.react_to_delivery = event_action.react_to_delivery
        flag_modified(funnel, "trigger_config")
        flag_modified(funnel, "global_exit_config")
        flag_modified(funnel, "attention_policy")

    step_defs = _build_steps_for_actions(event_action.actions or [])
    _replace_steps(db, funnel, step_defs)

    logger.info(
        "Compiled EventAction %s -> Funnel %s (%d steps, status=%s)",
        event_action.id, funnel.id, len(step_defs), status,
    )
    return funnel


def delete_system_funnel(db: Session, event_action_id: int) -> Optional[int]:
    """Remove the mirrored Funnel. Returns the deleted funnel id or None."""
    funnel = (
        db.query(Funnel)
        .filter(Funnel.event_action_id == event_action_id)
        .first()
    )
    if not funnel:
        return None
    fid = funnel.id
    db.delete(funnel)
    db.flush()
    return fid
