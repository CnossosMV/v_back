"""Migration-safe EventAction compiler using reflected SQLAlchemy tables.

Historical Alembic revisions must not import current ORM models: adding a new
model column would make an older revision select a column that does not exist
yet. This helper only addresses columns reflected at the current migration
step, so it remains safe as the runtime schema evolves.
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa


def _known(table: sa.Table, values: dict) -> dict:
    return {key: value for key, value in values.items() if key in table.c}


def compile_event_action_for_migration(conn, event_action_id: int) -> int:
    metadata = sa.MetaData()
    event_actions = sa.Table("event_actions", metadata, autoload_with=conn)
    funnels = sa.Table("funnels", metadata, autoload_with=conn)
    funnel_steps = sa.Table("funnel_steps", metadata, autoload_with=conn)

    event_action = conn.execute(
        sa.select(event_actions).where(event_actions.c.id == event_action_id)
    ).mappings().one()

    actions = [dict(action) for action in (event_action["actions"] or [])]
    actions_changed = False
    for action in actions:
        if not action.get("slot_id"):
            action["slot_id"] = str(uuid.uuid4())
            actions_changed = True
    if actions_changed:
        conn.execute(
            event_actions.update()
            .where(event_actions.c.id == event_action_id)
            .values(actions=actions)
        )

    stop_rules = []
    for condition in event_action["stop_conditions"] or []:
        if not isinstance(condition, dict) or not condition.get("event"):
            continue
        window = condition.get("within_seconds")
        stop_rules.append({
            "type": "event_within_window",
            "event_name": condition["event"],
            "within_seconds": int(window) if window else None,
        })

    funnel_values = _known(funnels, {
        "project_id": event_action["project_id"],
        "name": f"[EventAction] {event_action['name']}",
        "description": event_action["description"],
        "status": "active" if event_action["is_active"] else "paused",
        "trigger_type": "event",
        "trigger_config": {
            "event_name": event_action["trigger_event"],
            "conditions": list(event_action["conditions"] or []),
            "match_mode": "all",
            "priority": int(event_action["priority"] or 0),
        },
        "global_exit_config": {"rules": stop_rules} if stop_rules else {},
        "source": "event_action",
        "is_system": True,
        "event_action_id": event_action_id,
        "cooldown_seconds": event_action["cooldown_seconds"],
        "react_to_delivery": event_action["react_to_delivery"],
    })

    funnel_id = conn.execute(
        sa.select(funnels.c.id).where(funnels.c.event_action_id == event_action_id)
    ).scalar()
    if funnel_id is None:
        funnel_id = conn.execute(
            funnels.insert().values(**funnel_values).returning(funnels.c.id)
        ).scalar_one()
    else:
        conn.execute(
            funnels.update().where(funnels.c.id == funnel_id).values(**funnel_values)
        )

    conn.execute(funnel_steps.delete().where(funnel_steps.c.funnel_id == funnel_id))
    position = 0
    for action in actions:
        delay = int(action.get("delay_seconds") or 0)
        slot_id = action.get("slot_id")
        if delay > 0:
            conn.execute(funnel_steps.insert().values(**_known(funnel_steps, {
                "funnel_id": funnel_id,
                "step_type": "wait",
                "step_config": {"duration_seconds": delay},
                "position": position,
                "branch": "main",
                "slot_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{slot_id}:wait")),
            })))
            position += 1
        conn.execute(funnel_steps.insert().values(**_known(funnel_steps, {
            "funnel_id": funnel_id,
            "step_type": "action",
            "step_config": {
                "action_type": action.get("type"),
                "config": action.get("config") or {},
            },
            "position": position,
            "branch": "main",
            "slot_id": slot_id,
        })))
        position += 1

    return int(funnel_id)
