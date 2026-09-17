"""PostgreSQL integration coverage for DSAR privacy and safe merge reversal."""

import asyncio
from datetime import datetime, timedelta

import pytest

from app.main import app
from app.models import Project, ProjectMember, User, Workspace
from app.models.campaigns import (
    ContactEndpoint,
    ContactPermissionEvidence,
    ContactVerificationRequest,
)
from app.models.messaging import (
    ContactIdentity,
    ContactMergeLog,
    ContactVerificationItem,
    ContactVerificationJob,
    ContactVerificationOperation,
    ContactVerificationState,
    DSARRequestStatus,
    DSARRequestType,
    MessagingDSARRequest,
    MessagingUser,
    ProjectVerificationSettings,
)
from app.routers.auth import get_current_user
from app.services.campaigns.eligibility import CampaignEligibilityService
from app.services.contact_verification.providers import provider_registry
from app.services.messaging.contact_merge_service import ContactMergeService
from app.services.messaging.contact_profile_service import ContactProfileService
from app.services.messaging.dsar_processor import DSARProcessor
from app.services.messaging.pii_hasher import pii_hasher


@pytest.fixture(autouse=True)
def _postgresql_only(db_session):
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("DSAR privacy integration tests require PostgreSQL")


def _workspace_project(db, suffix: str):
    owner = User(
        email=f"privacy-owner-{suffix}@example.test",
        name=f"Privacy owner {suffix}",
        is_active=True,
    )
    db.add(owner)
    db.flush()
    workspace = Workspace(
        name=f"Privacy workspace {suffix}",
        owner_id=owner.id,
        is_active=True,
    )
    db.add(workspace)
    db.flush()
    owner.workspace_id = workspace.id
    project = Project(
        name=f"Privacy project {suffix}",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt=(suffix[0] if suffix else "p") * 64,
    )
    db.add(project)
    db.flush()
    return owner, workspace, project


def _contact(db, project: Project, suffix: str, email: str) -> MessagingUser:
    user = MessagingUser(
        project_id=project.id,
        external_id=f"privacy-contact-{suffix}",
        email=email,
        email_hash=pii_hasher.hash_email(email, project.pii_salt),
        status="active",
        is_sandbox=False,
        is_subscribed=True,
    )
    db.add(user)
    db.flush()
    return user


def _email_endpoint(db, user: MessagingUser, *, primary: bool = True) -> ContactEndpoint:
    endpoint = ContactEndpoint(
        project_id=user.project_id,
        user_id=user.id,
        endpoint_type="email",
        value=user.email,
        normalized_value=user.email,
        value_hash=user.email_hash,
        is_primary=primary,
        status="active",
        source="test",
    )
    db.add(endpoint)
    db.flush()
    return endpoint


def test_dsar_admin_routes_enforce_role_and_workspace(client, db_session):
    _owner, workspace, project = _workspace_project(db_session, "rbac-a")
    _other_owner, _other_workspace, other_project = _workspace_project(db_session, "rbac-b")
    admin = User(
        email="privacy-admin@example.test",
        name="Privacy admin",
        workspace_id=workspace.id,
        is_active=True,
    )
    viewer = User(
        email="privacy-viewer@example.test",
        name="Privacy viewer",
        workspace_id=workspace.id,
        is_active=True,
    )
    db_session.add_all([admin, viewer])
    db_session.flush()
    db_session.add_all([
        ProjectMember(project_id=project.id, user_id=admin.id, role="admin"),
        ProjectMember(project_id=project.id, user_id=viewer.id, role="viewer"),
    ])
    db_session.add(MessagingDSARRequest(
        project_id=project.id,
        request_type=DSARRequestType.export,
        external_id="subject",
    ))
    db_session.commit()

    app.dependency_overrides[get_current_user] = lambda: viewer
    assert client.get(
        f"/api/v1/dsar/projects/{project.id}/requests",
    ).status_code == 403

    app.dependency_overrides[get_current_user] = lambda: admin
    response = client.get(f"/api/v1/dsar/projects/{project.id}/requests")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert client.get(
        f"/api/v1/dsar/projects/{other_project.id}/requests",
    ).status_code == 404


def test_rectify_replaces_endpoint_invalidates_state_and_enqueues_first_seen(db_session):
    _owner, _workspace, project = _workspace_project(db_session, "rectify")
    db_session.add(ProjectVerificationSettings(
        project_id=project.id,
        email_verify_on_first_seen=True,
    ))
    user = _contact(db_session, project, "rectify", "old@example.test")
    old_endpoint = _email_endpoint(db_session, user)
    now = datetime.utcnow()
    db_session.add(ContactVerificationState(
        project_id=project.id,
        user_id=user.id,
        endpoint_id=old_endpoint.id,
        verification_type="email",
        identifier_hash=old_endpoint.value_hash,
        provider="millionverifier",
        canonical_status="valid",
        checked_at=now,
        expires_at=now + timedelta(days=60),
        last_attempt_status="succeeded",
        last_attempt_at=now,
    ))
    old_request = ContactVerificationRequest(
        project_id=project.id,
        user_id=user.id,
        endpoint_id=old_endpoint.id,
        verification_type="email",
        provider="millionverifier",
        trigger_type="first_seen_or_change",
        idempotency_key="old-first-seen",
        status="queued",
    )
    dsar_request = MessagingDSARRequest(
        project_id=project.id,
        request_type=DSARRequestType.rectify,
        user_id=user.id,
        result_data={"email": "  NEW@Example.Test "},
    )
    db_session.add_all([old_request, dsar_request])
    db_session.commit()

    processed = asyncio.run(DSARProcessor().process_request(
        db_session,
        project_id=project.id,
        request_id=dsar_request.id,
    ))

    assert processed.status == DSARRequestStatus.completed
    db_session.refresh(user)
    db_session.refresh(old_endpoint)
    db_session.refresh(old_request)
    assert user.email == "new@example.test"
    assert old_endpoint.status == "inactive"
    assert old_endpoint.is_primary is False
    assert old_request.status == "cancelled"
    assert old_request.error_code == "identifier_rectified"
    assert db_session.query(ContactVerificationState).filter_by(
        project_id=project.id,
        endpoint_id=old_endpoint.id,
    ).count() == 0
    new_endpoint = db_session.query(ContactEndpoint).filter_by(
        project_id=project.id,
        user_id=user.id,
        value="new@example.test",
    ).one()
    assert new_endpoint.status == "active"
    assert new_endpoint.is_primary is True
    new_request = db_session.query(ContactVerificationRequest).filter_by(
        project_id=project.id,
        endpoint_id=new_endpoint.id,
        status="queued",
    ).one()
    assert new_request.trigger_type == "first_seen_or_change"


def test_reintroduced_endpoint_requeues_same_first_seen_request_without_duplicate(
    db_session,
):
    _owner, _workspace, project = _workspace_project(db_session, "reactivate")
    db_session.add(ProjectVerificationSettings(
        project_id=project.id,
        email_verify_on_first_seen=True,
    ))
    user = _contact(db_session, project, "reactivate", "reactivate@example.test")
    service = ContactProfileService(db_session)

    service.sync_user(user, source="test")
    endpoint = db_session.query(ContactEndpoint).filter_by(
        project_id=project.id,
        user_id=user.id,
        endpoint_type="email",
    ).one()
    request = db_session.query(ContactVerificationRequest).filter_by(
        project_id=project.id,
        endpoint_id=endpoint.id,
        idempotency_key=f"first_seen:email:{endpoint.id}:{endpoint.value_hash}",
    ).one()
    assert request.status == "queued"

    # Reintroducing an inactive endpoint must retain the one active durable
    # request rather than creating a second idempotency row.
    endpoint.status = "inactive"
    endpoint.is_primary = False
    db_session.commit()
    service.sync_user(user, source="test")
    db_session.refresh(endpoint)
    db_session.refresh(request)
    assert endpoint.status == "active"
    assert request.status == "queued"
    assert db_session.query(ContactVerificationRequest).filter_by(
        project_id=project.id,
        endpoint_id=endpoint.id,
    ).count() == 1

    # A terminal request from the previous lifecycle is requeued in place.
    endpoint.status = "inactive"
    endpoint.is_primary = False
    request.status = "cancelled"
    request.finished_at = datetime.utcnow()
    db_session.commit()
    service.sync_user(user, source="test")
    db_session.refresh(request)
    assert request.status == "queued"
    assert request.finished_at is None
    assert db_session.query(ContactVerificationRequest).filter_by(
        project_id=project.id,
        endpoint_id=endpoint.id,
    ).count() == 1


def test_withdraw_consent_retires_prior_endpoint_grants(db_session):
    _owner, _workspace, project = _workspace_project(db_session, "withdraw")
    user = _contact(db_session, project, "withdraw", "withdraw@example.test")
    endpoint = _email_endpoint(db_session, user)
    captured_at = datetime.utcnow() - timedelta(days=1)
    endpoint_grant = ContactPermissionEvidence(
        project_id=project.id,
        user_id=user.id,
        endpoint_id=endpoint.id,
        channel="email",
        permission_type="marketing",
        status="granted",
        source="signup",
        evidence_ref="signup:endpoint",
        captured_at=captured_at,
    )
    global_grant = ContactPermissionEvidence(
        project_id=project.id,
        user_id=user.id,
        endpoint_id=None,
        channel="email",
        permission_type="marketing",
        status="granted",
        source="import",
        evidence_ref="import:global",
        captured_at=captured_at,
    )
    dsar_request = MessagingDSARRequest(
        project_id=project.id,
        request_type=DSARRequestType.withdraw_consent,
        user_id=user.id,
    )
    db_session.add_all([endpoint_grant, global_grant, dsar_request])
    db_session.commit()

    asyncio.run(DSARProcessor().process_request(
        db_session,
        project_id=project.id,
        request_id=dsar_request.id,
    ))
    db_session.refresh(user)
    db_session.refresh(endpoint_grant)
    db_session.refresh(global_grant)

    assert user.is_subscribed is False
    assert "email" in user.opted_out_channels
    assert endpoint_grant.expires_at is not None
    assert global_grant.expires_at is not None
    withdrawals = db_session.query(ContactPermissionEvidence).filter_by(
        project_id=project.id,
        user_id=user.id,
        channel="email",
        status="withdrawn",
    ).all()
    assert {row.endpoint_id for row in withdrawals} == {None, endpoint.id}

    # A legacy re-subscribe toggle cannot make the pre-withdrawal grant the
    # current evidence; fresh granted evidence must be appended explicitly.
    user.is_subscribed = True
    evidence = db_session.query(ContactPermissionEvidence).filter_by(
        project_id=project.id,
        user_id=user.id,
        channel="email",
        permission_type="marketing",
    ).order_by(
        ContactPermissionEvidence.captured_at.desc(),
        ContactPermissionEvidence.id.desc(),
    ).all()
    current = CampaignEligibilityService._permission_for(
        evidence,
        endpoint.id,
        datetime.utcnow(),
    )
    assert current.status == "withdrawn"


def test_delete_waits_for_remote_bulk_cleanup_and_preserves_cleanup_state(
    db_session,
    monkeypatch,
):
    _owner, _workspace, project = _workspace_project(db_session, "delete")
    user = _contact(db_session, project, "delete", "delete@example.test")
    user_id = user.id
    endpoint = _email_endpoint(db_session, user)
    job = ContactVerificationJob(
        project_id=project.id,
        endpoint_id=endpoint.id,
        verification_type="email",
        provider="millionverifier",
        trigger_type="manual",
        status="provider_processing",
        selection={"scope": "contact", "contact_id": user.id},
    )
    db_session.add(job)
    db_session.flush()
    item = ContactVerificationItem(
        project_id=project.id,
        job_id=job.id,
        user_id=user.id,
        endpoint_id=endpoint.id,
        verification_type="email",
        identifier_hash=endpoint.value_hash,
        status="running",
    )
    operation = ContactVerificationOperation(
        project_id=project.id,
        job_id=job.id,
        endpoint_id=endpoint.id,
        provider="millionverifier",
        operation_type="bulk_file",
        provider_operation_id="provider-file-with-subject-pii",
        status="provider_processing",
        item_count=1,
        response_summary={"remote_file_deleted": False},
    )
    verification_request = ContactVerificationRequest(
        project_id=project.id,
        user_id=user.id,
        endpoint_id=endpoint.id,
        job_id=job.id,
        verification_type="email",
        provider="millionverifier",
        trigger_type="manual",
        idempotency_key="delete-linked-request",
        status="processing",
    )
    dsar_request = MessagingDSARRequest(
        project_id=project.id,
        request_type=DSARRequestType.delete,
        user_id=user.id,
    )
    db_session.add_all([item, operation, verification_request, dsar_request])
    db_session.commit()
    item_id = item.id

    class FailingAdapter:
        key_source = "platform"

        async def stop_bulk(self, _file_id):
            return None

        async def delete_bulk(self, _file_id):
            raise RuntimeError("provider temporarily unavailable")

    monkeypatch.setattr(provider_registry, "email", lambda _provider: FailingAdapter())
    processor = DSARProcessor()
    first = asyncio.run(processor.process_request(
        db_session,
        project_id=project.id,
        request_id=dsar_request.id,
    ))

    assert first.status == DSARRequestStatus.processing
    assert db_session.get(MessagingUser, user_id) is not None
    assert db_session.get(ContactVerificationItem, item_id) is not None
    db_session.refresh(job)
    db_session.refresh(operation)
    assert job.status == "cancel_requested"
    assert operation.status == "provider_deleting"
    assert operation.response_summary["remote_file_deleted"] is False

    operation.response_summary = {
        **operation.response_summary,
        "cleanup_next_attempt_at": (datetime.utcnow() - timedelta(seconds=1)).isoformat(),
    }
    db_session.commit()

    deleted_files = []

    class SuccessfulAdapter:
        key_source = "platform"

        async def stop_bulk(self, _file_id):
            return None

        async def delete_bulk(self, file_id):
            deleted_files.append(file_id)

    monkeypatch.setattr(provider_registry, "email", lambda _provider: SuccessfulAdapter())
    second = asyncio.run(processor.process_request(
        db_session,
        project_id=project.id,
        request_id=dsar_request.id,
    ))

    assert second.status == DSARRequestStatus.completed
    assert deleted_files == ["provider-file-with-subject-pii"]
    assert db_session.get(MessagingUser, user_id) is None
    assert db_session.get(ContactVerificationItem, item_id) is None
    db_session.refresh(job)
    db_session.refresh(operation)
    assert job.status == "cancelled"
    assert job.selection == {"erased_by_dsar": True}
    assert operation.response_summary["remote_file_deleted"] is True
    assert operation.endpoint_id is None


def test_undo_merge_fails_closed_without_complete_reversal_manifest(db_session):
    _owner, _workspace, project = _workspace_project(db_session, "undo")
    winner = _contact(db_session, project, "undo-winner", "winner@example.test")
    loser = _contact(db_session, project, "undo-loser", "loser@example.test")
    loser.status = "merged"
    loser.merged_into = winner.id
    transferred_identity = ContactIdentity(
        project_id=project.id,
        user_id=winner.id,
        identity_type="email",
        identity_value="loser@example.test",
        verified=True,
        source="manual",
    )
    merge_log = ContactMergeLog(
        project_id=project.id,
        winner_id=winner.id,
        loser_id=loser.id,
        triggered_by="manual",
        snapshot_winner={"email": winner.email},
        snapshot_loser={"email": loser.email},
        identities_transferred=[{
            "type": "email",
            "value": transferred_identity.identity_value,
        }],
        properties_resolved={},
    )
    db_session.add_all([transferred_identity, merge_log])
    db_session.commit()

    with pytest.raises(ValueError, match="complete reversal manifest"):
        ContactMergeService(db_session).undo_merge(merge_log.id, project.id)

    db_session.refresh(loser)
    db_session.refresh(transferred_identity)
    db_session.refresh(merge_log)
    assert loser.status == "merged"
    assert loser.merged_into == winner.id
    assert transferred_identity.user_id == winner.id
    assert merge_log.undone_at is None
