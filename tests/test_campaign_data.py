"""Contract tests for provider-neutral contact and campaign persistence."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app.models.campaigns import (
    CampaignPolicyOverride,
    CampaignRecipient,
    CampaignRun,
    CampaignWave,
    ContactEndpoint,
    ContactGroupMembership,
    ContactPermissionEvidence,
)
from app.models.messaging import ContactVerificationState
from app.routers.campaigns import _campaign_dict
from app.schemas.campaigns import (
    CampaignRunResponse,
    CampaignResponse,
    CampaignWaveResponse,
    ContactEndpointResponse,
    ContactPermissionEvidenceResponse,
)


def _now() -> datetime:
    return datetime(2026, 8, 22, 12, 0, 0)


def test_campaign_response_aggregates_all_run_counters():
    campaign = SimpleNamespace(
        id=1,
        project_id=10,
        name="Aggregate contract",
        description=None,
        campaign_type="one_off",
        status="active",
        default_channel="email",
        selection_config={},
        policy_config={},
        recurrence_config=None,
        timezone="UTC",
        starts_at=None,
        target_at=None,
        external_key=None,
        version=2,
        created_by_user_id=20,
        created_at=_now(),
        updated_at=_now(),
        actions=[],
        runs=[
            SimpleNamespace(
                candidate_count=10,
                eligible_count=8,
                sent_count=6,
                delivered_count=5,
                failed_count=1,
            ),
            SimpleNamespace(
                candidate_count=4,
                eligible_count=3,
                sent_count=2,
                delivered_count=2,
                failed_count=0,
            ),
        ],
    )

    payload = _campaign_dict(campaign)
    response = CampaignResponse.model_validate(payload)

    assert response.audience_count == 14
    assert response.eligible_count == 11
    assert response.sent_count == 8
    assert response.delivered_count == 7
    assert response.failed_count == 1


def test_contact_endpoints_preserve_primary_and_secondary_values():
    primary = ContactEndpoint(
        id=1,
        project_id=10,
        user_id=20,
        endpoint_type="email",
        value="primary@example.com",
        normalized_value="primary@example.com",
        value_hash="a" * 64,
        is_primary=True,
        source="migration",
    )
    secondary = ContactEndpoint(
        id=2,
        project_id=10,
        user_id=20,
        endpoint_type="email",
        value="secondary@example.com",
        normalized_value="secondary@example.com",
        value_hash="b" * 64,
        is_primary=False,
        source="manual",
    )

    assert primary.is_primary is True
    assert secondary.is_primary is False
    assert primary.user_id == secondary.user_id
    assert primary.value_hash != secondary.value_hash


def test_permission_evidence_can_target_an_endpoint():
    evidence = ContactPermissionEvidence(
        id=1,
        project_id=10,
        user_id=20,
        endpoint_id=2,
        channel="email",
        permission_type="marketing",
        status="granted",
        source="import",
        captured_at=_now(),
    )

    assert evidence.endpoint_id == 2
    assert evidence.status == "granted"


def test_group_membership_is_not_exclusive_across_groups():
    memberships = [
        ContactGroupMembership(project_id=10, group_id=100, user_id=20),
        ContactGroupMembership(project_id=10, group_id=200, user_id=20),
    ]
    unique = next(
        constraint for constraint in ContactGroupMembership.__table__.constraints
        if constraint.name == "uq_contact_group_member"
    )

    assert [membership.group_id for membership in memberships] == [100, 200]
    assert {column.name for column in unique.columns} == {"project_id", "group_id", "user_id"}


def test_models_expose_endpoint_aware_verification_uniqueness_and_wave_approval():
    state_constraints = {
        constraint.name
        for constraint in ContactVerificationState.__table__.constraints
        if constraint.name
    }
    state_indexes = {
        index.name: index
        for index in ContactVerificationState.__table__.indexes
    }

    assert "uq_contact_verify_state" not in state_constraints
    assert "uq_contact_verify_state_ep" in state_indexes
    assert "uq_contact_verify_state_legacy" in state_indexes
    assert "endpoint_id" in ContactVerificationState.__table__.columns
    assert "endpoint_id" in CampaignRecipient.__table__.columns
    assert {"approval_mode", "is_canary", "approved_at", "approved_by_user_id"}.issubset(
        CampaignWave.__table__.columns.keys()
    )

    endpoint_predicate = str(
        state_indexes["uq_contact_verify_state_ep"].dialect_options["postgresql"]["where"]
    )
    legacy_predicate = str(
        state_indexes["uq_contact_verify_state_legacy"].dialect_options["postgresql"]["where"]
    )
    assert "endpoint_id IS NOT NULL" in endpoint_predicate
    assert "endpoint_id IS NULL" in legacy_predicate


def test_pydantic_orm_aliases_read_reserved_metadata_columns():
    endpoint = ContactEndpointResponse.model_validate(
        SimpleNamespace(
            id=1,
            project_id=10,
            user_id=20,
            endpoint_type="email",
            value="a@example.com",
            normalized_value="a@example.com",
            value_hash="a" * 64,
            is_primary=True,
            status="active",
            source="manual",
            endpoint_metadata={"country": "BR"},
            first_seen_at=_now(),
            last_seen_at=_now(),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    evidence = ContactPermissionEvidenceResponse.model_validate(
        SimpleNamespace(
            id=2,
            project_id=10,
            user_id=20,
            endpoint_id=1,
            channel="email",
            permission_type="marketing",
            status="granted",
            source="form",
            policy_version=None,
            evidence_ref=None,
            captured_at=_now(),
            expires_at=None,
            evidence_metadata={"form_id": "abc"},
            created_at=_now(),
        )
    )

    assert endpoint.metadata == {"country": "BR"}
    assert evidence.metadata == {"form_id": "abc"}


def test_wave_schema_exposes_canary_and_approval_state():
    wave = CampaignWaveResponse.model_validate(
        SimpleNamespace(
            id=1,
            project_id=10,
            run_id=30,
            position=0,
            status="held",
            approval_mode="manual",
            is_canary=True,
            approved_at=None,
            approved_by_user_id=None,
            scheduled_at=None,
            window_start=None,
            window_end=None,
            planned_count=10,
            queued_count=0,
            sent_count=0,
            failed_count=0,
            skipped_count=0,
            error_message=None,
            created_at=_now(),
            started_at=None,
            finished_at=None,
        )
    )

    assert wave.is_canary is True
    assert wave.approval_mode == "manual"


def test_campaign_migration_metadata_and_requested_tables_are_provider_neutral():
    migration = Path(__file__).parents[1] / "alembic" / "versions" / "138_campaign_data.py"
    source = migration.read_text(encoding="utf-8")

    assert 'revision = "138_campaign_data"' in source
    assert 'down_revision = "137_contact_verify"' in source
    assert 'postgresql.JSONB' in source
    assert "sa.Enum" not in source
    for table in (
        "contact_endpoints",
        "contact_permission_evidence",
        "contact_groups",
        "contact_group_memberships",
        "campaigns",
        "campaign_actions",
        "campaign_variants",
        "campaign_runs",
        "campaign_waves",
        "campaign_recipients",
        "channel_sender_identities",
        "channel_delivery_profiles",
        "channel_capacity_reservations",
        "contact_verification_requests",
        "operational_alerts",
    ):
        assert f'"{table}"' in source
    assert "endpoint_id IS NOT NULL" in source
    assert "endpoint_id IS NULL" in source
    assert "approval_mode" in source
    assert "is_canary" in source
    assert "_PROJECT_PARENT_UNIQUES" in source
    assert "_PROJECT_SCOPED_FOREIGN_KEYS" in source
    for constraint in (
        "fk_endpoint_user_project",
        "fk_group_member_group_project",
        "fk_campaign_run_campaign_project",
        "fk_campaign_wave_run_project",
        "fk_campaign_recipient_endpoint_project",
        "fk_capacity_profile_project",
        "fk_verify_request_job_project",
        "fk_operational_alert_project_workspace",
    ):
        assert constraint in source


def test_campaign_override_is_run_scoped_and_exposed_by_the_run_schema():
    override = SimpleNamespace(
        id=5,
        project_id=10,
        run_id=30,
        status="approved",
        override_keys=["channel_cooldown"],
        reason="Audited urgent operational exception",
        risk_acknowledged=True,
        dual_approval_required=False,
        planned_count_snapshot=500,
        requested_by_user_id=20,
        approved_by_user_id=20,
        revoked_by_user_id=None,
        requested_at=_now(),
        approved_at=_now(),
        expires_at=_now(),
        revoked_at=None,
    )
    run = SimpleNamespace(
        id=30,
        project_id=10,
        campaign_id=40,
        run_key="audited-run",
        trigger_type="manual",
        status="scheduled",
        audience_snapshot={},
        audience_hash="a" * 64,
        timezone="UTC",
        starts_at=None,
        target_at=None,
        deadline_at=None,
        capacity_plan={},
        candidate_count=500,
        eligible_count=500,
        planned_count=500,
        queued_count=500,
        sent_count=0,
        delivered_count=0,
        failed_count=0,
        skipped_count=0,
        canceled_count=0,
        cancel_requested=False,
        error_message=None,
        requested_by_user_id=20,
        created_at=_now(),
        started_at=None,
        finished_at=None,
        policy_override=override,
    )
    response = CampaignRunResponse.model_validate(run)

    assert response.policy_override is not None
    assert response.policy_override.override_keys == ["channel_cooldown"]
    assert "uq_campaign_run_project_id" in {
        item.name for item in CampaignRun.__table__.constraints if item.name
    }
    assert "fk_campaign_override_run_project" in {
        item.name for item in CampaignPolicyOverride.__table__.constraints if item.name
    }


def test_campaign_override_migration_is_owned_and_fail_closed():
    migration = Path(__file__).parents[1] / "alembic" / "versions" / "139_campaign_override.py"
    source = migration.read_text(encoding="utf-8")

    assert 'revision = "139_campaign_override"' in source
    assert 'down_revision = "138_campaign_data"' in source
    assert '"campaign_policy_overrides"' in source
    assert "fk_campaign_override_run_project" in source
    assert "uq_campaign_run_project_id" in source
    assert "versya:alembic:139_campaign_override:owned" in source
    assert "Refusing to adopt" in source
