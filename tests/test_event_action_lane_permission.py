from datetime import datetime, timedelta, timezone

from app.models import EventAction, Project, SendLog, User, Workspace
from app.routers.send_layer import _is_policy_blocked
from app.models.campaigns import ContactEndpoint, ContactPermissionEvidence
from app.models.messaging import MessagingUser
from app.schemas.messaging import EmailPermissionClaim
from app.services.channels.lanes import Lane, resolve_lane_for_send
from app.services.messaging.permission_evidence_service import PermissionEvidenceService
from app.services.messaging.pii_hasher import pii_hasher


def _project(db, suffix: str) -> Project:
    owner = User(email=f"owner-{suffix}@example.test", name="Owner", is_active=True)
    db.add(owner)
    db.flush()
    workspace = Workspace(name=f"Workspace {suffix}", owner_id=owner.id, is_active=True)
    db.add(workspace)
    db.flush()
    owner.workspace_id = workspace.id
    project = Project(
        name=f"Project {suffix}",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="a" * 64,
    )
    db.add(project)
    db.flush()
    return project


def test_event_action_declares_lane_with_project_scope(db_session):
    project = _project(db_session, "lane")
    other = _project(db_session, "other")
    action = EventAction(
        project_id=project.id,
        name="Service notification",
        trigger_event="service.ready",
        actions=[{"type": "send_template", "config": {"template_id": 1}}],
        lane="transactional",
    )
    db_session.add(action)
    db_session.flush()

    assert resolve_lane_for_send(
        db_session, project.id, "event_action", action.id,
    ) == (Lane.TRANSACTIONAL, True)
    assert resolve_lane_for_send(
        db_session, other.id, "event_action", action.id,
    ) == (Lane.PROMOTIONAL, False)


def _send_log(project_id: int, **overrides) -> SendLog:
    values = {
        "project_id": project_id,
        "channel": "email",
        "recipient": "subscriber@example.test",
        "content_type": "text",
        "source_type": "event_action",
        "status": "blocked",
    }
    values.update(overrides)
    return SendLog(**values)


def test_retry_inherits_durable_original_intent_snapshot(db_session):
    project = _project(db_session, "retry-snapshot")
    action = EventAction(
        project_id=project.id,
        name="Requested service reminder",
        trigger_event="service.reminder_requested",
        actions=[{"type": "send_template", "config": {"template_id": 1}}],
        lane="transactional",
    )
    db_session.add(action)
    db_session.flush()
    original = _send_log(
        project.id,
        source_id=action.id,
        intent_class="promotional",
    )
    db_session.add(original)
    db_session.flush()

    # Editing the action later must not silently rewrite the intent of an
    # existing send. Re-evaluating the business event is a separate operation.
    assert resolve_lane_for_send(
        db_session, project.id, "retry", original.id,
    ) == (Lane.PROMOTIONAL, True)


def test_retry_legacy_row_falls_back_to_authored_root_lane(db_session):
    project = _project(db_session, "retry-legacy")
    action = EventAction(
        project_id=project.id,
        name="Requested service reminder",
        trigger_event="service.reminder_requested",
        actions=[{"type": "send_template", "config": {"template_id": 1}}],
        lane="transactional",
    )
    db_session.add(action)
    db_session.flush()
    original = _send_log(project.id, source_id=action.id, intent_class=None)
    db_session.add(original)
    db_session.flush()
    first_retry = _send_log(
        project.id,
        source_type="retry",
        source_id=original.id,
        retry_of_id=original.id,
        intent_class=None,
    )
    db_session.add(first_retry)
    db_session.flush()

    assert resolve_lane_for_send(
        db_session, project.id, "retry", first_retry.id,
    ) == (Lane.TRANSACTIONAL, True)


def test_retry_without_provenance_fails_closed(db_session):
    project = _project(db_session, "retry-missing")
    assert resolve_lane_for_send(
        db_session, project.id, "retry", 999999,
    ) == (Lane.PROMOTIONAL, False)


def test_policy_block_is_not_a_transport_retry(db_session):
    project = _project(db_session, "policy-block")
    historical = _send_log(
        project.id,
        status="exhausted",
        error_message="All channels blocked by missing consent",
    )
    current = _send_log(project.id, status="blocked")

    assert _is_policy_blocked(historical) is True
    assert _is_policy_blocked(current) is True


def test_authenticated_email_permission_is_audited_idempotently(db_session):
    project = _project(db_session, "permission")
    contact = MessagingUser(
        project_id=project.id,
        external_id="subscriber-1",
        email="subscriber@example.test",
        status="active",
        is_subscribed=True,
        opted_out_channels=[],
    )
    db_session.add(contact)
    db_session.flush()
    db_session.add(ContactEndpoint(
        project_id=project.id,
        user_id=contact.id,
        endpoint_type="email",
        value=contact.email,
        normalized_value=contact.email,
        value_hash=pii_hasher.hash_email(contact.email, project.pii_salt),
        is_primary=True,
        status="active",
        source="test",
    ))
    db_session.flush()
    service = PermissionEvidenceService(db_session)
    captured = datetime.now(timezone.utc) - timedelta(minutes=5)
    policy_version = "tabloide-subscriber-optin-v1"
    granted = EmailPermissionClaim(
        status="granted",
        source="tabloide_subscription",
        captured_at=captured,
        policy_version=policy_version,
        evidence_ref="subscription:123:optin",
    )

    first = service.record_email_claim(
        project_id=project.id,
        user=contact,
        claim=granted,
        api_key_id=7,
        ingress="backend_events_hmac",
    )
    second = service.record_email_claim(
        project_id=project.id,
        user=contact,
        claim=granted,
        api_key_id=7,
        ingress="backend_events_hmac",
    )

    assert first.id == second.id
    assert db_session.query(ContactPermissionEvidence).count() == 1
    assert contact.consent_channels["email"]["granted"] is True
    assert contact.consent_version == policy_version
    assert first.evidence_metadata["authenticated_api_key_id"] == 7

    withdrawn = EmailPermissionClaim(
        status="withdrawn",
        source="tabloide_unsubscribe",
        captured_at=captured + timedelta(minutes=10),
        policy_version=policy_version,
        evidence_ref="subscription:123:withdrawn",
    )
    service.record_email_claim(
        project_id=project.id,
        user=contact,
        claim=withdrawn,
        api_key_id=7,
        ingress="backend_events_hmac",
    )
    assert contact.consent_channels["email"]["granted"] is False

    stale = EmailPermissionClaim(
        status="granted",
        source="delayed_replay",
        captured_at=captured - timedelta(days=1),
        policy_version=policy_version,
        evidence_ref="subscription:123:stale",
    )
    service.record_email_claim(
        project_id=project.id,
        user=contact,
        claim=stale,
        api_key_id=7,
        ingress="backend_events_hmac",
    )
    assert contact.consent_channels["email"]["granted"] is False
