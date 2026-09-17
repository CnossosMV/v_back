"""
Feature flags for the send-path consolidation (Phase 0/0D).

Centralized so every site that participates in a migration reads the same
switch. All default to the SAFE pre-consolidation behavior, so deploying
the code changes nothing until a flag is deliberately flipped after the
corresponding shadow/parity phase.
"""
import os


def _truthy(name: str) -> bool:
    return os.getenv(name, "false").lower() in ("1", "true", "yes")


def ledger_in_send_enabled(db=None, project_id=None) -> bool:
    """When true, SendService.send() is the single contact_ledger writer and
    the legacy per-caller ledger writes (the ones whose send goes through
    send()) skip themselves. Default false = legacy writers active, send()
    does not write — identical to pre-0D behavior.

    Flip only after confirming send() ledger parity against the legacy
    writers (hazard H3: a moment where both write double-counts attention).
    """
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "ledger") == "enforce"


def reroute_mode(path: str, db=None, project_id=None) -> str:
    """Migration mode for a bypass path being moved onto SendService.send().

    Returns one of:
    - 'off'     : legacy direct send only (pre-0D behavior). DEFAULT.
    - 'shadow'  : legacy send stays authoritative; also call send(dry_run=True)
                  and log a parity line — no second delivery, no SendLog.
    - 'enforce' : send() is authoritative; the legacy direct send is skipped.

    Controlled by env SEND_ROUTE_<PATH> (path upper-cased). Unknown values
    fall back to 'off' so a typo never silently reroutes live traffic.
    """
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, f"reroute_{path.lower()}")


def guardian_mode(db=None, project_id=None) -> str:
    """'off' | 'shadow' | 'enforce' from SEND_GUARDIAN_MODE. Default 'off'.

    When 'enforce', send() consults the Guardian (lane-aware attention budget /
    cooldown / quiet hours via PolicyService) and acts on the verdict —
    delay→defer, drop→skip. This closes the Phase-0 gap that send() never
    enforced PolicyService caps. 'shadow' logs the verdict, changes nothing.
    """
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "guardian")


def bandit_act_mode(db=None, project_id=None) -> str:
    """'off' | 'shadow' | 'enforce' from SEND_BANDIT_ACT. Default 'off'.

    When 'enforce', the selection pass acts on the bandit's recommendation:
    if a channel arm has earned auto for the winning slot, the winner is
    re-targeted to that channel before dispatch (execution-only — never
    changes which message or its intent). The RewardSweep then closes the
    loop on the realized channel. 'shadow' logs the recommendation only.
    """
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "bandit_act")
