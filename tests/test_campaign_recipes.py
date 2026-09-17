from datetime import datetime

import pytest

from app.models import Project, User, Workspace
from app.models.campaigns import Campaign, CampaignRecipeEpisode, CampaignRun
from app.models.project_import import LifecycleModel
from app.schemas.campaign_recipes import CampaignRecipeCreate
from app.services.campaigns.recipes import CampaignRecipeService


class FixedClock:
    def __init__(self, value: datetime):
        self.value = value

    def utcnow(self):
        return self.value


def _project_and_model(db):
    owner = User(email="recipe-owner@example.test", name="Owner", is_active=True)
    db.add(owner)
    db.flush()
    workspace = Workspace(name="Recipe workspace", owner_id=owner.id, is_active=True)
    db.add(workspace)
    db.flush()
    owner.workspace_id = workspace.id
    project = Project(
        name="Recipe project",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="r" * 64,
        default_locale="pt-BR",
        default_timezone="America/Sao_Paulo",
    )
    db.add(project)
    db.flush()
    model = LifecycleModel(
        project_id=project.id,
        version=2,
        name="Lifecycle v2",
        status="shadow",
        definition={"types": []},
        checksum="c" * 64,
    )
    db.add(model)
    db.flush()
    return owner, project, model


def _recipe_data(model_id: int, *, content_mode="agent_draft"):
    data = {
        "external_key": f"expired-trial-{content_mode}",
        "name": "Expired trial nurture",
        "purpose_key": "trial.conversion",
        "lifecycle_model_id": model_id,
        "timezone": "America/Sao_Paulo",
        "country_code": "BR",
        "selection_config": {
            "rule_config": {
                "match": "all",
                "filters": [
                    {"field": "position.type", "operator": "equals", "value": "prospect"},
                    {"field": "position.stage", "operator": "equals", "value": "trial_expired"},
                ],
            }
        },
        "attention_policy": {
            "ordinal": 60,
            "ordinal_reason": "Dated commercial nurture outranks Base",
            "entry_effect": "occlude",
            "exclusive_group": "promotional",
            "tie_policy": "require_order",
            "missed_window": "expire",
        },
        "schedule_rules": [{
            "rule_key": "weekend_offer",
            "name": "Weekend offer",
            "rule_type": "weekly",
            "timezone": "America/Sao_Paulo",
            "country_code": "BR",
            "priority": 60,
            "priority_source": "tenant_policy",
            "priority_reason": "Thursday starts the weekend purchase window",
            "config": {"weekdays": [4], "start_time": "10:00", "duration_hours": 72},
        }],
        "content_mode": content_mode,
        "content_brief": {
            "objective": "Bring expired trial prospects back to create a publication",
            "call_to_action": "Create a publication",
            "locales": ["pt-BR"],
            "required_points": ["Use only current product facts"],
            "forbidden_claims": ["Do not invent a discount"],
        },
        "planning_horizon_days": 14,
        "decision_lead_hours": 72,
    }
    if content_mode == "fixed":
        data["fixed_actions"] = [{
            "channel": "email",
            "variants": [{
                "locale": "pt-BR",
                "subject": "Prepare suas ofertas",
                "body": "Volte ao Tabloide.pro para criar sua próxima publicação.",
            }],
        }]
    return data


def test_recipe_activation_materializes_agent_inbox_without_campaign(db_session):
    owner, project, model = _project_and_model(db_session)
    clock = FixedClock(datetime(2026, 8, 28, 12, 0, 0))
    service = CampaignRecipeService(db_session, clock=clock)
    recipe = service.create(
        project.id,
        CampaignRecipeCreate(**_recipe_data(model.id)),
        owner.id,
    )

    recipe, preview = service.set_state(recipe, "active")
    inbox = service.planning_inbox(project.id, now=clock.utcnow())

    assert preview["can_activate"] is True
    assert inbox["summary"]["awaiting_copy"] >= 1
    assert inbox["items"][0]["recipe"]["lifecycle_model_id"] == model.id
    assert inbox["items"][0]["next_action"] == "author_copy_then_materialize"
    assert db_session.query(Campaign).filter(Campaign.project_id == project.id).count() == 0
    assert inbox["external_sends"] == 0


def test_fixed_recipe_auto_materializes_only_draft_campaign(db_session):
    owner, project, model = _project_and_model(db_session)
    clock = FixedClock(datetime(2026, 8, 28, 12, 0, 0))
    service = CampaignRecipeService(db_session, clock=clock)
    recipe = service.create(
        project.id,
        CampaignRecipeCreate(**_recipe_data(model.id, content_mode="fixed")),
        owner.id,
    )

    service.set_state(recipe, "active")

    episode = db_session.query(CampaignRecipeEpisode).filter(
        CampaignRecipeEpisode.recipe_id == recipe.id,
    ).first()
    campaign = db_session.query(Campaign).filter(Campaign.id == episode.campaign_id).first()
    assert episode.status == "materialized"
    assert episode.campaign is not None
    assert campaign.status == "draft"
    assert campaign.selection_config["lifecycle_model_id"] == model.id
    assert campaign.runs == []


def test_recipe_rejects_fixed_actions_for_agent_draft():
    data = _recipe_data(1)
    data["fixed_actions"] = [{"channel": "email", "variants": [{"body": "Unexpected"}]}]

    with pytest.raises(ValueError, match="fixed_actions"):
        CampaignRecipeCreate(**data)


def test_recipe_contract_explains_zero_send_boundaries():
    contract = CampaignRecipeService.contract()

    assert contract["external_sends"] == 0
    assert "episode materialization never creates a run" in contract["safety_invariants"]
    assert contract["unattended_behavior"]["missed_deadline"].startswith("expire")


def test_recipe_can_always_be_paused_after_its_model_context_changes(db_session):
    owner, project, model = _project_and_model(db_session)
    clock = FixedClock(datetime(2026, 8, 28, 12, 0, 0))
    service = CampaignRecipeService(db_session, clock=clock)
    recipe = service.create(
        project.id,
        CampaignRecipeCreate(**_recipe_data(model.id)),
        owner.id,
    )
    recipe, _preview = service.set_state(recipe, "active")
    model.status = "archived"
    db_session.commit()

    recipe, consequence = service.set_state(recipe, "paused")

    assert recipe.status == "paused"
    assert consequence["can_activate"] is None
    assert consequence["external_sends"] == 0


def test_planning_inbox_reconciles_terminal_campaign_runs(db_session):
    owner, project, model = _project_and_model(db_session)
    clock = FixedClock(datetime(2026, 8, 28, 12, 0, 0))
    service = CampaignRecipeService(db_session, clock=clock)
    recipe = service.create(
        project.id,
        CampaignRecipeCreate(**_recipe_data(model.id, content_mode="fixed")),
        owner.id,
    )
    service.set_state(recipe, "active")
    episode = db_session.query(CampaignRecipeEpisode).filter(
        CampaignRecipeEpisode.recipe_id == recipe.id,
    ).first()
    run = CampaignRun(
        project_id=project.id,
        campaign_id=episode.campaign_id,
        run_key="recipe-terminal-test",
        status="completed",
    )
    db_session.add(run)
    db_session.flush()
    episode.run_id = run.id
    episode.status = "scheduled"
    db_session.commit()

    inbox = service.planning_inbox(project.id)

    db_session.refresh(episode)
    assert episode.status == "completed"
    assert episode.id not in {item["id"] for item in inbox["items"]}
