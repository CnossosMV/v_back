from datetime import datetime

import pytest

from app.models import EmailInstance, Project, User, Workspace
from app.schemas.event_actions import ActionType
from app.services.capability_registry import CapabilityError, CapabilityRegistry
from app.services.decision_gate_service import DecisionGateService
from app.services.email_instance_resolver import resolve_email_instance
from app.services.engine_rollout_service import EngineRolloutService
from app.services.channels.selection import future_plan_config


def _seed_projects(db):
    owner = User(email="engine-owner@example.test", name="Engine owner", is_active=True)
    db.add(owner)
    db.flush()
    workspace = Workspace(name="Engine workspace", owner_id=owner.id, is_active=True)
    other_workspace = Workspace(name="Other workspace", owner_id=owner.id, is_active=True)
    db.add_all([workspace, other_workspace])
    db.flush()
    owner.workspace_id = workspace.id
    project = Project(name="Engine project", workspace_id=workspace.id, is_active=True)
    sibling = Project(name="Sibling", workspace_id=workspace.id, is_active=True)
    foreign = Project(name="Foreign", workspace_id=other_workspace.id, is_active=True)
    db.add_all([project, sibling, foreign])
    db.flush()
    return owner, workspace, project, sibling, foreign


def test_action_type_matches_all_real_handlers():
    assert {item.value for item in ActionType} == {
        "send_template", "assign_chatbot", "assign_agent", "assign_agent_team",
        "update_user", "add_tag", "webhook", "add_to_funnel", "run_graph",
        "api_call", "send_whatsapp_message",
    }


def test_engine_rollout_dependencies_and_optimistic_lock(db_session, monkeypatch):
    _, _, project, _, _ = _seed_projects(db_session)
    monkeypatch.setenv("SEND_ENGINE_EMERGENCY_OFF", "false")
    monkeypatch.setenv("SEND_LEDGER_IN_SEND", "false")
    monkeypatch.setenv("SEND_GUARDIAN_MODE", "off")
    service = EngineRolloutService(db_session)

    errors = service.validate(project.id, [{"feature_key": "guardian", "mode": "enforce"}])
    assert errors[0]["dependency"] == "ledger"

    features = service.update(project.id, [
        {"feature_key": "ledger", "mode": "enforce", "expected_version": 0},
        {"feature_key": "guardian", "mode": "enforce", "expected_version": 0},
    ], actor_user_id=None)
    effective = {row["feature_key"]: row["effective_mode"] for row in features}
    assert effective["ledger"] == "enforce"
    assert effective["guardian"] == "enforce"

    with pytest.raises(LookupError):
        service.update(project.id, [
            {"feature_key": "ledger", "mode": "shadow", "expected_version": 0},
        ], actor_user_id=None)

    monkeypatch.setenv("SEND_ENGINE_EMERGENCY_OFF", "true")
    assert service.effective_modes(project.id)["ledger"] == "off"


def test_future_plan_rollout_validates_dependencies_and_bounded_config(db_session, monkeypatch):
    _, _, project, _, _ = _seed_projects(db_session)
    monkeypatch.setenv("SEND_ENGINE_EMERGENCY_OFF", "false")
    service = EngineRolloutService(db_session)

    errors = service.validate(project.id, [{
        "feature_key": "future_plan",
        "mode": "enforce",
        "config": {"horizon_minutes": 10080, "collision_window_minutes": 2880},
    }])
    assert {item["dependency"] for item in errors} == {"candidate", "selection"}

    with pytest.raises(ValueError, match="cannot exceed horizon"):
        service.validate(project.id, [{
            "feature_key": "future_plan",
            "mode": "shadow",
            "config": {"horizon_minutes": 60, "collision_window_minutes": 120},
        }])

    # Preview uses replacement semantics, just like rollout.update: omitted
    # fields return to bounded platform defaults instead of leaking the old
    # tenant row into consequence_at_gate.
    from app.models.engine_control import ProjectEngineRollout

    db_session.add(ProjectEngineRollout(
        project_id=project.id,
        feature_key="future_plan",
        mode="shadow",
        config={"horizon_minutes": 20000, "collision_window_minutes": 9000},
    ))
    db_session.flush()
    preview = future_plan_config(
        db_session,
        project.id,
        override_config={"horizon_minutes": 60},
    )
    assert preview["horizon_minutes"] == 60
    assert preview["collision_window_minutes"] == 60


def test_email_instance_resolver_is_project_and_workspace_scoped(db_session):
    owner, workspace, project, sibling, foreign = _seed_projects(db_session)
    shared = EmailInstance(
        user_id=owner.id, workspace_id=workspace.id, project_id=None,
        provider_type="smtp", instance_name="shared-ready", from_email="shared@example.test",
        connection_status="verified", is_active=True,
    )
    explicit = EmailInstance(
        user_id=owner.id, workspace_id=workspace.id, project_id=project.id,
        provider_type="smtp", instance_name="project-ready", from_email="project@example.test",
        connection_status="verified", is_active=True,
    )
    invalid = EmailInstance(
        user_id=owner.id, workspace_id=workspace.id, project_id=project.id,
        provider_type="smtp", instance_name="project-pending", from_email="pending@example.test",
        connection_status="pending", is_active=True,
    )
    db_session.add_all([shared, explicit, invalid])
    db_session.flush()

    assert resolve_email_instance(db_session, project_id=sibling.id, instance_id=shared.id) == shared
    assert resolve_email_instance(db_session, project_id=sibling.id, instance_id=explicit.id) is None
    assert resolve_email_instance(db_session, project_id=foreign.id, instance_id=shared.id) is None
    assert resolve_email_instance(db_session, project_id=project.id, instance_id=invalid.id) is None


def test_capability_requires_simulate_propose_approval_execute(db_session, monkeypatch):
    owner, _, project, _, _ = _seed_projects(db_session)
    monkeypatch.setenv("SEND_ENGINE_EMERGENCY_OFF", "false")
    monkeypatch.setenv("SEND_LEDGER_IN_SEND", "false")
    payload = {"updates": [
        {"feature_key": "ledger", "mode": "shadow", "expected_version": 0},
    ]}
    registry = CapabilityRegistry(db_session)

    with pytest.raises(CapabilityError):
        registry.invoke(
            project_id=project.id, key="engine.rollout", phase="propose",
            payload=payload, actor_user_id=owner.id, idempotency_key="proposal-without-simulation",
        )

    simulated = registry.invoke(
        project_id=project.id, key="engine.rollout", phase="simulate",
        payload=payload, actor_user_id=owner.id, idempotency_key="simulation-one",
    )
    proposed_payload = {**payload, "simulation_execution_id": simulated["capability_execution_id"]}
    proposed = registry.invoke(
        project_id=project.id, key="engine.rollout", phase="propose",
        payload=proposed_payload, actor_user_id=owner.id, idempotency_key="proposal-one",
    )
    choice = DecisionGateService(db_session).choose(
        project.id, proposed["evaluation_id"], "apply_rollout", owner.id,
        "Approved in capability flow test",
    )
    execute_payload = {**payload, "proposal_execution_id": proposed["capability_execution_id"]}
    executed = registry.invoke(
        project_id=project.id, key="engine.rollout", phase="execute",
        payload=execute_payload, actor_user_id=owner.id, idempotency_key="execution-one",
        decision_choice_id=choice.id,
    )
    assert next(
        row for row in executed["features"] if row["feature_key"] == "ledger"
    )["effective_mode"] == "shadow"

    replay = registry.invoke(
        project_id=project.id, key="engine.rollout", phase="execute",
        payload=execute_payload, actor_user_id=owner.id, idempotency_key="execution-one",
        decision_choice_id=choice.id,
    )
    assert replay["capability_execution_id"] == executed["capability_execution_id"]
