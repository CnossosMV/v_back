import asyncio
from datetime import datetime, timedelta
from itertools import product
from types import SimpleNamespace

import pytest

from app.schemas.orchestration_attention import AttentionPolicyInput
from app.models import Project, SendLog, User, Workspace
from app.models.engine_control import ProjectEngineRollout
from app.models.messaging import MessagingTemplate, MessagingUser
from app.services.campaigns.worker import CampaignWorker
from app.services.channels.selection import (
    arbitrate_due_attention,
    attention_lock_id,
    due_attention_contest,
    eligible_candidates,
    group_by_contact,
    rank_candidates,
)
from app.services.channels.future_attention_planner import FutureAttentionPlanner
from app.services.channels.base import OutboundContent
from app.services.channels.lanes import (
    AttentionParticipation,
    OUTBOUND_SOURCE_CONTRACTS,
    OutboundSourceContractError,
    require_source_contract,
    validate_source_registry,
)
from app.services.channels.send_service import SendService
from app.services.channels.send_log_helper import record_direct_send
from app.services.channels.source_contract import resolve_dispatch_source
from app.services.messaging.event_processor import EventProcessor
from app.services.orchestration_impact_service import (
    OrchestrationImpactError,
    OrchestrationImpactService,
    _Asset,
    _relationship,
)


def policy(**overrides):
    values = {
        "attention_scope": "contact.promotional",
        "ordinal": 50,
        "ordinal_reason": "Declared product priority",
        "entry_effect": "occlude",
        "allowed_incoming_effects": ["occlude", "coexist"],
        "tie_policy": "require_order",
        "missed_window": "expire",
    }
    values.update(overrides)
    return AttentionPolicyInput(**values).model_dump(mode="json")


def asset(
    ref_id,
    authored_policy,
    *,
    purpose_key=None,
    kind="funnel",
    active=True,
    event_name=None,
    event_conditions=(),
):
    return _Asset(
        kind=kind,
        id=ref_id,
        project_id=1,
        name=f"asset-{ref_id}",
        active=active,
        status="active" if active else "draft",
        purpose_key=purpose_key,
        trigger_signature=None,
        definition_version="1",
        policy=authored_policy,
        policy_error=None,
        event_name=event_name,
        event_conditions=tuple(event_conditions),
    )


@pytest.mark.parametrize(
    "candidate_policy,existing_policy,candidate_purpose,existing_purpose,expected_action,expected_attention,expected_reason",
    [
        (
            policy(attention_scope="contact.promotional"),
            policy(attention_scope="contact.transactional"),
            "nurture", "receipt", "coexist", "independent_scope", "different_attention_scope",
        ),
        (
            policy(attention_scope="contact.promotional", entry_effect="exit", target_purpose_keys=["onboarding"]),
            policy(attention_scope="contact.transactional", allowed_incoming_effects=["coexist", "exit"]),
            "nurture", "onboarding", "exit", "independent_scope", "declared_cross_scope_incompatibility",
        ),
        (
            policy(attention_scope="contact.promotional", entry_effect="exit", target_purpose_keys=["onboarding"]),
            policy(attention_scope="contact.transactional"),
            "nurture", "onboarding", "reject_entry", "entry_rejected", "existing_episode_disallows_exit",
        ),
        (
            policy(ordinal=40), policy(ordinal=80),
            "weekly", "month_end", "candidate_occluded", "candidate_occluded", "existing_ordinal_is_higher",
        ),
        (
            policy(ordinal=40, allow_start_occluded=False), policy(ordinal=80),
            "weekly", "month_end", "reject_entry", "entry_rejected", "candidate_disallows_occluded_start",
        ),
        (
            policy(ordinal=40, entry_effect="exit", target_purpose_keys=["month_end"]),
            policy(ordinal=80, allowed_incoming_effects=["coexist", "exit"]),
            "weekly", "month_end", "unresolved", "candidate_occluded", "destructive_entry_effect_cannot_displace_higher_ordinal",
        ),
        (
            policy(ordinal=80), policy(ordinal=40),
            "month_end", "weekly", "occlude", "candidate_selected", "candidate_ordinal_is_higher",
        ),
        (
            policy(ordinal=80, entry_effect="exit", target_purpose_keys=["weekly"]),
            policy(ordinal=40, allowed_incoming_effects=["coexist", "occlude", "exit"]),
            "month_end", "weekly", "exit", "candidate_selected", "declared_incompatible_episode",
        ),
        (
            policy(ordinal=80, entry_effect="exit", target_purpose_keys=["weekly"]),
            policy(ordinal=40),
            "month_end", "weekly", "reject_entry", "entry_rejected", "existing_episode_disallows_exit",
        ),
        (
            policy(ordinal=50), policy(ordinal=50),
            "a", "b", "unresolved", "unresolved_tie", "equal_ordinal_requires_order",
        ),
        (
            policy(ordinal=50, tie_policy="perishability"),
            policy(ordinal=50, tie_policy="perishability"),
            "a", "b", "tie", "delegated_tie", "delegated_to_perishability",
        ),
        (
            policy(ordinal=50, tie_policy="perishability"),
            policy(ordinal=50, tie_policy="bounded_learning"),
            "a", "b", "unresolved", "unresolved_tie", "equal_ordinal_tie_policies_disagree",
        ),
        (
            policy(ordinal=90),
            policy(ordinal=20, entry_effect="reject_entry", target_purpose_keys=["urgent_offer"]),
            "urgent_offer", "protected", "reject_entry", "entry_rejected", "existing_episode_rejects_candidate",
        ),
        (
            policy(ordinal=90, entry_effect="reject_entry", target_purpose_keys=["protected"]),
            policy(ordinal=20),
            "urgent_offer", "protected", "reject_entry", "entry_rejected", "declared_incompatible_episode_active",
        ),
    ],
)
def test_attention_relationship_matrix(
    candidate_policy,
    existing_policy,
    candidate_purpose,
    existing_purpose,
    expected_action,
    expected_attention,
    expected_reason,
):
    result = _relationship(
        asset(1, candidate_policy, purpose_key=candidate_purpose),
        asset(2, existing_policy, purpose_key=existing_purpose),
    )
    assert result["entry_action"] == expected_action
    assert result["attention_decision"] == expected_attention
    assert result["reason"] == expected_reason


def test_attention_policy_normalizes_sets_and_requires_destructive_targets():
    normalized = policy(
        target_purpose_keys=["b", "a", "b"],
        allowed_incoming_effects=["coexist", "occlude", "coexist"],
    )
    assert normalized["target_purpose_keys"] == ["a", "b"]
    assert normalized["allowed_incoming_effects"] == ["coexist", "occlude"]
    with pytest.raises(ValueError):
        policy(entry_effect="exit")


def test_outbound_source_registry_is_complete_and_explicit_about_exemptions():
    validate_source_registry()
    assert set(OUTBOUND_SOURCE_CONTRACTS) >= {
        "campaign", "event_action", "funnel", "template",
        "chatbot", "agent_team", "manual",
    }
    for source_type in {"campaign", "event_action", "funnel", "template"}:
        contract = require_source_contract(source_type)
        assert contract.attention_participation == AttentionParticipation.REQUIRED_WHEN_PROMOTIONAL
        assert contract.activation_kind == source_type
        assert contract.requires_authored_attention is True
    assert require_source_contract("chatbot").exemption_reason
    with pytest.raises(OutboundSourceContractError):
        require_source_contract("future_automation_that_skipped_registration")


def test_unknown_outbound_source_is_blocked_before_provider_io():
    decision = asyncio.run(SendService(None).send(
        project_id=1,
        user_id=None,
        recipient="nobody@example.test",
        content=OutboundContent(content_type="text", text="must not send"),
        channel="email",
        source_type="unregistered_worker",
        dry_run=True,
    ))
    assert decision.status == "blocked"
    assert decision.success is False
    assert "Unregistered outbound source_type" in decision.error


def test_promotional_source_requires_authored_identity_and_enforced_runtime(monkeypatch):
    import app.services.channels.selection as selection
    import app.services.channels.source_contract as source_contract

    monkeypatch.setattr(
        source_contract,
        "_authored_row",
        lambda *_args, **_kwargs: (policy(ordinal=77), "trial.activation"),
    )
    monkeypatch.setattr(selection, "candidate_mode", lambda *_args: "off")
    monkeypatch.setattr(selection, "selection_mode", lambda *_args: "enforce")
    with pytest.raises(OutboundSourceContractError, match="Candidate and Selection"):
        resolve_dispatch_source(None, 1, "template", 9)

    monkeypatch.setattr(selection, "candidate_mode", lambda *_args: "enforce")
    resolved = resolve_dispatch_source(None, 1, "template", 9)
    assert resolved.intent_fields == {
        "intent_class": "promotional",
        "intent_tier": 77,
        "attention_scope": "contact.promotional",
        "purpose_key": "trial.activation",
        "attention_policy_snapshot": policy(ordinal=77),
    }


def test_event_template_uses_the_same_activation_impact_contract():
    row = SimpleNamespace(
        id=19,
        project_id=1,
        name="Trial expired nurture",
        is_active=True,
        automation_enabled=False,
        purpose_key="trial.reactivation",
        trigger_events=["subscription.trial_expired"],
        attention_policy=policy(ordinal=40),
        updated_at=None,
    )
    report = ImpactWithoutDatabase().preview("template", row)
    assert report["safe_to_activate"] is True
    assert report["candidate"]["trigger_signature"].startswith("events:")


def test_direct_send_logger_rejects_attention_automation_sources():
    with pytest.raises(ValueError, match="cannot use record_direct_send"):
        record_direct_send(
            None,
            project_id=1,
            channel="email",
            recipient="blocked@example.test",
            content_summary="must not bypass",
            source_type="template",
            source_id=9,
        )


def test_triggered_template_is_inert_until_separately_activated(db_session):
    owner = User(email="template-gate@example.test", name="Template Gate", is_active=True)
    db_session.add(owner)
    db_session.flush()
    workspace = Workspace(name="Template gate workspace", owner_id=owner.id, is_active=True)
    db_session.add(workspace)
    db_session.flush()
    project = Project(
        name="Template gate project",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="f" * 64,
    )
    db_session.add(project)
    db_session.flush()
    db_session.add_all([
        ProjectEngineRollout(project_id=project.id, feature_key="candidate", mode="enforce"),
        ProjectEngineRollout(project_id=project.id, feature_key="selection", mode="enforce"),
    ])
    template = MessagingTemplate(
        project_id=project.id,
        slug="trial-expired-gate",
        name="Trial expired gate",
        body="Come back",
        trigger_events=["subscription.trial_expired"],
        purpose_key="trial.reactivation",
        attention_policy=policy(ordinal=45),
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    assert template.automation_enabled is False
    assert EventProcessor().find_triggered_templates(
        db_session, project.id, "subscription.trial_expired",
    ) == []
    with pytest.raises(OutboundSourceContractError, match="enabled authored automation"):
        resolve_dispatch_source(db_session, project.id, "template", template.id)
    impact = OrchestrationImpactService(db_session).preview("template", template)
    assert impact["safe_to_activate"] is True

    template.automation_enabled = True
    db_session.flush()
    assert EventProcessor().find_triggered_templates(
        db_session, project.id, "subscription.trial_expired",
    ) == [template]
    resolved = resolve_dispatch_source(db_session, project.id, "template", template.id)
    assert resolved.intent_fields["purpose_key"] == "trial.reactivation"

    template.is_active = False
    db_session.flush()
    with pytest.raises(OutboundSourceContractError, match="enabled authored automation"):
        resolve_dispatch_source(db_session, project.id, "template", template.id)


class ImpactWithoutDatabase(OrchestrationImpactService):
    def __init__(self, existing=None):
        self.db = None
        self.existing = existing or []

    def _assets(self, project_id):
        return list(self.existing)

    def _materialized_impact(self, impacts):
        return {
            "active_funnel_enrollments": 0,
            "active_campaign_recipients": 0,
            "pending_event_actions": 0,
            "per_asset": [],
        }


def test_activation_approval_is_bound_to_exact_impact_fingerprint():
    row = SimpleNamespace(
        id=10,
        project_id=1,
        name="Candidate",
        status="draft",
        purpose_key="nurture",
        trigger_type="segment",
        trigger_config={},
        attention_policy=policy(ordinal=60),
        updated_at=None,
    )
    service = ImpactWithoutDatabase()
    preview = service.preview("funnel", row)
    assert preview["safe_to_activate"] is True
    approved = service.ensure_approved("funnel", row, preview["impact_fingerprint"])
    assert approved["impact_fingerprint"] == preview["impact_fingerprint"]
    with pytest.raises(OrchestrationImpactError) as stale:
        service.ensure_approved("funnel", row, "0" * 64)
    assert stale.value.report["blockers"][-1]["code"] == "attention_impact_approval_required"


def test_activation_gate_blocks_same_purpose_double_owner():
    existing = asset(2, policy(ordinal=20), purpose_key="nurture")
    row = SimpleNamespace(
        id=10,
        project_id=1,
        name="Replacement",
        status="draft",
        purpose_key="nurture",
        trigger_type="segment",
        trigger_config={},
        attention_policy=policy(ordinal=60),
        updated_at=None,
    )
    report = ImpactWithoutDatabase([existing]).preview("funnel", row)
    assert report["safe_to_activate"] is False
    assert "purpose_owner_overlap" in {item["code"] for item in report["blockers"]}


def test_activation_gate_allows_disjoint_event_action_variants_to_share_purpose():
    existing = asset(
        23,
        policy(attention_scope="contact.transactional", ordinal=110, entry_effect="coexist"),
        purpose_key="aha.flyer_ready",
        kind="event_action",
        event_name="aha.flyer_ready",
        event_conditions=({"field": "lang", "operator": "==", "value": "pt"},),
    )
    row = SimpleNamespace(
        id=24,
        project_id=1,
        name="AHA flyer ready EN",
        is_active=False,
        lane="transactional",
        trigger_event="aha.flyer_ready",
        conditions=[{"field": "lang", "operator": "==", "value": "en"}],
        purpose_key="aha.flyer_ready",
        attention_policy=policy(
            attention_scope="contact.transactional",
            ordinal=110,
            entry_effect="coexist",
        ),
        updated_at=None,
    )

    report = ImpactWithoutDatabase([existing]).preview("event_action", row)

    assert report["safe_to_activate"] is True
    assert report["blockers"] == []
    assert report["impacts"][0]["overlap_certainty"] == "proven_disjoint_event_conditions"
    assert report["impacts"][0]["reason"] == "event_conditions_proven_disjoint"


def test_activation_gate_keeps_overlapping_event_action_variants_blocked():
    existing = asset(
        6,
        policy(ordinal=96),
        purpose_key="checkout.recovery",
        kind="event_action",
        event_name="checkout.abandoned",
        event_conditions=({"field": "country", "operator": "==", "value": "BR"},),
    )
    row = SimpleNamespace(
        id=7,
        project_id=1,
        name="Checkout recovery WhatsApp",
        is_active=False,
        lane="promotional",
        trigger_event="checkout.abandoned",
        conditions=[{"field": "country", "operator": "==", "value": "BR"}],
        purpose_key="checkout.recovery",
        attention_policy=policy(ordinal=95),
        updated_at=None,
    )

    report = ImpactWithoutDatabase([existing]).preview("event_action", row)

    assert report["safe_to_activate"] is False
    assert "purpose_owner_overlap" in {item["code"] for item in report["blockers"]}


def test_selection_groups_same_contact_by_attention_scope_and_ranks_authored_ordinal():
    rows = [
        SimpleNamespace(id=1, user_id=7, recipient="x", attention_scope="contact.promotional", intent_tier=20, expires_at=None, scheduled_at=None),
        SimpleNamespace(id=2, user_id=7, recipient="x", attention_scope="contact.promotional", intent_tier=80, expires_at=None, scheduled_at=None),
        SimpleNamespace(id=3, user_id=7, recipient="x", attention_scope="contact.transactional", intent_tier=10, expires_at=None, scheduled_at=None),
    ]
    groups = group_by_contact(rows)
    assert len(groups) == 2
    promotional = groups[("u", 7, "contact.promotional")]
    assert rank_candidates(promotional)["winner"].id == 2


def test_attention_relationship_cross_product_is_total_and_preserves_invariants():
    """Exercise the combinatorial contract, not only hand-picked examples."""
    effects = ["occlude", "suspend", "exit", "reject_entry", "coexist"]
    allowed_sets = [
        [],
        ["coexist"],
        ["occlude", "coexist"],
        ["suspend", "exit", "occlude", "coexist"],
    ]
    ordinals = [(20, 80), (80, 20), (50, 50)]
    scopes = [("contact.promotional", "contact.promotional"),
              ("contact.promotional", "contact.transactional")]
    valid_actions = {
        "coexist", "reject_entry", "exit", "suspend",
        "candidate_occluded", "occlude", "tie", "unresolved",
    }
    valid_attention = {
        "independent_scope", "entry_rejected", "candidate_occluded",
        "candidate_selected", "delegated_tie", "unresolved_tie", "unresolved",
    }

    for candidate_effect, existing_effect, allowed, ordinal_pair, scope_pair in product(
        effects, effects, allowed_sets, ordinals, scopes,
    ):
        candidate_targets = (
            ["existing"]
            if candidate_effect in {"suspend", "exit", "reject_entry"}
            else []
        )
        existing_targets = (
            ["candidate"]
            if existing_effect in {"suspend", "exit", "reject_entry"}
            else []
        )
        candidate_policy = policy(
            ordinal=ordinal_pair[0],
            attention_scope=scope_pair[0],
            entry_effect=candidate_effect,
            target_purpose_keys=candidate_targets,
            allowed_incoming_effects=allowed,
            tie_policy="perishability",
        )
        existing_policy = policy(
            ordinal=ordinal_pair[1],
            attention_scope=scope_pair[1],
            entry_effect=existing_effect,
            target_purpose_keys=existing_targets,
            allowed_incoming_effects=allowed,
            tie_policy="perishability",
        )
        result = _relationship(
            asset(1, candidate_policy, purpose_key="candidate"),
            asset(2, existing_policy, purpose_key="existing"),
        )

        assert result["entry_action"] in valid_actions
        assert result["attention_decision"] in valid_attention
        assert result["existing_episode_effect"] in {"preserve", "exit", "suspend"}
        assert result["reason"]
        if result["existing_episode_effect"] in {"exit", "suspend"}:
            assert candidate_effect == result["existing_episode_effect"]
            assert result["existing_episode_effect"] in allowed
        if scope_pair[0] == scope_pair[1] and ordinal_pair[0] < ordinal_pair[1]:
            assert result["existing_episode_effect"] == "preserve"
        if result["entry_action"] == "reject_entry":
            assert result["existing_episode_effect"] == "preserve"


def test_selection_lock_identity_and_expiration_are_deterministic():
    first = attention_lock_id(1, 7, "ignored-a", "contact.promotional")
    second = attention_lock_id(1, 7, "ignored-b", "contact.promotional")
    other_scope = attention_lock_id(1, 7, "ignored-a", "contact.transactional")
    assert first == second
    assert first != other_scope
    assert -(2 ** 63) <= first < 2 ** 63

    now = datetime.utcnow()
    rows = [
        SimpleNamespace(id=1, expires_at=now - timedelta(seconds=1)),
        SimpleNamespace(id=2, expires_at=now + timedelta(seconds=1)),
        SimpleNamespace(id=3, expires_at=None),
    ]
    assert [row.id for row in eligible_candidates(rows, now)] == [2, 3]


def test_future_attention_is_tenant_authored_bounded_and_revalidated(db_session):
    owner = User(email="future-owner@example.test", name="Future owner", is_active=True)
    db_session.add(owner)
    db_session.flush()
    workspace = Workspace(name="Future workspace", owner_id=owner.id, is_active=True)
    db_session.add(workspace)
    db_session.flush()
    project = Project(
        name="Future project",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="d" * 64,
    )
    db_session.add(project)
    db_session.flush()
    contact = MessagingUser(
        project_id=project.id,
        external_id="future-contact",
        email="future-contact@example.test",
    )
    db_session.add(contact)
    db_session.flush()
    rollout = ProjectEngineRollout(
        project_id=project.id,
        feature_key="future_plan",
        mode="shadow",
        config={
            "horizon_minutes": 10080,
            "collision_window_minutes": 2880,
            "recheck_minutes": 30,
            "max_candidates_per_contact": 100,
        },
    )
    db_session.add(rollout)
    now = datetime.utcnow()
    due = SendLog(
        project_id=project.id,
        user_id=contact.id,
        channel="email",
        recipient=contact.email,
        content_type="rich",
        source_type="funnel",
        source_id=1,
        status="candidate",
        scheduled_at=now - timedelta(minutes=1),
        attention_scope="contact.promotional",
        purpose_key="base.nurture",
        intent_tier=20,
        attention_policy_snapshot=policy(ordinal=20, future_reservation=False),
    )
    month_end = SendLog(
        project_id=project.id,
        user_id=contact.id,
        channel="email",
        recipient=contact.email,
        content_type="rich",
        source_type="campaign",
        source_id=2,
        status="delayed",
        scheduled_at=now + timedelta(days=1),
        attention_scope="contact.promotional",
        purpose_key="month_end.offer",
        intent_tier=80,
        attention_policy_snapshot=policy(
            ordinal=80,
            ordinal_reason="Month boundary outranks an ordinary weekday offer",
            future_reservation=True,
        ),
    )
    db_session.add_all([due, month_end])
    db_session.flush()

    shadow = arbitrate_due_attention(db_session, [due], now, lock_future=False)
    assert shadow["dispatch_winner"].id == due.id
    assert shadow["attention_owner"].id == month_end.id
    assert shadow["consequence_at_gate"] == "would_hold_due_for_future_reservation"

    report = FutureAttentionPlanner(db_session).preview_contact(
        project.id, user_id=contact.id, as_of=now,
    )
    assert report["external_sends"] == 0
    assert report["windows"][0]["consequence_at_gate"] == "would_hold_due_for_future_reservation"
    assert next(
        item for item in report["windows"][0]["candidates"]
        if item["send_log_id"] == month_end.id
    )["ordinal_reason"].startswith("Month boundary")
    assert report["past_context"]["available"] is True
    consequence = FutureAttentionPlanner(db_session).configuration_consequence(
        project.id,
        mode="enforce",
        config=report["tenant_policy"],
    )
    assert consequence["future_reservation_intents"] == 1
    assert consequence["external_sends"] == 0

    rollout.mode = "enforce"
    db_session.flush()
    enforced = arbitrate_due_attention(db_session, [due], now, lock_future=False)
    assert enforced["dispatch_winner"] is None
    assert enforced["attention_owner"].id == month_end.id
    assert now < enforced["hold_until"] <= now + timedelta(minutes=30)
    assert enforced["consequence_at_gate"] == "hold_due_for_future_reservation"

    month_end.attention_policy_snapshot = policy(
        ordinal=80,
        ordinal_reason="Visible future opportunity without an early hold",
        future_reservation=False,
    )
    db_session.flush()
    no_opt_in = arbitrate_due_attention(db_session, [due], now, lock_future=False)
    assert no_opt_in["dispatch_winner"].id == due.id
    assert no_opt_in["consequence_at_gate"] == "dispatch_due_winner"


def test_activation_consequence_requires_enforced_future_runtime(db_session):
    owner = User(email="future-gate@example.test", name="Future gate", is_active=True)
    db_session.add(owner)
    db_session.flush()
    workspace = Workspace(name="Future gate workspace", owner_id=owner.id, is_active=True)
    db_session.add(workspace)
    db_session.flush()
    project = Project(name="Future gate project", workspace_id=workspace.id, is_active=True)
    db_session.add(project)
    db_session.flush()
    db_session.add_all([
        ProjectEngineRollout(project_id=project.id, feature_key="candidate", mode="enforce"),
        ProjectEngineRollout(project_id=project.id, feature_key="selection", mode="enforce"),
        ProjectEngineRollout(project_id=project.id, feature_key="future_plan", mode="shadow"),
    ])
    db_session.flush()
    row = SimpleNamespace(
        id=99,
        project_id=project.id,
        name="Future-aware nurture",
        status="draft",
        purpose_key="future.nurture",
        trigger_type="segment",
        trigger_config={},
        attention_policy=policy(ordinal=70, future_reservation=True),
        updated_at=None,
    )
    service = OrchestrationImpactService(db_session)
    service._assets = lambda _project_id: []
    service._materialized_impact = lambda _impacts: {
        "active_funnel_enrollments": 0,
        "active_campaign_recipients": 0,
        "pending_event_actions": 0,
        "pending_template_sends": 0,
        "per_asset": [],
    }
    blocked = service.preview("funnel", row)
    assert "future_attention_runtime_required" in {
        item["code"] for item in blocked["blockers"]
    }
    assert blocked["consequence_at_gate"]["decision"] == "blocked"

    rollout = db_session.query(ProjectEngineRollout).filter(
        ProjectEngineRollout.project_id == project.id,
        ProjectEngineRollout.feature_key == "future_plan",
    ).one()
    rollout.mode = "enforce"
    db_session.flush()
    allowed = service.preview("funnel", row)
    assert allowed["safe_to_activate"] is True
    assert allowed["consequence_at_gate"]["future_attention"]["mode"] == "enforce"


def test_campaign_reservation_reenters_selection_when_dispatch_is_postponed(
    db_session, monkeypatch,
):
    owner = User(email="attention-owner@example.test", name="Attention Owner", is_active=True)
    db_session.add(owner)
    db_session.flush()
    workspace = Workspace(name="Attention workspace", owner_id=owner.id, is_active=True)
    db_session.add(workspace)
    db_session.flush()
    project = Project(
        name="Attention project",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="e" * 64,
    )
    db_session.add(project)
    db_session.flush()
    now = datetime.utcnow()
    selected = SendLog(
        project_id=project.id,
        user_id=None,
        channel="email",
        recipient="attention@example.test",
        content_type="rich",
        source_type="campaign",
        source_id=77,
        status="campaign_selected",
        scheduled_at=now,
        attention_scope="contact.promotional",
        intent_tier=60,
    )
    contender = SendLog(
        project_id=project.id,
        user_id=None,
        channel="email",
        recipient="attention@example.test",
        content_type="rich",
        source_type="event_action",
        source_id=88,
        status="candidate",
        scheduled_at=now,
        attention_scope="contact.promotional",
        intent_tier=80,
    )
    expired = SendLog(
        project_id=project.id,
        user_id=None,
        channel="email",
        recipient="attention@example.test",
        content_type="rich",
        source_type="funnel",
        source_id=99,
        status="candidate",
        scheduled_at=now,
        expires_at=now - timedelta(seconds=1),
        attention_scope="contact.promotional",
        intent_tier=100,
    )
    db_session.add_all([selected, contender, expired])
    db_session.flush()

    contest = eligible_candidates(
        due_attention_contest(db_session, selected, now, lock=False),
        now,
    )
    assert {row.id for row in contest} == {selected.id, contender.id}
    assert rank_candidates(contest)["winner"].id == contender.id

    retry_at = now + timedelta(minutes=15)
    recipient = SimpleNamespace(
        id=77,
        project_id=project.id,
        send_log_id=selected.id,
    )
    CampaignWorker()._return_attention_to_selection(
        db_session, recipient, retry_at, "capacity_wait",
    )
    db_session.refresh(selected)
    assert selected.status == "candidate"
    assert selected.scheduled_at == retry_at
    assert selected.decision_trace[-1]["step"] == "campaign_attention_released"

    # CampaignWorker must perform the same contest again at the provider
    # boundary: a reservation wins while it remains highest, then is revoked
    # if a stronger contender appears before ``submitting`` is committed.
    import app.services.channels.selection as selection
    monkeypatch.setattr(selection, "candidate_mode", lambda *_args: "enforce")
    monkeypatch.setattr(selection, "selection_mode", lambda *_args: "enforce")
    worker = CampaignWorker()
    monkeypatch.setattr(
        worker,
        "_requeue_existing_deferral",
        lambda db, _recipient, _log, _reason=None: db.flush(),
    )
    selected.status = "campaign_selected"
    selected.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
    selected.intent_tier = 90
    contender.intent_tier = 80
    db_session.flush()
    assert worker._confirm_attention_selection(db_session, recipient, selected) is True
    assert selected.decision_trace[-1]["step"] == "campaign_selection_revalidated"

    contender.intent_tier = 100
    db_session.flush()
    assert worker._confirm_attention_selection(db_session, recipient, selected) is False
    assert selected.status == "candidate"
    assert selected.decision_trace[-1]["step"] == "campaign_selection_revalidation_lost"
