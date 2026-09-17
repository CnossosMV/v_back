from datetime import datetime, timedelta
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models
from app.mcp.audit import consume_pending_action, create_pending_action, hash_confirm_token, redact_value
from app.mcp.auth import (
    AGENT_SESSION_SCOPE,
    PRODUCT_BOOTSTRAP_SCOPE,
    SUPPORTED_SCOPES,
    McpPrincipal,
    is_mcp_admin_user,
    oauth_challenge_scope,
    protected_resource_metadata,
    resolve_user,
    unauthorized_response,
)
from app.mcp.config import get_mcp_settings
import app.mcp.tools as mcp_tools
from app.mcp.product_server import create_product_server
from app.mcp.tools import _model_dump_json, _readonly_sql, _resolve_project_id
from app.schemas.event_actions import EventActionUpdate


TEST_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
if TEST_DATABASE_URL.startswith("sqlite"):
    mcp_test_engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    mcp_test_engine = create_engine(TEST_DATABASE_URL)

McpTestingSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=mcp_test_engine,
)


def _test_email(label: str) -> str:
    return f"mcp-test-{label}-{uuid4().hex}@example.com"


@pytest.fixture
def mcp_db_session():
    """Use the test database without dropping migration-managed schema."""
    Base.metadata.create_all(bind=mcp_test_engine)
    session = McpTestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        test_users = (
            session.query(models.User.id, models.User.workspace_id)
            .filter(models.User.email.like("mcp-test-%@example.com"))
            .all()
        )
        user_ids = [row.id for row in test_users]
        workspace_ids = [row.workspace_id for row in test_users if row.workspace_id]

        if user_ids:
            session.query(models.McpPendingAction).filter(
                models.McpPendingAction.user_id.in_(user_ids)
            ).delete(synchronize_session=False)
            session.query(models.McpToolAuditLog).filter(
                models.McpToolAuditLog.user_id.in_(user_ids)
            ).delete(synchronize_session=False)
            session.query(models.McpConnectorInstallation).filter(
                models.McpConnectorInstallation.user_id.in_(user_ids)
            ).delete(synchronize_session=False)
            session.query(models.ProjectMember).filter(
                models.ProjectMember.user_id.in_(user_ids)
            ).delete(synchronize_session=False)
            session.query(models.User).filter(models.User.id.in_(user_ids)).update(
                {models.User.workspace_id: None},
                synchronize_session=False,
            )
        if workspace_ids:
            session.query(models.Project).filter(
                models.Project.workspace_id.in_(workspace_ids)
            ).delete(synchronize_session=False)
            session.query(models.Workspace).filter(
                models.Workspace.id.in_(workspace_ids)
            ).delete(synchronize_session=False)
        if user_ids:
            session.query(models.User).filter(models.User.id.in_(user_ids)).delete(
                synchronize_session=False
            )
        session.commit()
        session.close()


def _principal(user_id: int = 1) -> McpPrincipal:
    return McpPrincipal(
        user_id=user_id,
        email="owner@example.com",
        scopes={"versya.admin:ops"},
        token_claims={"sub": str(user_id)},
        server_type="admin",
        correlation_id="test-correlation",
    )


def test_protected_resource_metadata_uses_env(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("MCP_AUTH_ISSUER", "https://auth.example.com/realms/versya")
    monkeypatch.delenv("MCP_ADMIN_ENABLED", raising=False)

    metadata = protected_resource_metadata()

    assert metadata["resource"] == "https://api.example.com/mcp/"
    assert metadata["authorization_servers"] == ["https://auth.example.com/realms/versya"]
    assert "versya.templates:write" in metadata["scopes_supported"]
    assert AGENT_SESSION_SCOPE in metadata["scopes_supported"]
    assert AGENT_SESSION_SCOPE not in SUPPORTED_SCOPES
    assert "versya.admin:ops" not in metadata["scopes_supported"]


def test_product_metadata_never_advertises_admin_scope(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("MCP_AUTH_ISSUER", "https://auth.example.com/realms/versya")
    monkeypatch.setenv("MCP_ADMIN_ENABLED", "true")

    metadata = protected_resource_metadata()

    assert metadata["resource"] == "https://api.example.com/mcp/"
    assert "versya.projects:read" in metadata["scopes_supported"]
    assert "versya.admin:ops" not in metadata["scopes_supported"]


def test_product_oauth_challenge_requests_usable_bootstrap_scope(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://api.example.com")

    response = unauthorized_response(
        "Authentication required",
        oauth_challenge_scope("product"),
    )

    assert PRODUCT_BOOTSTRAP_SCOPE == "versya.projects:read"
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://api.example.com/.well-known/oauth-protected-resource", '
        'scope="versya.projects:read offline_access"'
    )
    assert oauth_challenge_scope("admin") == "versya.admin:ops"
    assert oauth_challenge_scope("product", "versya.lifecycle:write") == "versya.lifecycle:write"


def test_mcp_status_reports_long_running_oauth_readiness(monkeypatch):
    class FakeDb:
        def close(self):
            pass

    principal = McpPrincipal(
        user_id=1,
        email="owner@example.com",
        scopes={"versya.projects:read"},
        token_claims={
            "sub": "keycloak-subject",
            "scope": "profile email versya.projects:read offline_access",
            "exp": int((datetime.utcnow() + timedelta(minutes=5)).timestamp()),
        },
        server_type="product",
        correlation_id="test-correlation",
    )

    monkeypatch.setattr(mcp_tools, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(mcp_tools, "require_scopes", lambda _required: principal)
    monkeypatch.setattr(mcp_tools, "log_tool_call", lambda *_args, **_kwargs: None)

    output = mcp_tools.get_mcp_status()

    assert output["oauth_session"]["offline_access_granted"] is True
    assert output["oauth_session"]["refresh_mode"] == "offline"
    assert output["oauth_session"]["long_running_agent_ready"] is True
    assert output["oauth_session"]["action_required"] is None


def test_project_mcp_projection_never_exposes_internal_pii_salt():
    project = models.Project(
        id=1,
        name="Tenant project",
        description="Visible project metadata",
        workspace_id=7,
        is_active=True,
        pii_salt="secret-key-material",
        default_locale="pt-BR",
        supported_locales=["pt-BR", "en-US"],
        default_timezone="America/Sao_Paulo",
    )

    output = mcp_tools._project_public_row(project)

    assert output["id"] == 1
    assert output["default_locale"] == "pt-BR"
    assert "pii_salt" not in output


def test_project_onboarding_contract_is_tenant_safe_and_proposal_first():
    project = models.Project(
        id=1,
        name="Tenant project",
        workspace_id=7,
        is_active=True,
        pii_salt="secret-key-material",
        default_locale="pt-BR",
        supported_locales=["pt-BR"],
        default_timezone="America/Sao_Paulo",
    )

    output = mcp_tools._project_onboarding_contract_document(
        project,
        {
            "contacts": 0,
            "templates": 0,
            "funnels": 0,
            "campaigns": 0,
            "campaign_recipes": 0,
            "project_imports": 0,
            "lifecycle_models": 0,
        },
        [],
        {
            "base_project_inventory": "available",
            "lifecycle": "requires versya.lifecycle:read",
            "campaigns": "requires versya.campaigns:read",
            "imports": "requires versya.imports:read",
        },
    )

    assert output["contract_version"] == "1.2"
    assert output["external_sends"] == 0
    assert output["observed_inventory"]["counts"]["contacts"] == 0
    assert output["observed_inventory"]["scope_status"]["campaigns"] == (
        "requires versya.campaigns:read"
    )
    assert output["agent_mandate"]["evidence_labels"] == [
        "observed", "inferred", "recommended", "tenant_decided",
    ]
    assert output["configuration_ladder"][0]["phase"] == "01_discover"
    assert "get_project_onboarding_contract" in output["configuration_ladder"][0]["tools"]
    assert "create_project_setup_plan" in output["persistent_plan_contract"]["tools"]
    assert output["persistent_plan_contract"]["external_sends"] == 0
    assert "schedule_campaign_run" in output["configuration_ladder"][-1]["tools"]
    assert "pii_salt" not in output["project"]


def test_project_onboarding_contract_respects_optional_read_scopes(
    monkeypatch,
    mcp_db_session,
):
    user = models.User(
        email=_test_email("onboarding-scopes"),
        name="Owner",
        role="user",
        is_active=True,
        is_verified=True,
    )
    mcp_db_session.add(user)
    mcp_db_session.flush()
    workspace = models.Workspace(name="Workspace", owner_id=user.id, is_active=True)
    mcp_db_session.add(workspace)
    mcp_db_session.flush()
    user.workspace_id = workspace.id
    project = models.Project(
        workspace_id=workspace.id,
        name="New tenant",
        is_active=True,
        default_locale="pt-BR",
        default_timezone="America/Sao_Paulo",
    )
    mcp_db_session.add(project)
    mcp_db_session.commit()

    principal = McpPrincipal(
        user_id=user.id,
        email=user.email,
        scopes={"versya.projects:read"},
        token_claims={"sub": "keycloak-subject"},
        server_type="product",
        correlation_id="test-correlation",
        default_project_id=project.id,
    )
    tool_session = McpTestingSessionLocal()
    monkeypatch.setattr(mcp_tools, "SessionLocal", lambda: tool_session)
    monkeypatch.setattr(mcp_tools, "require_scopes", lambda _required: principal)
    monkeypatch.setattr(mcp_tools, "require_project_access", lambda *_args: None)
    monkeypatch.setattr(mcp_tools, "log_tool_call", lambda *_args, **_kwargs: None)

    output = mcp_tools.get_project_onboarding_contract(project_id=project.id)

    assert output["observed_inventory"]["counts"]["campaigns"] is None
    assert output["observed_inventory"]["counts"]["campaign_recipes"] is None
    assert output["observed_inventory"]["counts"]["project_imports"] is None
    assert output["observed_inventory"]["counts"]["lifecycle_models"] is None
    assert output["observed_inventory"]["lifecycle_models"] == []
    assert output["observed_inventory"]["scope_status"] == {
        "base_project_inventory": "available",
        "lifecycle": "requires versya.lifecycle:read",
        "campaigns": "requires versya.campaigns:read",
        "imports": "requires versya.imports:read",
    }


def test_product_mcp_registers_complete_agent_first_import_workflow():
    server = create_product_server()
    registered = set(server._tool_manager._tools)

    assert {
        "get_project_onboarding_contract",
        "get_agent_skill_catalog",
        "assess_project_setup",
        "create_project_setup_plan",
        "list_project_setup_plans",
        "get_project_setup_plan",
        "record_project_setup_decision",
        "approve_project_setup_phase",
        "compare_project_setup_plans",
        "set_project_setup_plan_status",
        "configure_project_foundation",
        "get_project_market_contract",
        "list_project_markets",
        "create_project_market",
        "update_project_market",
        "set_default_project_market",
        "set_project_market_status",
        "get_project_import_contract",
        "get_event_ingestion_contract",
        "get_event_ingestion_status",
        "create_event_ingestion_key",
        "create_project_import",
        "list_project_imports",
        "get_project_import",
        "validate_project_import",
        "apply_project_import",
        "audit_project_import",
        "list_project_import_records",
        "get_lifecycle_model_contract",
        "validate_lifecycle_model_definition",
        "create_lifecycle_model",
        "get_lifecycle_distribution",
        "compare_lifecycle_materialization",
        "explain_contact_lifecycle",
        "get_orchestration_contract",
        "get_attention_planning_contract",
        "preview_contact_attention_plan",
        "configure_attention_planning",
        "list_orchestration_purposes",
        "assess_orchestration_readiness",
        "get_campaign_recipe_contract",
        "preview_campaign_recipe",
        "list_campaign_recipes",
        "get_campaign_recipe",
        "create_draft_campaign_recipe",
        "update_campaign_recipe",
        "set_campaign_recipe_state",
        "get_campaign_planning_inbox",
        "materialize_campaign_episode",
        "list_campaigns",
        "get_campaign",
        "create_draft_campaign",
        "update_draft_campaign",
        "preview_campaign_plan",
        "preview_orchestration_impact",
        "activate_campaign",
        "evaluate_campaign_run_consequences",
        "choose_campaign_run_consequence",
        "schedule_campaign_run",
        "get_campaign_run_status",
        "cancel_campaign_run",
        "activate_funnel",
        "activate_template_automation",
        "set_event_action_state",
    }.issubset(registered)
    assert "versya.campaigns:read" in SUPPORTED_SCOPES
    assert "versya.campaigns:write" in SUPPORTED_SCOPES
    assert "versya.projects:write" in SUPPORTED_SCOPES
    assert "versya.ingestion:read" in SUPPORTED_SCOPES
    assert "versya.ingestion:write" in SUPPORTED_SCOPES


def test_event_ingestion_key_dry_run_is_least_privilege_and_send_free(monkeypatch):
    class FakeDb:
        def close(self):
            pass

    principal = McpPrincipal(
        user_id=1,
        email="owner@example.com",
        scopes={"versya.ingestion:write"},
        token_claims={"sub": "keycloak-subject"},
        server_type="product",
        correlation_id="test-correlation",
        default_project_id=7,
    )
    access = {}

    monkeypatch.setattr(mcp_tools, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(mcp_tools, "require_scopes", lambda _required: principal)
    monkeypatch.setattr(mcp_tools, "_resolve_project_id", lambda *_args: 7)
    monkeypatch.setattr(
        mcp_tools,
        "require_project_access",
        lambda _db, project_id, role: access.update(
            {"project_id": project_id, "role": role}
        ),
    )
    monkeypatch.setattr(mcp_tools, "log_tool_call", lambda *_args, **_kwargs: None)

    output = mcp_tools.create_event_ingestion_key(
        name="Source production",
        project_id=7,
        dry_run=True,
    )

    assert output["dry_run"] is True
    assert output["key"]["permissions"] == ["identify", "track"]
    assert output["external_sends"] == 0
    assert output["secret_returned"] is False
    assert access == {"project_id": 7, "role": "admin"}


def test_mcp_settings_fall_back_to_keycloak_env(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_ISSUER", raising=False)
    monkeypatch.delenv("MCP_AUTH_AUDIENCE", raising=False)
    monkeypatch.setenv("KEYCLOAK_URL", "https://auth.example.com")
    monkeypatch.setenv("KEYCLOAK_REALM", "customer")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "versya-frontend")

    settings = get_mcp_settings()

    assert settings.auth_issuer == "https://auth.example.com/realms/customer"
    assert settings.auth_audience == "versya-frontend"
    assert settings.admin_enabled is False


def test_redact_value_removes_nested_secrets():
    payload = {
        "name": "connection",
        "auth": {"api_key": "secret-value"},
        "items": [{"password": "hidden"}, {"safe": "visible"}],
    }

    redacted = redact_value(payload)

    assert redacted["auth"] == "[redacted]"
    assert redacted["items"][0]["password"] == "[redacted]"
    assert redacted["items"][1]["safe"] == "visible"
    assert redact_value({"headers": {"X-Versya-Upload-Token": "one-time-secret"}}) == {
        "headers": {"X-Versya-Upload-Token": "[redacted]"}
    }


def test_readonly_sql_adds_limit_and_rejects_mutations():
    assert _readonly_sql("select * from users").lower().endswith("limit 100")

    with pytest.raises(Exception):
        _readonly_sql("update users set role = 'admin'")

    with pytest.raises(Exception):
        _readonly_sql("select * from users; delete from users")


def test_event_action_partial_update_omits_unspecified_fields():
    payload = EventActionUpdate(
        purpose_key="aha.readiness_reminder",
        lane="transactional",
    )

    assert _model_dump_json(payload, exclude_unset=True) == {
        "purpose_key": "aha.readiness_reminder",
        "lane": "transactional",
    }


def test_handle_tool_success_audits_project_id_without_error_message(monkeypatch):
    class FakeDb:
        def close(self):
            pass

    principal = McpPrincipal(
        user_id=1,
        email="owner@example.com",
        scopes={"versya.templates:read"},
        token_claims={"sub": "keycloak-subject"},
        server_type="product",
        correlation_id="test-correlation",
        default_project_id=123,
    )
    captured = {}

    def fake_log_tool_call(
        db,
        principal_arg,
        tool_name,
        status,
        input_summary=None,
        output_summary=None,
        error_message=None,
        project_id=None,
    ):
        captured.update(
            {
                "tool_name": tool_name,
                "status": status,
                "input_summary": input_summary,
                "output_summary": output_summary,
                "error_message": error_message,
                "project_id": project_id,
            }
        )

    monkeypatch.setattr(mcp_tools, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(mcp_tools, "require_scopes", lambda _required: principal)
    monkeypatch.setattr(mcp_tools, "log_tool_call", fake_log_tool_call)

    output = mcp_tools._handle_tool(
        "list_templates",
        None,
        {"project_id": None},
        lambda _db, _principal: {"templates": []},
    )

    assert output == {"templates": []}
    assert captured["tool_name"] == "list_templates"
    assert captured["status"] == "success"
    assert captured["error_message"] is None
    assert captured["project_id"] == 123


def test_resolve_user_prefers_keycloak_id_then_email(mcp_db_session):
    keycloak_id = f"keycloak-{uuid4().hex}"
    email = _test_email("resolve")
    user = models.User(
        email=email,
        name="Owner",
        keycloak_id=keycloak_id,
        role="user",
        is_active=True,
        is_verified=True,
    )
    mcp_db_session.add(user)
    mcp_db_session.commit()
    mcp_db_session.refresh(user)

    assert resolve_user(mcp_db_session, {"sub": keycloak_id}).id == user.id
    assert resolve_user(mcp_db_session, {"sub": "other", "email": email}).id == user.id


def test_default_project_uses_connector_project(mcp_db_session):
    user = models.User(
        email=_test_email("default-project"),
        name="Owner",
        role="user",
        is_active=True,
        is_verified=True,
    )
    mcp_db_session.add(user)
    mcp_db_session.flush()

    workspace = models.Workspace(name="Workspace", owner_id=user.id, is_active=True)
    mcp_db_session.add(workspace)
    mcp_db_session.flush()
    user.workspace_id = workspace.id

    project = models.Project(workspace_id=workspace.id, name="Tabloide", is_active=True)
    mcp_db_session.add(project)
    mcp_db_session.flush()
    mcp_db_session.add(
        models.ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role="admin",
            is_active=True,
        )
    )
    mcp_db_session.commit()

    principal = McpPrincipal(
        user_id=user.id,
        email=user.email,
        scopes={"versya.projects:read"},
        token_claims={"sub": "keycloak-subject"},
        server_type="product",
        correlation_id="test-correlation",
        default_project_id=project.id,
    )

    assert _resolve_project_id(mcp_db_session, principal, None) == project.id


def test_admin_mcp_requires_platform_admin_role_not_workspace_owner(mcp_db_session):
    user = models.User(
        email=_test_email("workspace-owner-not-admin"),
        name="Workspace Owner",
        role="user",
        is_active=True,
        is_verified=True,
    )
    mcp_db_session.add(user)
    mcp_db_session.flush()

    workspace = models.Workspace(name="Workspace", owner_id=user.id, is_active=True)
    mcp_db_session.add(workspace)
    mcp_db_session.flush()
    user.workspace_id = workspace.id
    mcp_db_session.commit()

    assert is_mcp_admin_user(mcp_db_session, user) is False

    user.role = "admin"
    mcp_db_session.commit()
    mcp_db_session.refresh(user)

    assert is_mcp_admin_user(mcp_db_session, user) is True


def test_pending_action_confirmation_flow(mcp_db_session):
    user = models.User(
        email=_test_email("pending"),
        name="Owner",
        role="user",
        is_active=True,
        is_verified=True,
    )
    mcp_db_session.add(user)
    mcp_db_session.commit()
    mcp_db_session.refresh(user)
    principal = _principal(user.id)

    pending = create_pending_action(
        mcp_db_session,
        principal,
        "run_maintenance_action",
        {"action": "inspect_scheduler_state", "secret": "hidden"},
    )

    consumed = consume_pending_action(
        mcp_db_session,
        principal,
        "run_maintenance_action",
        pending["confirm_token"],
    )

    assert consumed.status == "confirmed"
    assert consumed.confirmed_at is not None


def test_pending_action_confirmation_is_bound_to_current_payload(mcp_db_session):
    user = models.User(
        email=_test_email("pending-binding"),
        name="Owner",
        role="user",
        is_active=True,
        is_verified=True,
    )
    mcp_db_session.add(user)
    mcp_db_session.commit()
    principal = _principal(user.id)
    approved = {
        "project_id": 1,
        "import_id": "import-1",
        "bundle_checksum": "a" * 64,
        "validation_fingerprint": "b" * 64,
    }
    pending = create_pending_action(
        mcp_db_session,
        principal,
        "apply_project_import",
        approved,
    )

    with pytest.raises(ValueError, match="no longer matches"):
        consume_pending_action(
            mcp_db_session,
            principal,
            "apply_project_import",
            pending["confirm_token"],
            expected_payload={**approved, "bundle_checksum": "c" * 64},
        )

    consumed = consume_pending_action(
        mcp_db_session,
        principal,
        "apply_project_import",
        pending["confirm_token"],
        expected_payload=approved,
    )
    assert consumed.status == "confirmed"


def test_pending_action_expiry(mcp_db_session):
    user = models.User(
        email=_test_email("expired"),
        name="Owner",
        role="user",
        is_active=True,
        is_verified=True,
    )
    mcp_db_session.add(user)
    mcp_db_session.flush()
    action = models.McpPendingAction(
        user_id=user.id,
        server_type="admin",
        tool_name="run_maintenance_action",
        action_payload={"action": "inspect_scheduler_state"},
        token_hash=hash_confirm_token("expired-token"),
        expires_at=datetime.utcnow() - timedelta(minutes=1),
    )
    mcp_db_session.add(action)
    mcp_db_session.commit()

    with pytest.raises(ValueError):
        consume_pending_action(
            mcp_db_session,
            _principal(user.id),
            "run_maintenance_action",
            "expired-token",
        )
