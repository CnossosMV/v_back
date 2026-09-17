"""PostgreSQL smoke for project rollout, capability flow and sender scoping."""

import os
from uuid import uuid4

from app.database import SessionLocal
from app.models import EmailInstance, Project, User, Workspace
from app.models.campaigns import Campaign
from app.services.capability_registry import CapabilityError, CapabilityRegistry
from app.services.decision_gate_service import DecisionGateError, DecisionGateService
from app.services.email_instance_resolver import resolve_email_instance
from app.services.engine_rollout_service import EngineRolloutService


def main() -> None:
    if not os.getenv("DATABASE_URL", "").startswith("postgresql"):
        raise RuntimeError("This smoke requires an explicitly selected PostgreSQL database")
    db = SessionLocal()
    suffix = uuid4().hex[:10]
    try:
        owner = User(email=f"engine-{suffix}@example.test", name="Engine smoke", is_active=True)
        db.add(owner)
        db.flush()
        workspace = Workspace(name=f"Engine {suffix}", owner_id=owner.id, is_active=True)
        other_workspace = Workspace(name=f"Other {suffix}", owner_id=owner.id, is_active=True)
        db.add_all([workspace, other_workspace])
        db.flush()
        owner.workspace_id = workspace.id
        project = Project(name=f"Engine {suffix}", workspace_id=workspace.id, is_active=True)
        sibling = Project(name=f"Sibling {suffix}", workspace_id=workspace.id, is_active=True)
        foreign = Project(name=f"Foreign {suffix}", workspace_id=other_workspace.id, is_active=True)
        db.add_all([project, sibling, foreign])
        db.flush()

        service = EngineRolloutService(db)
        errors = service.validate(project.id, [{"feature_key": "guardian", "mode": "enforce"}])
        assert any(error["dependency"] == "ledger" for error in errors)
        service.update(project.id, [
            {"feature_key": "ledger", "mode": "enforce", "expected_version": 0},
            {"feature_key": "guardian", "mode": "enforce", "expected_version": 0},
        ], owner.id)
        assert service.effective_modes(project.id)["guardian"] == "enforce"

        shared = EmailInstance(
            user_id=owner.id, workspace_id=workspace.id, project_id=None,
            provider_type="smtp", instance_name=f"shared-{suffix}",
            from_email=f"shared-{suffix}@example.test", connection_status="verified",
            is_active=True,
        )
        scoped = EmailInstance(
            user_id=owner.id, workspace_id=workspace.id, project_id=project.id,
            provider_type="smtp", instance_name=f"scoped-{suffix}",
            from_email=f"scoped-{suffix}@example.test", connection_status="verified",
            is_active=True,
        )
        db.add_all([shared, scoped])
        db.commit()
        assert resolve_email_instance(db, project_id=sibling.id, instance_id=shared.id)
        assert resolve_email_instance(db, project_id=sibling.id, instance_id=scoped.id) is None
        assert resolve_email_instance(db, project_id=foreign.id, instance_id=shared.id) is None

        payload = {"updates": [
            {"feature_key": "ledger", "mode": "shadow", "expected_version": 0},
        ]}
        registry = CapabilityRegistry(db)
        try:
            registry.invoke(
                project_id=sibling.id, key="engine.rollout", phase="propose",
                payload=payload, actor_user_id=owner.id,
                idempotency_key=f"proposal-no-sim-{suffix}",
            )
            raise AssertionError("Proposal without simulation was accepted")
        except CapabilityError:
            pass
        simulated = registry.invoke(
            project_id=sibling.id, key="engine.rollout", phase="simulate",
            payload=payload, actor_user_id=owner.id,
            idempotency_key=f"simulation-{suffix}",
        )
        proposed = registry.invoke(
            project_id=sibling.id, key="engine.rollout", phase="propose",
            payload={**payload, "simulation_execution_id": simulated["capability_execution_id"]},
            actor_user_id=owner.id, idempotency_key=f"proposal-{suffix}",
        )
        choice = DecisionGateService(db).choose(
            sibling.id, proposed["evaluation_id"], "apply_rollout", owner.id,
            "Smoke approval",
        )
        executed = registry.invoke(
            project_id=sibling.id, key="engine.rollout", phase="execute",
            payload={**payload, "proposal_execution_id": proposed["capability_execution_id"]},
            actor_user_id=owner.id, idempotency_key=f"execute-{suffix}",
            decision_choice_id=choice.id,
        )
        assert any(
            item["feature_key"] == "ledger" and item["effective_mode"] == "shadow"
            for item in executed["features"]
        )
        campaign = db.query(Campaign).filter(
            Campaign.name == "Feature announcement",
        ).order_by(Campaign.id.desc()).first()
        if campaign:
            evaluation = DecisionGateService(db).evaluate_campaign(campaign, {
                "mode": "start_forward",
                "start_at": "2026-08-25T12:00:00",
                "timezone": "America/Sao_Paulo",
            }, owner.id)
            assert evaluation.method == "exact"
            assert evaluation.provenance["audience"] == "exact"
            campaign.version += 1
            db.commit()
            try:
                DecisionGateService(db).choose(
                    campaign.project_id, evaluation.id, "protect_reputation", owner.id,
                )
                raise AssertionError("Changed campaign reused a stale decision")
            except DecisionGateError:
                pass
        print("engine-program-smoke-ok")
    finally:
        db.close()


if __name__ == "__main__":
    main()
