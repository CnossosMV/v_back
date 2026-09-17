"""Project-scoped automation conflict analysis.

The MCP agent needs one read-only view that combines user funnels and event
actions.  Neither object owns a complete arbitration model yet, so this
module deliberately reports possible conflicts instead of silently choosing a
winner.  Exact duplicate trigger/condition pairs are errors; broader overlaps
are warnings that require an explicit priority or a deliberate consolidation.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable


def _canonical_json(value: Any) -> Any:
    """Canonicalize JSON conditions; conjunction order has no meaning."""
    if isinstance(value, dict):
        return {key: _canonical_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canonical_json(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str))
    return value


def _json_signature(value: Any) -> str:
    return json.dumps(_canonical_json(value or []), sort_keys=True, separators=(",", ":"), default=str)


def _event_action_trigger(item: Any) -> str | None:
    return getattr(item, "trigger_event", None)


def _funnel_trigger(item: Any) -> str | None:
    if getattr(item, "trigger_type", None) != "event":
        return None
    config = getattr(item, "trigger_config", None) or {}
    return config.get("event_name") or config.get("event")


def _funnel_conditions(item: Any) -> Any:
    config = getattr(item, "trigger_config", None) or {}
    return config.get("conditions") or []


def _action_conditions(item: Any) -> Any:
    return getattr(item, "conditions", None) or []


def _name(item: Any) -> str:
    return str(getattr(item, "name", None) or f"#{getattr(item, 'id', '?')}")


def _finding(
    *,
    code: str,
    severity: str,
    message: str,
    recommendation: str,
    trigger_event: str | None,
    automations: Iterable[Any],
    automation_kind: str,
) -> dict[str, Any]:
    items = list(automations)
    return {
        "code": code,
        "severity": severity,
        "automation_kind": automation_kind,
        "trigger_event": trigger_event,
        "automation_ids": [getattr(item, "id", None) for item in items],
        "automation_names": [_name(item) for item in items],
        "message": message,
        "recommendation": recommendation,
    }


def analyze_automation_conflicts(
    event_actions: Iterable[Any],
    funnels: Iterable[Any],
    *,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Return deterministic, project-scoped findings for an automation set.

    System funnels compiled from Event Actions are excluded from the funnel
    side; their source Event Action is analyzed instead.  This prevents the
    compiler's mirror from being reported as a duplicate automation.
    """
    actions = [
        item for item in event_actions
        if include_inactive or bool(getattr(item, "is_active", False))
    ]
    user_funnels = [
        item for item in funnels
        if (include_inactive or getattr(item, "status", None) == "active")
        and not bool(getattr(item, "is_system", False))
    ]

    findings: list[dict[str, Any]] = []

    action_groups: dict[str, list[Any]] = defaultdict(list)
    for item in actions:
        trigger = _event_action_trigger(item)
        if trigger:
            action_groups[trigger].append(item)

    for trigger, group in action_groups.items():
        for item in group:
            delayed = any(
                isinstance(action, dict) and int(action.get("delay_seconds") or 0) > 0
                for action in (getattr(item, "actions", None) or [])
            )
            if delayed and not (getattr(item, "stop_conditions", None) or []):
                findings.append(_finding(
                    code="delayed_event_action_without_stop",
                    severity="warning",
                    message="A delayed Event Action has no stop condition; it can execute after the contact has moved on.",
                    recommendation="Add a resolve event/window or verify that the delayed effect is still valid at dispatch time.",
                    trigger_event=trigger,
                    automations=[item],
                    automation_kind="event_action",
                ))

        if len(group) < 2:
            continue
        by_conditions: dict[str, list[Any]] = defaultdict(list)
        for item in group:
            by_conditions[_json_signature(_action_conditions(item))].append(item)
        for same_conditions in by_conditions.values():
            if len(same_conditions) > 1:
                findings.append(_finding(
                    code="duplicate_event_action_conditions",
                    severity="error",
                    message="Active Event Actions have the same trigger and conditions; the same contact may receive multiple effects.",
                    recommendation="Consolidate the rules or make their conditions mutually exclusive before activation.",
                    trigger_event=trigger,
                    automations=same_conditions,
                    automation_kind="event_action",
                ))

        priorities = [getattr(item, "priority", 0) or 0 for item in group]
        if len(set(priorities)) < len(priorities) or any(priority <= 0 for priority in priorities):
            findings.append(_finding(
                code="event_action_priority_tie",
                severity="warning",
                message="Multiple active Event Actions can react to the same event without a unique positive priority.",
                recommendation="Assign explicit priorities, or document why the actions are intentionally co-triggered.",
                trigger_event=trigger,
                automations=group,
                automation_kind="event_action",
            ))

    funnel_groups: dict[str, list[Any]] = defaultdict(list)
    for item in user_funnels:
        trigger = _funnel_trigger(item)
        if trigger:
            funnel_groups[trigger].append(item)

    for trigger, group in funnel_groups.items():
        if len(group) > 1:
            by_conditions: dict[str, list[Any]] = defaultdict(list)
            for item in group:
                by_conditions[_json_signature(_funnel_conditions(item))].append(item)
            for same_conditions in by_conditions.values():
                if len(same_conditions) > 1:
                    findings.append(_finding(
                        code="duplicate_funnel_trigger_conditions",
                        severity="error",
                        message="Active user funnels have the same event trigger and conditions; a contact can enter both.",
                        recommendation="Merge them or make the trigger conditions mutually exclusive.",
                        trigger_event=trigger,
                        automations=same_conditions,
                        automation_kind="funnel",
                    ))
            findings.append(_finding(
                code="funnel_trigger_overlap_without_arbiter",
                severity="warning",
                message="More than one active user funnel listens to the same event, but funnels have no independent priority arbiter.",
                recommendation="Define mutually exclusive conditions or let Selection arbitrate the resulting candidates.",
                trigger_event=trigger,
                automations=group,
                automation_kind="funnel",
            ))

    for trigger, action_group in action_groups.items():
        funnel_group = funnel_groups.get(trigger, [])
        if not funnel_group:
            continue
        findings.append(_finding(
            code="event_action_funnel_overlap",
            severity="warning",
            message="An Event Action and a user funnel react to the same event; both systems may create effects for one contact.",
            recommendation="Choose one owner, make conditions exclusive, or confirm the overlap is intentional.",
            trigger_event=trigger,
            automations=[*action_group, *funnel_group],
            automation_kind="cross_system",
        ))

    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: (
        severity_order.get(item["severity"], 9),
        item.get("trigger_event") or "",
        item["code"],
        item["automation_ids"],
    ))
    return {
        "safe_to_activate": not any(item["severity"] == "error" for item in findings),
        "requires_confirmation": bool(findings),
        "finding_count": len(findings),
        "error_count": sum(item["severity"] == "error" for item in findings),
        "warning_count": sum(item["severity"] == "warning" for item in findings),
        "findings": findings,
    }
