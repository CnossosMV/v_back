from types import SimpleNamespace

from app.services.automation_conflicts import analyze_automation_conflicts
from app.mcp.auth import SUPPORTED_SCOPES


def _action(id, trigger="subscriber.created", conditions=None, priority=0, active=True, actions=None):
    return SimpleNamespace(
        id=id,
        name=f"Action {id}",
        trigger_event=trigger,
        conditions=conditions or [],
        priority=priority,
        is_active=active,
        actions=actions or [{"type": "send_template"}],
        stop_conditions=[],
    )


def _funnel(id, trigger="subscriber.created", conditions=None, status="active", system=False):
    return SimpleNamespace(
        id=id,
        name=f"Funnel {id}",
        trigger_type="event",
        trigger_config={"event_name": trigger, "conditions": conditions or []},
        status=status,
        is_system=system,
    )


def test_exact_event_action_duplicate_is_error_and_priority_tie_is_visible():
    result = analyze_automation_conflicts([
        _action(1, conditions=[{"field": "plan", "operator": "==", "value": "pro"}]),
        _action(2, conditions=[{"field": "plan", "operator": "==", "value": "pro"}]),
    ], [])

    codes = {finding["code"] for finding in result["findings"]}
    assert "duplicate_event_action_conditions" in codes
    assert "event_action_priority_tie" in codes
    assert result["safe_to_activate"] is False


def test_condition_order_does_not_hide_duplicate_rules():
    result = analyze_automation_conflicts([
        _action(1, conditions=[
            {"field": "plan", "operator": "==", "value": "pro"},
            {"field": "locale", "operator": "==", "value": "pt-BR"},
        ]),
        _action(2, conditions=[
            {"field": "locale", "operator": "==", "value": "pt-BR"},
            {"field": "plan", "operator": "==", "value": "pro"},
        ]),
    ], [])
    assert "duplicate_event_action_conditions" in {finding["code"] for finding in result["findings"]}


def test_cross_system_overlap_is_reported_but_system_funnel_is_not_duplicate():
    result = analyze_automation_conflicts(
        [_action(1, priority=10)],
        [_funnel(2), _funnel(3, system=True)],
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert "event_action_funnel_overlap" in codes
    assert "funnel_trigger_overlap_without_arbiter" not in codes
    assert result["safe_to_activate"] is True


def test_inactive_automations_are_ignored_by_default():
    result = analyze_automation_conflicts(
        [_action(1, active=False)],
        [_funnel(2, status="paused")],
    )
    assert result["findings"] == []


def test_single_delayed_event_action_without_stop_is_warned():
    result = analyze_automation_conflicts(
        [_action(1, actions=[{"type": "send_template", "delay_seconds": 3600}])],
        [],
    )
    assert {finding["code"] for finding in result["findings"]} == {"delayed_event_action_without_stop"}
    assert result["safe_to_activate"] is True


def test_automation_scopes_are_advertised():
    assert "versya.automations:read" in SUPPORTED_SCOPES
    assert "versya.automations:write" in SUPPORTED_SCOPES
