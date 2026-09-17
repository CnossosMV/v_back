from datetime import datetime, timedelta
import logging

import pytest
from fastapi import HTTPException

from app.dependencies import require_project_role
from app.models import Project, ProjectMember, User, WhatsAppInstance, Workspace
from app.models.campaigns import ContactEndpoint, ContactVerificationRequest, OperationalAlert
from app.models.messaging import (
    ContactVerificationItem,
    ContactVerificationJob,
    ContactVerificationOperation,
    ContactVerificationState,
    MessagingUser,
    ProjectVerificationSettings,
    WorkspaceVerificationEntitlement,
)
from app.services.channels.send_service import SendService
from app.services.channels.lanes import Lane, resolve_lane
from app.services.contact_verification.access import VerificationAccessError, VerificationAccessService
from app.services.contact_verification.providers import (
    MillionVerifierAdapter,
    ProviderRequestError,
    evolution_status,
    millionverifier_status,
    provider_registry,
)
from app.services.contact_verification.service import ContactVerificationService
from app.services.contact_verification.worker import ContactVerificationWorker
from app.services.evolution_api_service import evolution_api_service
from app.services.messaging.contact_merge_service import ContactMergeService
from app.services.messaging.pii_hasher import pii_hasher
from app.routers.contact_verification import _project_admin
from app.routers.messaging.users import _apply_user_filters, _verification_map
from app.logging_security import (
    SensitiveQueryFilter,
    install_sensitive_query_filter,
    redact_sensitive_query_values,
)
from app.schemas.contact_verification import VerificationJobResponse


def test_sensitive_query_values_are_redacted_from_http_logs():
    message = 'GET https://provider.test/fileinfo?key=secret-value&file_id=123&api=other-secret'
    redacted = redact_sensitive_query_values(message)
    assert "secret-value" not in redacted
    assert "other-secret" not in redacted
    assert "file_id=123" in redacted
    assert redacted.count("[REDACTED]") == 2

    record = logging.LogRecord(
        "httpx", 20, __file__, 1, 'HTTP Request: %s',
        ('https://provider.test/credits?api=credential',), None,
    )
    assert SensitiveQueryFilter().filter(record) is True
    assert "credential" not in record.getMessage()

    websocket_record = logging.LogRecord(
        "uvicorn.error", 20, __file__, 1,
        '%s - "WebSocket %s" [accepted]',
        (("127.0.0.1", 12345), "/ws/inbox/1?token=jwt-secret&view=open"),
        None,
    )
    assert SensitiveQueryFilter().filter(websocket_record) is True
    rendered = websocket_record.getMessage()
    assert "jwt-secret" not in rendered
    assert "token=[REDACTED]" in rendered
    assert "view=open" in rendered


def test_sensitive_query_filter_is_installed_on_server_loggers():
    log_filter = install_sensitive_query_filter()
    protected = (
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("gunicorn.error"),
        logging.getLogger("gunicorn.access"),
    )
    try:
        assert all(log_filter in logger.filters for logger in protected)
        assert all(
            log_filter in handler.filters
            for logger in protected
            for handler in logger.handlers
        )
    finally:
        for logger in [logging.getLogger(), *protected, logging.getLogger("httpx"), logging.getLogger("uvicorn")]:
            logger.removeFilter(log_filter)
            for handler in logger.handlers:
                handler.removeFilter(log_filter)


def test_job_provider_progress_is_weighted_across_bulk_operations():
    job = ContactVerificationJob(
        id=1,
        project_id=1,
        verification_type="email",
        provider="millionverifier",
        source_type="project_contacts",
        trigger_type="manual",
        status="provider_processing",
        candidate_count=400,
        unique_count=400,
        processed_count=0,
        valid_count=0,
        invalid_count=0,
        risky_count=0,
        skipped_count=0,
        failed_count=0,
        provider_units=0,
        billing_disposition="waived",
        cancel_requested=False,
        created_at=datetime.utcnow(),
    )
    job.operations = [
        ContactVerificationOperation(item_count=100, response_summary={"percent": 25}),
        ContactVerificationOperation(item_count=300, response_summary={"percent": 75}),
    ]
    assert job.provider_progress == 62
    assert VerificationJobResponse.model_validate(job).provider_progress == 62


def test_contact_filters_and_badges_ignore_old_identifier_results(db_session):
    _owner, _workspace, project = _project(
        db_session, "identifier-filter@example.test",
    )
    contact = MessagingUser(
        project_id=project.id,
        external_id="identifier-filter-contact",
        email="current@example.test",
        status="active",
        is_sandbox=False,
    )
    db_session.add(contact)
    db_session.flush()
    contact.email_hash = pii_hasher.hash_email(contact.email, project.pii_salt)
    checked_at = datetime.utcnow()
    state = ContactVerificationState(
        project_id=project.id,
        user_id=contact.id,
        verification_type="email",
        identifier_hash=pii_hasher.hash_email("old@example.test", project.pii_salt),
        provider="millionverifier",
        canonical_status="valid",
        checked_at=checked_at,
        expires_at=checked_at + timedelta(days=60),
        last_attempt_status="succeeded",
        last_attempt_at=checked_at,
    )
    db_session.add(state)
    db_session.commit()

    base = db_session.query(MessagingUser).filter(MessagingUser.project_id == project.id)
    assert _apply_user_filters(
        base,
        db_session,
        project.id,
        email_verification_status="valid",
    ).count() == 0
    assert _apply_user_filters(
        base,
        db_session,
        project.id,
        email_verification_status="unverified",
    ).count() == 1
    assert _verification_map(db_session, project.id, [contact.id]) == {}

    state.identifier_hash = contact.email_hash
    db_session.commit()
    assert _apply_user_filters(
        base,
        db_session,
        project.id,
        email_verification_status="valid",
    ).count() == 1
    assert _verification_map(db_session, project.id, [contact.id])[contact.id]["email"]["status"] == "valid"


def _project(db, email: str, name: str = "Workspace") -> tuple[User, Workspace, Project]:
    owner = User(email=email, name=email, is_active=True)
    db.add(owner)
    db.flush()
    workspace = Workspace(name=name, owner_id=owner.id, is_active=True)
    db.add(workspace)
    db.flush()
    owner.workspace_id = workspace.id
    project = Project(
        name=f"{name} project",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="a" * 64,
    )
    db.add(project)
    db.flush()
    return owner, workspace, project


def _enable(db, workspace_id: int) -> None:
    db.add(WorkspaceVerificationEntitlement(
        workspace_id=workspace_id,
        access_mode="unmetered",
        is_active=True,
    ))
    db.flush()


def _add_contact_endpoint(db, project: Project, contact: MessagingUser, verification_type: str):
    endpoint_type = "email" if verification_type == "email" else "phone"
    value = contact.email if verification_type == "email" else (contact.phone_e164 or contact.phone)
    value_hash = (
        pii_hasher.hash_email(value, project.pii_salt)
        if verification_type == "email"
        else pii_hasher.hash_phone(value, project.pii_salt)
    )
    endpoint = ContactEndpoint(
        project_id=project.id,
        user_id=contact.id,
        endpoint_type=endpoint_type,
        value=value,
        normalized_value=value,
        value_hash=value_hash,
        is_primary=True,
        status="active",
        source="test",
    )
    db.add(endpoint)
    db.flush()
    return endpoint


@pytest.mark.parametrize(
    ("native", "canonical"),
    [
        ("ok", "valid"),
        ("invalid", "invalid"),
        ("disposable", "invalid"),
        ("catch_all", "risky"),
        ("unknown", "risky"),
    ],
)
def test_millionverifier_result_mapping(native, canonical):
    assert millionverifier_status(native) == canonical


def test_bulk_report_reconciles_with_or_without_custom_hash_column():
    report = (
        "email,versya_item_key,result,quality\n"
        "first@example.test,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,ok,good\n"
        "second@example.test,,invalid,bad\n"
    )
    results = MillionVerifierAdapter._parse_bulk_report(report)
    assert results["a" * 64].canonical_status == "valid"
    assert results["email:second@example.test"].canonical_status == "invalid"


@pytest.mark.parametrize(("exists", "canonical"), [(True, "valid"), (False, "invalid")])
def test_evolution_result_mapping(exists, canonical):
    assert evolution_status(exists) == canonical


@pytest.mark.asyncio
async def test_evolution_verification_does_not_fallback_to_platform_key():
    result = await evolution_api_service.check_whatsapp_numbers("project-instance", None, ["5511999999999"])
    assert result["success"] is False
    assert "credential" in result["error"].lower()


def test_entitlement_is_workspace_scoped(db_session):
    _owner_a, workspace_a, project_a = _project(db_session, "a@example.test", "A")
    _owner_b, _workspace_b, project_b = _project(db_session, "b@example.test", "B")
    _enable(db_session, workspace_a.id)
    db_session.commit()

    access = VerificationAccessService(db_session)
    assert access.authorize(project_a.id).billing_disposition == "waived"
    with pytest.raises(VerificationAccessError):
        access.authorize(project_b.id)


@pytest.mark.asyncio
async def test_admin_rbac_and_router_workspace_isolation(db_session):
    owner, workspace, project = _project(db_session, "rbac-owner@example.test", "RBAC")
    _enable(db_session, workspace.id)
    viewer = User(
        email="rbac-viewer@example.test", name="Viewer", is_active=True,
        workspace_id=workspace.id,
    )
    db_session.add(viewer)
    db_session.flush()
    db_session.add(ProjectMember(project_id=project.id, user_id=viewer.id, role="viewer", is_active=True))
    _other_owner, _other_workspace, _other_project = _project(
        db_session, "rbac-other@example.test", "Other RBAC",
    )
    db_session.commit()

    admin_check = require_project_role("admin")
    with pytest.raises(HTTPException) as denied:
        await admin_check(project_id=project.id, db=db_session, current_user=viewer)
    assert denied.value.status_code == 403

    with pytest.raises(HTTPException) as owner_cross_tenant:
        await admin_check(
            project_id=project.id,
            db=db_session,
            current_user=_other_owner,
        )
    assert owner_cross_tenant.value.status_code == 404

    with pytest.raises(HTTPException) as isolated:
        _project_admin(
            project_id=project.id,
            membership={"user_id": _other_owner.id, "role": "owner"},
            current_user=_other_owner,
            db=db_session,
        )
    assert isolated.value.status_code == 404


def test_preview_and_job_deduplicate_normalized_email(db_session):
    owner, workspace, project = _project(db_session, "owner@example.test")
    _enable(db_session, workspace.id)
    db_session.add_all([
        MessagingUser(
            project_id=project.id, external_id="one", email="Same@Example.test ",
            status="active", is_sandbox=False,
        ),
        MessagingUser(
            project_id=project.id, external_id="two", email="same@example.test",
            status="active", is_sandbox=False,
        ),
    ])
    db_session.commit()

    service = ContactVerificationService(db_session)
    preview = service.preview(project.id, "email", {"scope": "all"})
    assert preview["candidate_count"] == 2
    assert preview["unique_count"] == 1

    job = service.create_job(
        project.id, "email", {"scope": "all"}, owner.id,
        expected_unique_count=1,
        idempotency_key="dedupe-normalized-email",
    )
    assert job.unique_count == 1
    assert db_session.query(ContactVerificationItem).filter_by(job_id=job.id).count() == 2


def test_individual_job_idempotency_replays_and_rejects_conflicts(db_session, monkeypatch):
    owner, workspace, project = _project(db_session, "individual-idempotency@example.test")
    _enable(db_session, workspace.id)
    first = MessagingUser(
        project_id=project.id,
        external_id="individual-idempotency-first",
        email="individual-first@example.test",
        status="active",
        is_sandbox=False,
    )
    second = MessagingUser(
        project_id=project.id,
        external_id="individual-idempotency-second",
        email="individual-second@example.test",
        status="active",
        is_sandbox=False,
    )
    db_session.add_all([first, second])
    db_session.flush()
    first_endpoint = ContactEndpoint(
        project_id=project.id,
        user_id=first.id,
        endpoint_type="email",
        value=first.email,
        normalized_value=first.email,
        value_hash=pii_hasher.hash_email(first.email, project.pii_salt),
        is_primary=True,
        status="active",
        source="test",
    )
    second_endpoint = ContactEndpoint(
        project_id=project.id,
        user_id=second.id,
        endpoint_type="email",
        value=second.email,
        normalized_value=second.email,
        value_hash=pii_hasher.hash_email(second.email, project.pii_salt),
        is_primary=True,
        status="active",
        source="test",
    )
    db_session.add_all([first_endpoint, second_endpoint])
    db_session.commit()

    service = ContactVerificationService(db_session)
    monkeypatch.setattr(service, "_lock_individual_job_idempotency", lambda *_args: None)
    key = "individual-stable-request-key"
    first_job = service.create_job(
        project.id,
        "email",
        {"scope": "contact", "contact_id": first.id},
        owner.id,
        trigger_type="individual",
        contact_id=first.id,
        endpoint_id=first_endpoint.id,
        idempotency_key=key,
    )
    replayed_job = service.create_job(
        project.id,
        "email",
        {"scope": "contact", "contact_id": first.id},
        owner.id,
        trigger_type="individual",
        contact_id=first.id,
        endpoint_id=first_endpoint.id,
        idempotency_key=key,
    )

    assert replayed_job.id == first_job.id
    assert getattr(replayed_job, "_idempotency_replayed") is True
    assert db_session.query(ContactVerificationJob).filter_by(
        project_id=project.id,
        trigger_type="individual",
    ).count() == 1
    metadata = (first_job.selection or {})["_idempotency"]
    assert metadata["endpoint_id"] == first_endpoint.id
    assert metadata["verification_type"] == "email"

    with pytest.raises(ValueError, match="different individual verification request"):
        service.create_job(
            project.id,
            "email",
            {"scope": "contact", "contact_id": second.id},
            owner.id,
            trigger_type="individual",
            contact_id=second.id,
            endpoint_id=second_endpoint.id,
            idempotency_key=key,
        )


def test_resolve_individual_endpoint_is_project_contact_and_type_scoped(db_session):
    _owner, _workspace, project = _project(db_session, "endpoint-scope@example.test")
    contact = MessagingUser(
        project_id=project.id,
        external_id="endpoint-scope-contact",
        email="endpoint-scope@example.test",
        status="active",
        is_sandbox=False,
    )
    db_session.add(contact)
    db_session.flush()
    endpoint = ContactEndpoint(
        project_id=project.id,
        user_id=contact.id,
        endpoint_type="email",
        value=contact.email,
        normalized_value=contact.email,
        value_hash=pii_hasher.hash_email(contact.email, project.pii_salt),
        is_primary=True,
        status="active",
        source="test",
    )
    db_session.add(endpoint)
    db_session.commit()

    service = ContactVerificationService(db_session)
    assert service.resolve_individual_endpoint(project.id, contact.id, "email").id == endpoint.id
    with pytest.raises(ValueError, match="no active phone endpoint"):
        service.resolve_individual_endpoint(project.id, contact.id, "whatsapp")


@pytest.mark.asyncio
async def test_changed_identifier_is_never_applied(db_session, monkeypatch):
    owner, workspace, project = _project(db_session, "change@example.test")
    _enable(db_session, workspace.id)
    contact = MessagingUser(
        project_id=project.id, external_id="change", email="old@example.test",
        status="active", is_sandbox=False,
    )
    db_session.add(contact)
    db_session.flush()
    endpoint = _add_contact_endpoint(db_session, project, contact, "email")
    db_session.commit()
    job = ContactVerificationService(db_session).create_job(
        project.id, "email", {"scope": "contact"}, owner.id,
        trigger_type="individual", contact_id=contact.id,
        endpoint_id=endpoint.id, idempotency_key="individual-change-test",
    )
    contact.email = "new@example.test"
    endpoint.value = contact.email
    endpoint.normalized_value = contact.email
    db_session.commit()

    class Adapter:
        provider_code = "millionverifier"
        provider_version = "test"

        async def credits(self):
            return {"credits": 100}

        async def verify_single(self, _email):
            raise AssertionError("provider must not receive a changed identifier")

    monkeypatch.setattr(provider_registry, "email", lambda _code: Adapter())
    await ContactVerificationService(db_session).process_job(job)

    item = db_session.query(ContactVerificationItem).filter_by(job_id=job.id).one()
    assert item.status == "skipped"
    assert item.error_code == "identifier_changed"
    assert db_session.query(ContactVerificationState).count() == 0


@pytest.mark.asyncio
async def test_insufficient_single_credits_fails_explicitly(db_session, monkeypatch):
    owner, workspace, project = _project(db_session, "credits@example.test")
    _enable(db_session, workspace.id)
    contact = MessagingUser(
        project_id=project.id, external_id="credits", email="credits-contact@example.test",
        status="active", is_sandbox=False,
    )
    db_session.add(contact)
    db_session.flush()
    endpoint = _add_contact_endpoint(db_session, project, contact, "email")
    db_session.commit()
    job = ContactVerificationService(db_session).create_job(
        project.id, "email", {"scope": "contact"}, owner.id,
        trigger_type="individual", contact_id=contact.id,
        endpoint_id=endpoint.id, idempotency_key="individual-credits-test",
    )

    class NoCreditAdapter:
        provider_code = "millionverifier"
        provider_version = "test"
        key_source = "platform"

        async def credits(self):
            return {"credits": 0}

    monkeypatch.setattr(provider_registry, "email", lambda _code: NoCreditAdapter())
    await ContactVerificationService(db_session).process_job(job)

    db_session.refresh(job)
    item = db_session.query(ContactVerificationItem).filter_by(job_id=job.id).one()
    assert job.status == "failed"
    assert "Insufficient" in job.error_message
    assert item.status == "failed"
    assert item.error_code == "insufficient_credits"


@pytest.mark.asyncio
async def test_worker_resumes_an_existing_running_job(db_session, monkeypatch):
    owner, workspace, project = _project(db_session, "restart@example.test")
    _enable(db_session, workspace.id)
    db_session.add(MessagingUser(
        project_id=project.id, external_id="restart", email="restart-contact@example.test",
        status="active", is_sandbox=False,
    ))
    db_session.commit()
    job = ContactVerificationService(db_session).create_job(
        project.id, "email", {"scope": "all"}, owner.id,
        idempotency_key="restart-running-job",
    )
    job.status = "running"
    db_session.commit()
    processed = []

    async def record_resume(_service, selected_job):
        processed.append(selected_job.id)
        selected_job.status = "completed"
        selected_job.finished_at = datetime.utcnow()
        db_session.commit()

    monkeypatch.setattr(ContactVerificationService, "process_job", record_resume)
    worker = ContactVerificationWorker()
    worker._last_automatic_scan = datetime.utcnow()
    assert await worker._process_cycle(db_session) == 1
    assert processed == [job.id]


@pytest.mark.asyncio
async def test_whatsapp_daily_instance_limit_is_conservative(db_session):
    owner, workspace, project = _project(db_session, "wa-limit@example.test")
    _enable(db_session, workspace.id)
    instance = WhatsAppInstance(
        user_id=owner.id,
        workspace_id=workspace.id,
        project_id=project.id,
        provider_type="evolution_api",
        instance_name="verification-limit-instance",
        instance_key="encrypted",
        connection_status="connected",
        is_active=True,
    )
    contact = MessagingUser(
        project_id=project.id, external_id="wa-limit", phone_e164="5511999999999",
        status="active", is_sandbox=False,
    )
    db_session.add_all([instance, contact])
    db_session.flush()
    endpoint = _add_contact_endpoint(db_session, project, contact, "whatsapp")
    db_session.add(ProjectVerificationSettings(
        project_id=project.id,
        whatsapp_instance_id=instance.id,
    ))
    usage_job = ContactVerificationJob(
        project_id=project.id,
        verification_type="whatsapp",
        provider="evolution_api",
        status="completed",
    )
    db_session.add(usage_job)
    db_session.flush()
    db_session.add_all([
        ContactVerificationOperation(
            project_id=project.id,
            job_id=usage_job.id,
            provider="evolution_api",
            provider_key_source="project",
            operation_type="whatsapp_lookup",
            status="completed",
            item_count=1,
            request_summary={"instance_id": str(instance.id)},
        )
        for _ in range(250)
    ])
    db_session.commit()
    service = ContactVerificationService(db_session)
    job = service.create_job(
        project.id, "whatsapp", {"scope": "contact"}, owner.id,
        trigger_type="individual", contact_id=contact.id,
        endpoint_id=endpoint.id, idempotency_key="individual-wa-limit-test",
    )
    await service.process_job(job)
    db_session.refresh(job)
    assert job.status == "queued"
    assert db_session.query(ContactVerificationOperation).filter_by(job_id=job.id).count() == 0


def test_send_policy_only_blocks_fresh_matching_result(db_session):
    _owner, workspace, project = _project(db_session, "policy@example.test")
    contact = MessagingUser(
        project_id=project.id,
        external_id="policy",
        email="policy-contact@example.test",
        status="active",
        is_sandbox=False,
        is_subscribed=True,
    )
    db_session.add(contact)
    db_session.flush()
    email_hash = pii_hasher.hash_email(contact.email, project.pii_salt)
    settings = ProjectVerificationSettings(
        project_id=project.id,
        email_send_policy="block_invalid",
        whatsapp_send_policy="report_only",
    )
    state = ContactVerificationState(
        project_id=project.id,
        user_id=contact.id,
        verification_type="email",
        identifier_hash=email_hash,
        provider="millionverifier",
        canonical_status="invalid",
        provider_status="invalid",
        checked_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=60),
        last_attempt_status="succeeded",
        last_attempt_at=datetime.utcnow(),
    )
    db_session.add_all([settings, state])
    db_session.commit()

    trace = []
    service = SendService(db_session)
    assert service._filter_verification_policy(project.id, contact.id, ["email"], trace) == []
    assert contact.is_subscribed is True

    state.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert service._filter_verification_policy(project.id, contact.id, ["email"], []) == ["email"]

    state.expires_at = datetime.utcnow() + timedelta(days=60)
    state.identifier_hash = "b" * 64
    db_session.commit()
    assert service._filter_verification_policy(project.id, contact.id, ["email"], []) == ["email"]


def test_campaign_is_always_promotional_and_requires_fresh_valid_verification(db_session):
    assert resolve_lane("campaign") == Lane.PROMOTIONAL

    _owner, _workspace, project = _project(db_session, "campaign-policy@example.test")
    contact = MessagingUser(
        project_id=project.id,
        external_id="campaign-policy",
        email="campaign-contact@example.test",
        status="active",
        is_sandbox=False,
        is_subscribed=True,
    )
    db_session.add(contact)
    db_session.flush()
    service = SendService(db_session)

    # A promotional campaign fails closed while the endpoint is unverified.
    assert service._filter_verification_policy(
        project.id, contact.id, ["email"], [], policy_profile="campaign",
    ) == []

    identifier_hash = pii_hasher.hash_email(contact.email, project.pii_salt)
    db_session.add(ContactVerificationState(
        project_id=project.id,
        user_id=contact.id,
        verification_type="email",
        identifier_hash=identifier_hash,
        provider="millionverifier",
        canonical_status="valid",
        checked_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=60),
        last_attempt_status="succeeded",
        last_attempt_at=datetime.utcnow(),
    ))
    db_session.commit()
    assert service._filter_verification_policy(
        project.id, contact.id, ["email"], [], policy_profile="campaign",
    ) == ["email"]

    state = db_session.query(ContactVerificationState).filter(
        ContactVerificationState.project_id == project.id,
        ContactVerificationState.user_id == contact.id,
    ).one()
    state.canonical_status = "unknown"
    db_session.commit()
    assert service._filter_verification_policy(
        project.id, contact.id, ["email"], [], policy_profile="campaign",
    ) == []

@pytest.mark.asyncio
async def test_cancelled_job_finishes_remaining_items_without_provider_call(db_session, monkeypatch):
    owner, workspace, project = _project(db_session, "cancel@example.test")
    _enable(db_session, workspace.id)
    db_session.add(MessagingUser(
        project_id=project.id, external_id="cancel", email="cancel-contact@example.test",
        status="active", is_sandbox=False,
    ))
    db_session.commit()
    service = ContactVerificationService(db_session)
    job = service.create_job(
        project.id, "email", {"scope": "all"}, owner.id,
        idempotency_key="cancel-manual-job",
    )
    service.cancel_job(job)

    class Adapter:
        async def stop_bulk(self, _file_id):
            raise AssertionError("no remote file exists")

    monkeypatch.setattr(provider_registry, "email", lambda _code: Adapter())
    await service.process_job(job)
    db_session.refresh(job)
    assert job.status == "cancelled"
    assert job.processed_count == job.candidate_count


@pytest.mark.asyncio
async def test_first_seen_request_maps_every_terminal_job_outcome(db_session, monkeypatch):
    _owner, _workspace, project = _project(
        db_session, "request-terminal-outcomes@example.test",
    )
    contact = MessagingUser(
        project_id=project.id,
        external_id="request-terminal-contact",
        email="request-terminal@example.test",
        status="active",
        is_sandbox=False,
    )
    db_session.add(contact)
    db_session.flush()

    async def no_provider_work(_service, _job):
        return None

    monkeypatch.setattr(ContactVerificationService, "process_job", no_provider_work)
    worker = ContactVerificationWorker()

    cases = [
        ("completed", "skipped", "identifier_changed", "skipped", "identifier_changed"),
        ("completed_with_errors", None, None, "failed", "missing_job_item"),
        ("cancelled", "cancelled", None, "cancelled", "verification_cancelled"),
    ]
    for index, (job_status, item_status, item_error, expected_status, expected_error) in enumerate(cases):
        job = ContactVerificationJob(
            project_id=project.id,
            verification_type="email",
            provider="millionverifier",
            trigger_type="first_seen_or_change",
            status=job_status,
            selection={"scope": "contact"},
        )
        db_session.add(job)
        db_session.flush()
        if item_status:
            db_session.add(ContactVerificationItem(
                project_id=project.id,
                job_id=job.id,
                user_id=contact.id,
                verification_type="email",
                identifier_hash=str(index + 1) * 64,
                status=item_status,
                error_code=item_error,
            ))
        request = ContactVerificationRequest(
            project_id=project.id,
            user_id=contact.id,
            job_id=job.id,
            verification_type="email",
            provider="millionverifier",
            trigger_type="first_seen_or_change",
            idempotency_key=f"terminal-case-{index}",
            status="processing_claimed",
        )
        db_session.add(request)
        db_session.commit()

        await worker._process_request(db_session, request)
        db_session.refresh(request)
        assert request.status == expected_status
        assert request.error_code == expected_error
        assert request.finished_at is not None


@pytest.mark.asyncio
async def test_bulk_cleanup_remains_durable_until_remote_delete_is_confirmed(db_session):
    _owner, _workspace, project = _project(db_session, "cleanup-durable@example.test")
    job = ContactVerificationJob(
        project_id=project.id,
        verification_type="email",
        provider="millionverifier",
        trigger_type="manual",
        status="provider_processing",
        selection={"scope": "all"},
    )
    db_session.add(job)
    db_session.flush()
    operation = ContactVerificationOperation(
        project_id=project.id,
        job_id=job.id,
        provider="millionverifier",
        provider_key_source="platform",
        operation_type="bulk_file",
        provider_operation_id="provider-file-with-pii",
        status="provider_deleting",
        item_count=1,
        billing_disposition="waived",
    )
    db_session.add(operation)
    db_session.commit()

    class FailingAdapter:
        key_source = "platform"

        async def delete_bulk(self, _file_id):
            raise ProviderRequestError("provider unavailable", transient=False)

    service = ContactVerificationService(db_session)
    assert await service._delete_bulk_file(job, operation, FailingAdapter()) is False
    db_session.commit()
    db_session.refresh(operation)
    db_session.refresh(job)
    assert operation.status == "provider_deleting"
    assert operation.finished_at is None
    assert operation.response_summary["cleanup_pending"] is True
    assert job.status == "provider_processing"
    alert = db_session.query(OperationalAlert).filter(
        OperationalAlert.project_id == project.id,
        OperationalAlert.alert_type == "verification_bulk_cleanup_pending",
    ).one()
    assert alert.status == "open"

    summary = dict(operation.response_summary)
    summary["cleanup_next_attempt_at"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
    operation.response_summary = summary
    db_session.commit()

    class SuccessfulAdapter:
        key_source = "platform"

        async def delete_bulk(self, _file_id):
            return None

    assert await service._delete_bulk_file(job, operation, SuccessfulAdapter()) is True
    db_session.commit()
    db_session.refresh(operation)
    db_session.refresh(alert)
    assert operation.status == "completed"
    assert operation.response_summary["remote_file_deleted"] is True
    assert alert.status == "resolved"


def test_merge_carries_matching_state_and_contact_delete_cascades(db_session):
    _owner, _workspace, project = _project(db_session, "merge-verify@example.test")
    winner = MessagingUser(
        project_id=project.id, external_id="winner", email="same@example.test",
        status="active", is_sandbox=False,
    )
    loser = MessagingUser(
        project_id=project.id, external_id="loser", email="same@example.test",
        status="active", is_sandbox=False,
    )
    identifier_hash = pii_hasher.hash_email("same@example.test", project.pii_salt)
    winner.email_hash = identifier_hash
    loser.email_hash = identifier_hash
    db_session.add_all([winner, loser])
    db_session.flush()
    state = ContactVerificationState(
        project_id=project.id,
        user_id=loser.id,
        verification_type="email",
        identifier_hash=identifier_hash,
        provider="legacy",
        provider_key_source="legacy",
        canonical_status="valid",
        checked_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=60),
        last_attempt_at=datetime.utcnow(),
    )
    db_session.add(state)
    db_session.commit()

    ContactMergeService(db_session)._merge_verification_states(project.id, winner, loser)
    db_session.commit()
    db_session.refresh(state)
    assert state.user_id == winner.id
    assert state.provider == "legacy"

    state_id = state.id
    db_session.delete(winner)
    db_session.commit()
    assert db_session.query(ContactVerificationState).filter_by(id=state_id).first() is None
