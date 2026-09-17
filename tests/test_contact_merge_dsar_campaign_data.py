"""Focused integration coverage for campaign/contact merge and DSAR data."""

import asyncio
from datetime import datetime, timedelta

from app.models import (
    ContactRateWindow,
    ContactPosition,
    ContactPositionTransition,
    EventAction,
    EventActionCooldown,
    Project,
    SendLog,
    User,
    Workspace,
)
from app.models.campaigns import (
    Campaign,
    CampaignRecipient,
    CampaignRun,
    ContactEndpoint,
    ContactGroup,
    ContactGroupMembership,
    ContactPermissionEvidence,
    ContactVerificationRequest,
)
from app.models.messaging import (
    ContactIdentity,
    ContactVerificationItem,
    ContactVerificationJob,
    ContactVerificationState,
    DSARRequestType,
    MessagingAdsConsentEvidence,
    MessagingAudience,
    MessagingAudienceMembership,
    MessagingAudienceSyncItem,
    MessagingAudienceSyncJob,
    MessagingDSARRequest,
    MessagingUser,
)
from app.services.lifecycle_model_service import LifecycleModelService
from app.services.messaging.contact_merge_service import ContactMergeService
from app.services.messaging.dsar_processor import DSARProcessor


def _project(db, suffix: str) -> Project:
    owner = User(
        email=f"contact-data-{suffix}@example.test",
        name=f"Owner {suffix}",
        is_active=True,
    )
    db.add(owner)
    db.flush()
    workspace = Workspace(
        name=f"Contact data {suffix}",
        owner_id=owner.id,
        is_active=True,
    )
    db.add(workspace)
    db.flush()
    owner.workspace_id = workspace.id
    project = Project(
        name=f"Contact project {suffix}",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt=suffix[0] * 64,
    )
    db.add(project)
    db.flush()
    return project


def _campaign_recipient(db, project: Project, user: MessagingUser, endpoint: ContactEndpoint):
    campaign = Campaign(
        project_id=project.id,
        name=f"Campaign {user.external_id}",
        status="active",
        default_channel="email",
    )
    db.add(campaign)
    db.flush()
    run = CampaignRun(
        project_id=project.id,
        campaign_id=campaign.id,
        run_key=f"run:{user.external_id}",
        status="running",
    )
    db.add(run)
    db.flush()
    recipient = CampaignRecipient(
        project_id=project.id,
        run_id=run.id,
        user_id=user.id,
        endpoint_id=endpoint.id,
        endpoint_hash=endpoint.value_hash,
        channel="email",
        idempotency_key=f"{user.id}:delivery",
        status="sent",
        sent_at=datetime.utcnow(),
    )
    db.add(recipient)
    db.flush()
    return campaign, run, recipient


def test_merge_canonicalizes_contact_data_without_reassigning_campaign_history(db_session):
    project = _project(db_session, "merge")
    now = datetime.utcnow()
    winner = MessagingUser(
        project_id=project.id,
        external_id="merge-winner",
        email="same@example.test",
        email_hash="a" * 64,
        status="active",
        is_sandbox=False,
    )
    loser = MessagingUser(
        project_id=project.id,
        external_id="merge-loser",
        email="same@example.test",
        email_hash="a" * 64,
        status="active",
        is_sandbox=False,
    )
    db_session.add_all([winner, loser])
    db_session.flush()
    winner_endpoint = ContactEndpoint(
        project_id=project.id,
        user_id=winner.id,
        endpoint_type="email",
        value="same@example.test",
        normalized_value="same@example.test",
        value_hash="a" * 64,
        is_primary=True,
        status="active",
        source="test",
    )
    duplicate_endpoint = ContactEndpoint(
        project_id=project.id,
        user_id=loser.id,
        endpoint_type="email",
        value="same@example.test",
        normalized_value="same@example.test",
        value_hash="a" * 64,
        is_primary=True,
        status="active",
        source="test",
    )
    unique_endpoint = ContactEndpoint(
        project_id=project.id,
        user_id=loser.id,
        endpoint_type="email",
        value="alternate@example.test",
        normalized_value="alternate@example.test",
        value_hash="b" * 64,
        is_primary=False,
        status="active",
        source="test",
    )
    db_session.add_all([winner_endpoint, duplicate_endpoint, unique_endpoint])
    db_session.flush()

    db_session.add_all([
        ContactVerificationState(
            project_id=project.id,
            user_id=winner.id,
            endpoint_id=winner_endpoint.id,
            verification_type="email",
            identifier_hash=winner_endpoint.value_hash,
            provider="millionverifier",
            canonical_status="invalid",
            checked_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=59),
            last_attempt_status="succeeded",
            last_attempt_at=now - timedelta(days=1),
        ),
        ContactVerificationState(
            project_id=project.id,
            user_id=loser.id,
            endpoint_id=duplicate_endpoint.id,
            verification_type="email",
            identifier_hash=duplicate_endpoint.value_hash,
            provider="millionverifier",
            canonical_status="valid",
            checked_at=now,
            expires_at=now + timedelta(days=60),
            last_attempt_status="succeeded",
            last_attempt_at=now,
        ),
        ContactPermissionEvidence(
            project_id=project.id,
            user_id=loser.id,
            endpoint_id=duplicate_endpoint.id,
            channel="email",
            permission_type="marketing",
            status="granted",
            source="signup",
            evidence_ref="signup:merge",
            captured_at=now,
        ),
        ContactVerificationRequest(
            project_id=project.id,
            user_id=loser.id,
            endpoint_id=duplicate_endpoint.id,
            verification_type="email",
            provider="millionverifier",
            trigger_type="first_seen_or_change",
            idempotency_key="merge-request",
            status="succeeded",
        ),
    ])
    group_a = ContactGroup(project_id=project.id, name="A", group_type="static", status="active")
    group_b = ContactGroup(project_id=project.id, name="B", group_type="static", status="active")
    db_session.add_all([group_a, group_b])
    db_session.flush()
    db_session.add_all([
        ContactGroupMembership(
            project_id=project.id,
            group_id=group_a.id,
            user_id=winner.id,
            state="included",
            source="manual",
        ),
        ContactGroupMembership(
            project_id=project.id,
            group_id=group_a.id,
            user_id=loser.id,
            state="excluded",
            source="manual",
            reason="explicit_exclusion",
        ),
        ContactGroupMembership(
            project_id=project.id,
            group_id=group_b.id,
            user_id=loser.id,
            state="included",
            source="manual",
        ),
    ])
    _campaign, _run, recipient = _campaign_recipient(
        db_session, project, loser, duplicate_endpoint,
    )
    original_recipient_user_id = recipient.user_id
    original_recipient_hash = recipient.endpoint_hash
    duplicate_endpoint_id = duplicate_endpoint.id
    db_session.commit()

    ContactMergeService(db_session)._merge_campaign_contact_data(project.id, winner, loser)
    db_session.commit()

    assert db_session.get(ContactEndpoint, duplicate_endpoint_id) is None
    assert db_session.get(ContactEndpoint, unique_endpoint.id).user_id == winner.id
    evidence = db_session.query(ContactPermissionEvidence).filter_by(
        project_id=project.id,
        evidence_ref="signup:merge",
    ).one()
    assert evidence.user_id == winner.id
    assert evidence.endpoint_id == winner_endpoint.id
    verification_request = db_session.query(ContactVerificationRequest).filter_by(
        project_id=project.id,
        idempotency_key="merge-request",
    ).one()
    assert verification_request.user_id == winner.id
    assert verification_request.endpoint_id == winner_endpoint.id
    state = db_session.query(ContactVerificationState).filter_by(
        project_id=project.id,
        endpoint_id=winner_endpoint.id,
        verification_type="email",
    ).one()
    assert state.user_id == winner.id
    assert state.canonical_status == "valid"
    memberships = {
        row.group_id: row
        for row in db_session.query(ContactGroupMembership).filter_by(
            project_id=project.id,
            user_id=winner.id,
        ).all()
    }
    assert memberships[group_a.id].state == "excluded"
    assert memberships[group_a.id].reason == "explicit_exclusion"
    assert memberships[group_b.id].state == "included"
    db_session.refresh(recipient)
    assert recipient.user_id == original_recipient_user_id
    assert recipient.endpoint_hash == original_recipient_hash


def test_full_merge_preserves_import_reconciliation_identity_and_audience_data(db_session):
    project = _project(db_session, "import-merge")
    now = datetime.utcnow()
    model = LifecycleModelService(db_session).ensure_legacy_model(project.id)
    winner = MessagingUser(
        project_id=project.id,
        external_id="source-contact-id",
        email=None,
        status="active",
        is_sandbox=False,
        is_subscribed=True,
        global_opt_out=False,
        consent_marketing=True,
        consent_analytics=True,
        consent_channels={
            "email": {
                "granted": True,
                "status": "granted",
            "source": "source_optin",
            "policy_version": "source-v1",
            "evidence_ref": "source:optin",
            "captured_at": (now - timedelta(days=2)).isoformat(),
            },
        },
    )
    loser = MessagingUser(
        project_id=project.id,
        external_id="aud_csv_duplicate",
        email="contact@example.test",
        email_hash="e" * 64,
        status="active",
        is_sandbox=False,
        created_via="audience_csv_import",
        is_subscribed=False,
        global_opt_out=True,
        consent_marketing=False,
        consent_analytics=False,
        consent_channels={
            "email": {
                "granted": False,
                "status": "withdrawn",
                "source": "tabloide_unsubscribe",
                "policy_version": "tabloide-v2",
                "evidence_ref": "source:withdrawn",
                "captured_at": (now - timedelta(days=1)).isoformat(),
            },
        },
        is_blocked=True,
        blocked_at=now - timedelta(hours=1),
        blocked_reason="source suppression",
    )
    db_session.add_all([winner, loser])
    db_session.flush()
    db_session.add_all([
        ContactIdentity(
            project_id=project.id,
            user_id=loser.id,
            identity_type="contact_id",
            identity_value=loser.external_id,
            verified=False,
            source="audience_csv_import",
        ),
        ContactIdentity(
            project_id=project.id,
            user_id=loser.id,
            identity_type="email",
            identity_value=loser.email,
            verified=False,
            source="audience_csv_import",
        ),
    ])
    endpoint = ContactEndpoint(
        project_id=project.id,
        user_id=loser.id,
        endpoint_type="email",
        value=loser.email,
        normalized_value=loser.email,
        value_hash=loser.email_hash,
        is_primary=True,
        status="active",
        source="audience_csv_import",
    )
    db_session.add(endpoint)
    db_session.flush()
    verification_job = ContactVerificationJob(
        project_id=project.id,
        endpoint_id=endpoint.id,
        verification_type="email",
        provider="fixture",
        source_type="project_contacts",
        trigger_type="manual",
        status="completed",
    )
    db_session.add(verification_job)
    db_session.flush()
    verification_item = ContactVerificationItem(
        project_id=project.id,
        job_id=verification_job.id,
        user_id=loser.id,
        endpoint_id=endpoint.id,
        verification_type="email",
        identifier_hash=loser.email_hash,
        status="completed",
    )
    db_session.add(verification_item)

    audience = MessagingAudience(
        project_id=project.id,
        name="Imported audience",
        source_type="csv_import",
        status="active",
    )
    db_session.add(audience)
    db_session.flush()
    membership = MessagingAudienceMembership(
        project_id=project.id,
        audience_id=audience.id,
        user_id=loser.id,
        state="eligible",
        eligibility_reason="eligible",
        last_evaluated_at=now,
    )
    consent = MessagingAdsConsentEvidence(
        project_id=project.id,
        user_id=loser.id,
        scope="ad_user_data",
        status="granted",
        source="csv_import",
        captured_at=now,
    )
    sync_job = MessagingAudienceSyncJob(
        project_id=project.id,
        audience_id=audience.id,
        provider_type="fixture",
        operation="sync",
        status="completed",
    )
    db_session.add_all([membership, consent, sync_job])
    db_session.flush()
    sync_item = MessagingAudienceSyncItem(
        project_id=project.id,
        job_id=sync_job.id,
        user_id=loser.id,
        operation="add",
        status="succeeded",
    )
    winner_position = ContactPosition(
        project_id=project.id,
        user_id=winner.id,
        lifecycle_model_id=model.id,
        type="default",
        stage="old",
        computed_at=now - timedelta(days=1),
    )
    loser_position = ContactPosition(
        project_id=project.id,
        user_id=loser.id,
        lifecycle_model_id=model.id,
        type="default",
        stage="new",
        computed_at=now,
    )
    transition = ContactPositionTransition(
        project_id=project.id,
        user_id=loser.id,
        lifecycle_model_id=model.id,
        from_stage="old",
        to_stage="new",
        reason="fixture",
        occurred_at=now,
    )
    db_session.add_all([sync_item, winner_position, loser_position, transition])
    action = EventAction(
        project_id=project.id,
        name="Merge cooldown fixture",
        trigger_event="fixture.event",
        actions=[],
        is_active=True,
    )
    db_session.add(action)
    db_session.flush()
    winner_cooldown = EventActionCooldown(
        project_id=project.id,
        event_action_id=action.id,
        user_id=winner.id,
        last_triggered_at=now - timedelta(hours=2),
        expires_at=now + timedelta(hours=1),
    )
    loser_last_triggered_at = now - timedelta(hours=1)
    loser_expires_at = now + timedelta(hours=2)
    loser_cooldown = EventActionCooldown(
        project_id=project.id,
        event_action_id=action.id,
        user_id=loser.id,
        last_triggered_at=loser_last_triggered_at,
        expires_at=loser_expires_at,
    )
    rate_window = ContactRateWindow(
        project_id=project.id,
        contact_identifier=loser.external_id,
        channel="email",
        window_start=now,
        message_count=3,
    )
    winner_rate_window = ContactRateWindow(
        project_id=project.id,
        contact_identifier=winner.external_id,
        channel="email",
        window_start=now - timedelta(minutes=1),
        message_count=2,
    )
    db_session.add_all([
        winner_cooldown,
        loser_cooldown,
        rate_window,
        winner_rate_window,
    ])
    db_session.commit()

    merge_log = ContactMergeService(db_session).merge_contacts(
        project.id,
        winner.id,
        loser.id,
        triggered_by="manual",
    )

    db_session.refresh(winner)
    db_session.refresh(loser)
    assert winner.email == "contact@example.test"
    assert winner.email_hash == "e" * 64
    assert winner.global_opt_out is True
    assert winner.is_subscribed is False
    assert winner.consent_marketing is False
    assert winner.consent_analytics is False
    assert winner.consent_channels["email"]["granted"] is False
    assert winner.consent_channels["email"]["status"] == "withdrawn"
    assert winner.consent_channels["email"]["policy_version"] == "tabloide-v2"
    assert winner.consent_channels["email"]["evidence_ref"] == "source:withdrawn"
    assert winner.consent_channels["email"]["timestamp"] == (now - timedelta(days=1)).isoformat()
    assert winner.consent_version == "tabloide-v2"
    assert winner.is_blocked is True
    assert winner.blocked_reason == "source suppression"
    assert loser.status == "merged"
    assert loser.merged_into == winner.id
    identities = db_session.query(ContactIdentity).filter_by(
        project_id=project.id,
        user_id=winner.id,
    ).all()
    assert {(row.identity_type, row.identity_value) for row in identities} == {
        ("contact_id", "source-contact-id"),
        ("contact_id", "aud_csv_duplicate"),
        ("email", "contact@example.test"),
    }
    assert len(merge_log.identities_transferred) == 2
    assert db_session.get(ContactEndpoint, endpoint.id).user_id == winner.id
    assert db_session.get(ContactVerificationItem, verification_item.id).user_id == winner.id
    assert db_session.get(MessagingAudienceMembership, membership.id).user_id == winner.id
    assert db_session.get(MessagingAdsConsentEvidence, consent.id).user_id == winner.id
    assert db_session.get(MessagingAudienceSyncItem, sync_item.id).user_id == winner.id
    position = db_session.query(ContactPosition).filter_by(
        project_id=project.id,
        user_id=winner.id,
        lifecycle_model_id=model.id,
    ).one()
    assert position.stage == "new"
    assert db_session.query(ContactPosition).filter_by(user_id=loser.id).count() == 0
    assert db_session.get(ContactPositionTransition, transition.id).user_id == winner.id
    cooldown = db_session.query(EventActionCooldown).filter_by(
        project_id=project.id,
        event_action_id=action.id,
        user_id=winner.id,
    ).one()
    assert cooldown.last_triggered_at == loser_last_triggered_at
    assert cooldown.expires_at == loser_expires_at
    assert db_session.query(EventActionCooldown).filter_by(user_id=loser.id).count() == 0
    rate_windows = db_session.query(ContactRateWindow).filter_by(
        project_id=project.id,
        contact_identifier=winner.external_id,
        channel="email",
    ).all()
    assert len(rate_windows) == 1
    assert rate_windows[0].message_count == 5
    assert rate_windows[0].window_start == now


def test_dsar_exports_contact_campaign_data_and_pseudonymizes_audit(db_session):
    project = _project(db_session, "dsar")
    other_project = _project(db_session, "other")
    now = datetime.utcnow()
    user = MessagingUser(
        project_id=project.id,
        external_id="dsar-contact",
        email="dsar@example.test",
        email_hash="c" * 64,
        status="active",
        is_sandbox=False,
    )
    other_user = MessagingUser(
        project_id=other_project.id,
        external_id="other-contact",
        email="other@example.test",
        email_hash="d" * 64,
        status="active",
        is_sandbox=False,
    )
    db_session.add_all([user, other_user])
    db_session.flush()
    user_id = user.id
    other_user_id = other_user.id
    merged_user = MessagingUser(
        project_id=project.id,
        external_id="dsar-merged-tombstone",
        email="dsar@example.test",
        email_hash="c" * 64,
        status="merged",
        merged_into=user.id,
        is_sandbox=False,
    )
    db_session.add(merged_user)
    db_session.flush()
    merged_user_id = merged_user.id
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
        endpoint_metadata={"locale": "pt-BR"},
    )
    other_endpoint = ContactEndpoint(
        project_id=other_project.id,
        user_id=other_user.id,
        endpoint_type="email",
        value=other_user.email,
        normalized_value=other_user.email,
        value_hash=other_user.email_hash,
        is_primary=True,
        status="active",
        source="test",
    )
    db_session.add_all([endpoint, other_endpoint])
    db_session.flush()
    other_endpoint_id = other_endpoint.id
    group = ContactGroup(project_id=project.id, name="DSAR group", group_type="static", status="active")
    db_session.add(group)
    db_session.flush()
    db_session.add_all([
        ContactPermissionEvidence(
            project_id=project.id,
            user_id=user.id,
            endpoint_id=endpoint.id,
            channel="email",
            permission_type="marketing",
            status="granted",
            source="signup",
            evidence_ref="signup:dsar",
            captured_at=now,
        ),
        ContactGroupMembership(
            project_id=project.id,
            group_id=group.id,
            user_id=user.id,
            state="included",
            source="manual",
        ),
        ContactVerificationState(
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
        ),
        ContactVerificationRequest(
            project_id=project.id,
            user_id=user.id,
            endpoint_id=endpoint.id,
            verification_type="email",
            provider="millionverifier",
            trigger_type="manual",
            idempotency_key="dsar-request",
            status="succeeded",
            request_metadata={"identifier_hash": endpoint.value_hash},
        ),
    ])
    # Campaign history intentionally remains on the merged tombstone. A DSAR
    # for the winner must still export and pseudonymize it.
    campaign, run, recipient = _campaign_recipient(
        db_session, project, merged_user, endpoint,
    )
    send_log = SendLog(
        project_id=project.id,
        user_id=merged_user.id,
        channel="email",
        recipient=user.email,
        content_type="text",
        content_summary="personal subject",
        content_payload={"body": "personal body"},
        source_type="campaign",
        source_id=recipient.id,
        status="sent",
        provider_message_id="provider-personal-id",
        render_context={"name": "Data Subject"},
    )
    db_session.add(send_log)
    db_session.flush()
    recipient.send_log_id = send_log.id
    export_request = MessagingDSARRequest(
        project_id=project.id,
        request_type=DSARRequestType.export,
        user_id=merged_user.id,
    )
    db_session.add(export_request)
    db_session.commit()

    processor = DSARProcessor()
    asyncio.run(processor._process_export(db_session, export_request))
    exported = export_request.result_data
    assert exported["contact_endpoints"][0]["value"] == "dsar@example.test"
    assert exported["permission_evidence"][0]["evidence_ref"] == "signup:dsar"
    assert exported["contact_group_memberships"][0]["group_name"] == "DSAR group"
    assert exported["verification_states"][0]["canonical_status"] == "valid"
    assert exported["verification_requests"][0]["idempotency_key"] == "dsar-request"
    assert exported["campaign_deliveries"][0]["campaign_id"] == campaign.id
    assert exported["campaign_deliveries"][0]["run_id"] == run.id
    assert exported["send_layer_messages"][0]["recipient"] == "dsar@example.test"
    assert exported["send_layer_messages"][0]["content_payload"] == {"body": "personal body"}

    identifier_only_export = MessagingDSARRequest(
        project_id=project.id,
        request_type=DSARRequestType.export,
        external_id=user.external_id,
        email_hash=user.email_hash,
        result_data={"contact_endpoints": [{"value": user.email}]},
    )
    db_session.add(identifier_only_export)
    delete_request = MessagingDSARRequest(
        project_id=project.id,
        request_type=DSARRequestType.delete,
        user_id=merged_user.id,
    )
    db_session.add(delete_request)
    db_session.commit()
    asyncio.run(processor._process_delete(db_session, delete_request))
    db_session.commit()

    assert db_session.query(ContactEndpoint).filter_by(project_id=project.id, user_id=user_id).count() == 0
    assert db_session.query(ContactPermissionEvidence).filter_by(project_id=project.id, user_id=user_id).count() == 0
    assert db_session.query(ContactGroupMembership).filter_by(project_id=project.id, user_id=user_id).count() == 0
    assert db_session.query(ContactVerificationState).filter_by(project_id=project.id, user_id=user_id).count() == 0
    assert db_session.query(ContactVerificationRequest).filter_by(project_id=project.id, user_id=user_id).count() == 0
    assert db_session.get(MessagingUser, merged_user_id) is None
    db_session.refresh(recipient)
    assert recipient.user_id is None
    assert recipient.endpoint_id is None
    assert recipient.endpoint_hash is None
    assert recipient.idempotency_key == f"erased:{recipient.id}"
    db_session.refresh(send_log)
    assert send_log.user_id is None
    assert send_log.recipient == "[DELETED]"
    assert send_log.content_payload is None
    assert send_log.provider_message_id is None
    db_session.refresh(export_request)
    assert export_request.user_id is None
    assert export_request.result_data.get("erased_at")
    assert "contact_endpoints" not in export_request.result_data
    db_session.refresh(identifier_only_export)
    assert identifier_only_export.external_id is None
    assert identifier_only_export.email_hash is None
    assert identifier_only_export.result_data.get("erased_at")
    assert "contact_endpoints" not in identifier_only_export.result_data
    assert db_session.get(ContactEndpoint, other_endpoint_id) is not None
    assert db_session.get(MessagingUser, other_user_id) is not None
