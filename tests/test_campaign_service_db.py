"""Database integration smoke for group materialization and campaign snapshots."""

from datetime import datetime, timedelta

from app.models import (
    ContactLedger,
    CustomerSMTPConfig,
    EmailInstance,
    Project,
    ProjectPolicy,
    SendLog,
    User,
    Workspace,
)
from app.models.campaigns import (
    Campaign,
    CampaignAction,
    CampaignRun,
    CampaignRecipient,
    ChannelCapacityReservation,
    ContactEndpoint,
    ContactPermissionEvidence,
    ContactVerificationRequest,
)
from app.models.messaging import ContactVerificationState, MessagingUser, ProjectVerificationSettings
from app.services.campaigns.delivery_profiles import DeliveryProfileError, DeliveryProfileService
from app.services.campaigns.policy_overrides import (
    active_override_rules,
    approve_override,
    create_override,
)
from app.services.campaigns.service import CampaignError, CampaignService
from app.services.campaigns.worker import CampaignWorker
from app.services.contact_groups.service import ContactGroupService
from app.services.messaging.contact_profile_service import ContactProfileService
from app.services.scoring.policy_service import PolicyService


def run_campaign_service_smoke(db):
    owner = User(email="campaign-owner@example.test", name="Owner", is_active=True)
    db.add(owner)
    db.flush()
    workspace = Workspace(name="Campaign test", owner_id=owner.id, is_active=True)
    db.add(workspace)
    db.flush()
    owner.workspace_id = workspace.id
    project = Project(
        name="Campaign project",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="a" * 64,
        default_locale="pt-BR",
        default_timezone="America/Sao_Paulo",
    )
    db.add(project)
    db.flush()

    verification_settings = ProjectVerificationSettings(
        project_id=project.id,
        email_auto_verify=True,
        email_verify_on_first_seen=False,
    )
    legacy_only_user = MessagingUser(
        project_id=project.id,
        external_id="legacy-recheck-only",
        email="legacy-recheck-only@example.test",
        status="active",
        is_sandbox=False,
    )
    db.add_all([verification_settings, legacy_only_user])
    db.flush()
    profile_service = ContactProfileService(db)
    profile_service.sync_user(legacy_only_user, source="test")
    assert db.query(ContactVerificationRequest).filter(
        ContactVerificationRequest.user_id == legacy_only_user.id,
    ).count() == 0

    verification_settings.email_verify_on_first_seen = True
    first_seen_user = MessagingUser(
        project_id=project.id,
        external_id="first-seen-idempotent",
        email="first-seen-idempotent@example.test",
        status="active",
        is_sandbox=False,
    )
    db.add(first_seen_user)
    db.flush()
    profile_service.sync_user(first_seen_user, source="test")
    profile_service.sync_user(first_seen_user, source="test")
    assert db.query(ContactEndpoint).filter(
        ContactEndpoint.project_id == project.id,
        ContactEndpoint.user_id == first_seen_user.id,
        ContactEndpoint.endpoint_type == "email",
    ).count() == 1
    assert db.query(ContactVerificationRequest).filter(
        ContactVerificationRequest.user_id == first_seen_user.id,
        ContactVerificationRequest.verification_type == "email",
    ).count() == 1

    users = [
        MessagingUser(
            project_id=project.id,
            external_id=f"campaign-contact-{index}",
            email=f"contact-{index}@example.test",
            email_hash=str(index) * 64,
            locale="pt-BR",
            status="active",
            is_sandbox=False,
            is_subscribed=True,
            global_opt_out=False,
            is_blocked=False,
        )
        for index in (1, 2)
    ]
    db.add_all(users)
    db.flush()
    now = datetime.utcnow()
    endpoints = []
    for index, user in enumerate(users, start=1):
        endpoint = ContactEndpoint(
            project_id=project.id,
            user_id=user.id,
            endpoint_type="email",
            value=user.email,
            normalized_value=user.email,
            value_hash=user.email_hash,
            is_primary=True,
            status="active",
            source="test",
        )
        db.add(endpoint)
        db.flush()
        endpoints.append(endpoint)
        db.add(ContactVerificationState(
            project_id=project.id,
            user_id=user.id,
            endpoint_id=endpoint.id,
            verification_type="email",
            identifier_hash=endpoint.value_hash,
            provider="millionverifier",
            canonical_status="valid",
            checked_at=now,
            expires_at=now + timedelta(days=60),
            last_attempt_status="succeeded",
            last_attempt_at=now,
        ))
    db.add(ContactPermissionEvidence(
        project_id=project.id,
        user_id=users[0].id,
        endpoint_id=endpoints[0].id,
        channel="email",
        permission_type="marketing",
        status="granted",
        source="consent_form",
        evidence_ref="form:campaign-test",
        captured_at=now,
    ))

    group_service = ContactGroupService(db)
    group = group_service.create(project.id, {
        "name": "Portuguese contacts",
        "group_type": "dynamic",
        "rule_config": {
            "match": "all",
            "filters": [{"field": "locale", "operator": "equals", "value": "pt-BR"}],
        },
    }, owner.id)
    evaluated = group_service.evaluate(group)
    assert evaluated["included"] == 2

    email_instance = EmailInstance(
        user_id=owner.id,
        workspace_id=workspace.id,
        project_id=project.id,
        provider_type="smtp",
        instance_name="campaign-smoke-instance",
        from_email="sender@example.test",
        smtp_server="smtp.example.test",
        smtp_port=587,
        connection_status="verified",
        is_active=True,
    )
    db.add(email_instance)
    db.flush()
    profile_service = DeliveryProfileService(db)
    sender = profile_service.create_identity(project.id, {
        "channel": "email",
        "provider": "smtp",
        "identity_key": "campaign-smoke-sender",
        "address": "sender@example.test",
        "external_instance_id": str(email_instance.id),
        "is_default": True,
    })
    profile = profile_service.create_profile(project.id, {
        "channel": "email",
        "provider": "smtp",
        "name": "Campaign SMTP",
        "sender_identity_id": sender.id,
        "max_per_minute": 10,
        "max_per_day": 100,
        "timezone": "America/Sao_Paulo",
        "config": {"instance_id": email_instance.id},
    })
    assert profile.health_status == "healthy"
    available = profile_service.available_senders(project.id)
    assert any(
        sender["kind"] == "email_instance"
        and sender["id"] == email_instance.id
        and set(sender) == {"kind", "id", "provider", "name", "address"}
        for sender in available
    )
    automatic_profile = profile_service.create_profile(project.id, {
        "channel": "email",
        "provider": "smtp",
        "name": "Automatic campaign instance",
        "max_per_day": 100,
        "timezone": "America/Sao_Paulo",
        "config": {"instance_id": email_instance.id},
    })
    assert automatic_profile.sender_identity_id is not None
    assert automatic_profile.health_status == "healthy"
    try:
        profile_service.create_profile(project.id, {
            "channel": "email",
            "provider": "smtp",
            "name": "Invalid string instance id",
            "max_per_day": 10,
            "config": {"instance_id": str(email_instance.id)},
        })
        raise AssertionError("string instance ids must be rejected")
    except DeliveryProfileError as exc:
        assert "positive integer" in str(exc)

    legacy_smtp = CustomerSMTPConfig(
        project_id=project.id,
        smtp_server="smtp.example.test",
        smtp_port=587,
        smtp_username="campaign-test",
        smtp_password="encrypted-test-value",
        smtp_use_tls=True,
        smtp_use_ssl=False,
        from_email="legacy-sender@example.test",
        from_name="Legacy Sender",
        is_active=True,
    )
    db.add(legacy_smtp)
    db.flush()
    legacy_profile = profile_service.create_profile(project.id, {
        "channel": "email",
        "provider": "smtp",
        "name": "Automatic legacy SMTP",
        "max_per_day": 100,
        "timezone": "America/Sao_Paulo",
        "config": {"smtp_config_id": legacy_smtp.id},
    })
    assert legacy_profile.sender_identity_id is not None
    assert legacy_profile.health_status == "healthy"
    assert any(
        sender["kind"] == "smtp_config" and sender["id"] == legacy_smtp.id
        for sender in profile_service.available_senders(project.id)
    )

    campaign_service = CampaignService(db)
    campaign = campaign_service.create(project.id, {
        "name": "Feature announcement",
        "campaign_type": "one_off",
        "default_channel": "email",
        "selection_config": {
            "group_id": group.id,
            "email_selection_rule": "primary",
        },
        "policy_config": {
            "permission_mode": "explicit_consent",
            "verification_mode": "require_valid",
        },
        "timezone": "America/Sao_Paulo",
        "actions": [{
            "action_type": "send_message",
            "channel": "email",
            "config": {"subject": "Olá {{name|default:cliente}}", "body": "Nova feature"},
        }],
    }, owner.id)
    schedule = {
        "mode": "start_forward",
        "start_at": now + timedelta(minutes=5),
        "timezone": "America/Sao_Paulo",
    }
    preview = campaign_service.preview_plan(campaign, schedule)
    assert preview["candidate_count"] == 2
    assert preview["eligible_count"] == 1
    assert preview["suppression_reasons"] == {"missing_permission_evidence": 1}
    assert preview["capacity_plan"]["feasible"] is True

    try:
        campaign_service.create_run(
            campaign,
            schedule=schedule,
            requested_by_user_id=owner.id,
        )
        raise AssertionError("draft campaigns must not create runs")
    except CampaignError as exc:
        assert "active" in str(exc)

    campaign.status = "active"
    db.commit()
    run = campaign_service.create_run(
        campaign,
        schedule=schedule,
        requested_by_user_id=owner.id,
        run_key="campaign-service-smoke-run",
        expected={
            "campaign_version": campaign.version,
            "candidate_count": 2,
            "eligible_count": 1,
            "planned_count": 1,
        },
        policy_override={
            "rules": ["channel_cooldown", "contact_caps"],
            "reason": "Urgent feature announcement approved for this run",
            "risk_acknowledged": True,
            "expires_in_hours": 24,
        },
    )
    repeated_run = campaign_service.create_run(
        campaign,
        schedule=schedule,
        requested_by_user_id=owner.id,
        run_key="campaign-service-smoke-run",
        expected={
            "campaign_version": campaign.version,
            "candidate_count": 2,
            "eligible_count": 1,
            "planned_count": 1,
        },
        policy_override={
            "rules": ["contact_caps", "channel_cooldown"],
            "reason": "Urgent feature announcement approved for this run",
            "risk_acknowledged": True,
            "expires_in_hours": 24,
        },
    )
    assert repeated_run.id == run.id
    assert run.policy_override.status == "approved"
    assert active_override_rules(run.policy_override) == {
        "channel_cooldown", "contact_caps",
    }
    assert run.policy_override.approved_by_user_id == owner.id
    assert db.query(ChannelCapacityReservation).filter(
        ChannelCapacityReservation.run_id == run.id,
    ).count() == 1
    recipients = db.query(CampaignRecipient).filter(
        CampaignRecipient.project_id == project.id,
        CampaignRecipient.run_id == run.id,
    ).all()
    assert sorted(recipient.status for recipient in recipients) == ["pending", "skipped"]
    assert all("@" not in str(recipient.endpoint_hash or "") for recipient in recipients)
    assert "@example.test" not in str(run.audience_snapshot)
    assert "@example.test" not in str(run.capacity_plan)

    pending = next(recipient for recipient in recipients if recipient.status == "pending")
    reservation = db.query(ChannelCapacityReservation).filter(
        ChannelCapacityReservation.run_id == run.id,
        ChannelCapacityReservation.delivery_profile_id == pending.delivery_profile_id,
    ).one()
    original_reserved = reservation.units_reserved
    reserved_profile = reservation.delivery_profile
    reserved_identity = reserved_profile.sender_identity
    try:
        profile_service.update_profile(reserved_profile, {
            "max_per_day": int(reserved_profile.max_per_day or 0) + 1,
        })
        raise AssertionError("reserved campaign profile capacity must be immutable")
    except DeliveryProfileError as exc:
        assert "while campaign capacity is reserved" in str(exc)
        db.rollback()
    try:
        profile_service.update_identity(reserved_identity, {
            "address": "changed-during-run@example.test",
        })
        raise AssertionError("reserved campaign sender identity must be immutable")
    except DeliveryProfileError as exc:
        assert "while campaign capacity is reserved" in str(exc)
        db.rollback()
    try:
        profile_service.update_profile(reserved_profile, {"status": "disabled"})
        raise AssertionError("reserved campaign profile must not be disabled")
    except DeliveryProfileError as exc:
        assert "cannot be disabled" in str(exc)
        db.rollback()
    try:
        profile_service.update_identity(reserved_identity, {"status": "disabled"})
        raise AssertionError("reserved campaign sender identity must not be disabled")
    except DeliveryProfileError as exc:
        assert "cannot be disabled" in str(exc)
        db.rollback()
    worker = CampaignWorker()
    pending.status = "processing"
    pending.attempt_count = 2
    db.commit()
    worker._requeue_for_delivery_pause(db, pending, "delivery_profile_paused")
    db.refresh(pending)
    assert pending.status == "pending"
    assert pending.attempt_count == 1
    assert pending.last_error_code == "delivery_profile_paused"
    message_id = worker._message_id(pending)
    assert message_id == worker._message_id(pending)
    assert message_id.startswith("<campaign-") and message_id.endswith("@versya.io>")
    worker._consume_capacity(db, pending)
    worker._consume_capacity(db, pending)
    db.refresh(reservation)
    assert reservation.units_consumed == 1
    assert reservation.units_reserved == original_reserved
    original_per_minute = reserved_profile.max_per_minute
    reserved_profile.max_per_minute = 1
    pending.status = "failed"
    pending.sent_at = now
    db.flush()
    assert worker._profile_throttle_until(
        db, pending, reserved_profile, now + timedelta(seconds=1), {},
    ) is not None
    pending.status = "pending"
    pending.sent_at = None
    reserved_profile.max_per_minute = original_per_minute
    db.commit()

    delayed_log = SendLog(
        project_id=project.id,
        user_id=pending.user_id,
        channel="email",
        recipient=endpoints[0].value,
        content_type="rich",
        content_summary="Campaign smoke",
        source_type="campaign",
        source_id=pending.id,
        status="delayed",
        scheduled_at=now + timedelta(hours=1),
    )
    db.add(delayed_log)
    db.flush()
    pending.send_log_id = delayed_log.id
    pending.status = "deferred"
    run.status = "running"
    db.commit()
    assert worker._reclaim_campaign_deferrals(db, now) == 1
    db.refresh(pending)
    db.refresh(delayed_log)
    assert pending.status == "pending"
    assert pending.send_log_id == delayed_log.id
    assert delayed_log.status == "delayed"

    cancellation_log = SendLog(
        project_id=project.id,
        user_id=pending.user_id,
        channel="email",
        recipient=endpoints[0].value,
        content_type="rich",
        content_summary="Campaign cancellation smoke",
        source_type="campaign",
        source_id=pending.id,
        status="delayed",
        scheduled_at=now + timedelta(hours=1),
    )
    db.add(cancellation_log)
    db.flush()
    pending.send_log_id = cancellation_log.id
    pending.status = "processing"
    pending.last_error_code = None
    reservation.units_consumed = 0
    reservation.units_reserved = original_reserved
    reservation.status = "reserved"
    cancellation_log.status = "submitting"
    db.commit()
    campaign_service.cancel_run(run)
    db.refresh(pending)
    db.refresh(cancellation_log)
    db.refresh(reservation)
    assert pending.status == "processing"
    assert cancellation_log.status == "canceled"
    assert reservation.status == "reserved"
    worker._finish_recipient(db, pending, "canceled", "run_canceled")
    db.refresh(pending)
    db.refresh(reservation)
    assert pending.status == "canceled"
    assert reservation.status == "released"
    pending.status = "processing"
    pending.updated_at = now - timedelta(minutes=11)
    pending.completed_at = None
    db.commit()
    assert worker._recover_stale_processing(db, now) == 1
    db.refresh(pending)
    assert pending.status == "canceled"
    assert pending.completed_at is not None

    run.cancel_requested = False
    run.status = "running"
    pending.status = "processing"
    pending.updated_at = now - timedelta(minutes=11)
    pending.completed_at = None
    cancellation_log.status = "submitting"
    cancellation_log.error_message = None
    db.commit()
    assert worker._recover_stale_processing(db, now) == 1
    db.refresh(pending)
    db.refresh(cancellation_log)
    assert pending.status == "failed"
    assert pending.last_error_code == "submission_unknown"
    assert cancellation_log.status == "submission_unknown"

    old_action_id = pending.action_id
    campaign.status = "paused"
    db.commit()
    campaign_service.update(campaign, {
        "actions": [{
            "action_type": "send_message",
            "channel": "email",
            "config": {"subject": "Revised", "body": "Revised feature"},
        }],
    }, owner.id)
    old_action = db.query(CampaignAction).filter(CampaignAction.id == old_action_id).one()
    db.refresh(pending)
    assert old_action.status == "archived"
    assert pending.action_id == old_action_id
    assert any(action.status == "active" for action in campaign.actions)

    large_run = CampaignRun(
        project_id=project.id,
        campaign_id=campaign.id,
        run_key="large-override-approval-smoke",
        trigger_type="manual",
        status="scheduled",
        planned_count=1001,
        requested_by_user_id=owner.id,
    )
    db.add(large_run)
    db.flush()
    large_override = create_override(db, large_run, {
        "rules": ["channel_cooldown"],
        "reason": "Large launch requires an audited operational exception",
        "risk_acknowledged": True,
        "expires_in_hours": 24,
    }, owner.id)
    assert large_override.status == "approved"
    assert large_override.dual_approval_required is False
    assert large_override.approved_by_user_id == owner.id
    assert large_run.status == "scheduled"

    # Compatibility for overrides created before self-approval became the rule:
    # the requesting admin can release a legacy pending run as well.
    large_override.status = "pending_approval"
    large_override.approved_by_user_id = None
    large_override.approved_at = None
    large_run.status = "awaiting_override_approval"
    db.flush()
    approve_override(db, large_override, owner.id)
    assert large_override.status == "approved"
    assert large_override.approved_by_user_id == owner.id
    assert large_run.status == "scheduled"

    project_policy = ProjectPolicy(
        project_id=project.id,
        is_active=True,
        channel_cooldowns={"email": 3600},
        contact_caps={"daily": {"email": 1}},
    )
    db.add(project_policy)
    db.flush()
    policy_service = PolicyService(db)
    assert policy_service.record_contact_once(
        project.id, users[0].id, "email", "campaign", pending.id, now,
    ) is True
    assert policy_service.record_contact_once(
        project.id, users[0].id, "email", "campaign", pending.id, now,
    ) is False
    assert db.query(ContactLedger).filter(
        ContactLedger.project_id == project.id,
        ContactLedger.source == "campaign",
        ContactLedger.source_id == str(pending.id),
    ).count() == 1
    assert policy_service.check_can_contact(
        project.id, users[0].id, "email", "campaign",
    ).policy_violated == "channel_cooldown"
    assert policy_service.check_can_contact(
        project.id, users[0].id, "email", "campaign",
        ignored_policies={"channel_cooldown"},
    ).policy_violated == "daily_cap"
    assert policy_service.check_can_contact(
        project.id, users[0].id, "email", "campaign",
        ignored_policies={"channel_cooldown", "contact_caps"},
    ).allowed is True
    try:
        policy_service.check_can_contact(
            project.id, users[0].id, "email", "campaign",
            ignored_policies={"opt_out"},
        )
        raise AssertionError("immutable policies must never be accepted as ignored")
    except ValueError as exc:
        assert "not allowed" in str(exc)


def test_campaign_group_and_snapshot_integration(db_session):
    run_campaign_service_smoke(db_session)


def test_campaign_service_supports_transactional_draft_preview(db_session):
    owner = User(email="campaign-draft-preview@example.test", name="Owner", is_active=True)
    db_session.add(owner)
    db_session.flush()
    workspace = Workspace(name="Campaign draft preview", owner_id=owner.id, is_active=True)
    db_session.add(workspace)
    db_session.flush()
    owner.workspace_id = workspace.id
    project = Project(
        name="Campaign draft preview project",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="d" * 64,
    )
    db_session.add(project)
    db_session.flush()

    service = CampaignService(db_session)
    draft = service.create(project.id, {
        "name": "Draft only",
        "status": "draft",
        "default_channel": "email",
        "selection_config": {"rule_config": {"filters": []}},
        "purpose_key": "trial.activation",
        "actions": [{
            "action_type": "send_message",
            "channel": "email",
            "config": {"subject": "Welcome", "body": "Hello"},
        }],
    }, owner.id, commit=False)

    assert draft.id is not None
    assert draft.status == "draft"
    assert db_session.query(Campaign).filter_by(project_id=project.id).count() == 1
    db_session.rollback()
    assert db_session.query(Campaign).filter_by(name="Draft only").count() == 0
