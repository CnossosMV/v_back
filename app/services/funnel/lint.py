"""
Funnel lint (escape-hatch guidance) — surfaces the opinionated risks on a
hand-built funnel WITHOUT blocking it. Embodies "make risks visible rather
than forbid": forks (path-not-tree), missing stop conditions, hardcoded
channels. The funnel still runs; the user just sees the guidance.
"""
from typing import Any, Dict, List


def lint_funnel(funnel) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    steps = list(funnel.steps or [])
    cfg = funnel.global_exit_config or {}

    # 1. Fork — Episodes are paths, not trees.
    fork_ids = [s.id for s in steps if s.step_type == "fork"]
    if fork_ids:
        findings.append({
            "severity": "warn", "code": "fork",
            "message": (
                "This funnel forks into parallel paths. Episodes are paths, "
                "not trees — branches that fork the future make conflicts hard "
                "to reason about. Consider sequential steps or separate funnels."
            ),
            "step_ids": fork_ids,
        })

    # 2. No stop condition — may message indefinitely.
    has_stop = bool(
        cfg.get("goal_events") or cfg.get("time_limit_hours")
        or cfg.get("message_limit") or cfg.get("rules")
    )
    if not has_stop:
        findings.append({
            "severity": "warn", "code": "no_stop",
            "message": (
                "No stop condition (goal event, time limit, or message limit). "
                "Add 'when should this stop?' so it doesn't keep messaging "
                "after it's no longer relevant."
            ),
        })

    # 3. Hardcoded channel — bypasses the send layer's channel choice.
    hc = []
    for s in steps:
        scfg = s.step_config or {}
        inner = scfg.get("config") or {}
        if scfg.get("channel") or inner.get("channel"):
            hc.append(s.id)
    if hc:
        findings.append({
            "severity": "info", "code": "hardcoded_channel",
            "message": (
                "Some steps pin a channel, bypassing channel fallback. The send "
                "layer can pick the best available channel for each contact."
            ),
            "step_ids": hc,
        })

    return findings
