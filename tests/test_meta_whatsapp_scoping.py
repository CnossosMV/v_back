"""
Project-scoping tests for the Meta WhatsApp Cloud (BYOC) integration:
router RBAC, no-overwrite connect, phone uniqueness, fail-closed webhook,
deterministic inbound project resolution, and outbound instance validation.
"""
import hashlib
import hmac
import json
from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from app import models
from app.main import app
from app.routers.auth import get_current_user
from app.services.encryption_service import encrypt_value

TEST_KEY = Fernet.generate_key().decode()
APP_SECRET = "tenant-app-secret"


@pytest.fixture(autouse=True)
def wa_env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setenv("BACKEND_BASE_URL", "https://api.test.example")


@pytest.fixture
def seed(db_session):
    owner = models.User(email="wsowner@test.io", name="WS Owner")
    db_session.add(owner)
    db_session.flush()
    ws = models.Workspace(name="W1", owner_id=owner.id)
    db_session.add(ws)
    db_session.flush()
    admin_a = models.User(email="admina@test.io", name="Admin A", workspace_id=ws.id)
    viewer_a = models.User(email="viewera@test.io", name="Viewer A", workspace_id=ws.id)
    outsider = models.User(email="out@test.io", name="Out", workspace_id=ws.id)
    db_session.add_all([admin_a, viewer_a, outsider])
    db_session.flush()
    project_a = models.Project(name="A", workspace_id=ws.id)
    project_b = models.Project(name="B", workspace_id=ws.id)
    db_session.add_all([project_a, project_b])
    db_session.flush()
    db_session.add_all(
        [
            models.ProjectMember(project_id=project_a.id, user_id=admin_a.id, role="admin"),
            models.ProjectMember(project_id=project_a.id, user_id=viewer_a.id, role="viewer"),
            models.ProjectMember(project_id=project_b.id, user_id=admin_a.id, role="admin"),
        ]
    )
    db_session.commit()
    return {
        "workspace": ws,
        "admin_a": admin_a,
        "viewer_a": viewer_a,
        "outsider": outsider,
        "project_a": project_a,
        "project_b": project_b,
    }


def _instance(db, seed, project, phone_id="111", with_secret=True, **kw):
    inst = models.WhatsAppInstance(
        user_id=seed["admin_a"].id,
        workspace_id=seed["workspace"].id,
        project_id=project.id if project else None,
        provider_type="meta_cloud_api",
        instance_name=f"meta_{phone_id}_{project.id if project else 'x'}_{kw.pop('suffix', '0')}",
        meta_phone_number_id=phone_id,
        meta_waba_id="waba-1",
        meta_access_token_enc=encrypt_value("token"),
        meta_app_secret_enc=encrypt_value(APP_SECRET) if with_secret else None,
        meta_webhook_verify_token="vt-1",
        connection_status="connected",
        is_active=True,
        **kw,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def _auth_as(user):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def clear_auth_override():
    yield
    app.dependency_overrides.pop(get_current_user, None)


# ── Router RBAC + scoping ───────────────────────────────────────────────

class TestRouterScoping:
    def test_list_scoped_to_project(self, client, db_session, seed):
        _instance(db_session, seed, seed["project_a"], "111")
        _instance(db_session, seed, seed["project_b"], "222")

        _auth_as(seed["viewer_a"])
        resp = client.get(f"/api/v1/whatsapp/meta/project/{seed['project_a'].id}/instances")
        assert resp.status_code == 200
        phones = [i["phone_number_id"] for i in resp.json()]
        assert phones == ["111"]

    def test_viewer_cannot_connect(self, client, seed):
        _auth_as(seed["viewer_a"])
        resp = client.post(
            f"/api/v1/whatsapp/meta/project/{seed['project_a'].id}/connect",
            json={
                "phone_number_id": "999",
                "waba_id": "w",
                "access_token": "t",
                "app_secret": "s",
            },
        )
        assert resp.status_code == 403

    def test_non_member_403(self, client, seed):
        _auth_as(seed["outsider"])
        resp = client.get(f"/api/v1/whatsapp/meta/project/{seed['project_a'].id}/instances")
        assert resp.status_code == 403

    def test_cross_project_instance_404(self, client, db_session, seed):
        inst_b = _instance(db_session, seed, seed["project_b"], "222")
        _auth_as(seed["admin_a"])
        resp = client.get(
            f"/api/v1/whatsapp/meta/project/{seed['project_a'].id}/instances/{inst_b.id}/status"
        )
        assert resp.status_code == 404

    def test_connect_creates_project_scoped_instance(
        self, client, db_session, seed, monkeypatch
    ):
        from app.services import meta_cloud_api_service as mcs

        async def fake_validate(phone_number_id, access_token):
            return {"valid": True, "phone_number": "+551199", "verified_name": "Biz"}

        monkeypatch.setattr(mcs.meta_cloud_api_service, "validate_credentials", fake_validate)

        _auth_as(seed["admin_a"])
        resp = client.post(
            f"/api/v1/whatsapp/meta/project/{seed['project_a'].id}/connect",
            json={
                "phone_number_id": "333",
                "waba_id": "w",
                "access_token": "tok",
                "app_secret": "sec",
            },
        )
        assert resp.status_code == 200
        inst = (
            db_session.query(models.WhatsAppInstance)
            .filter(models.WhatsAppInstance.meta_phone_number_id == "333")
            .first()
        )
        assert inst.project_id == seed["project_a"].id
        assert inst.webhook_url.endswith(f"/api/v1/whatsapp/meta/webhook/{inst.id}")

    def test_no_overwrite_two_connects_two_instances(
        self, client, db_session, seed, monkeypatch
    ):
        from app.services import meta_cloud_api_service as mcs

        async def fake_validate(phone_number_id, access_token):
            return {"valid": True, "phone_number": "+551199", "verified_name": "Biz"}

        monkeypatch.setattr(mcs.meta_cloud_api_service, "validate_credentials", fake_validate)

        _auth_as(seed["admin_a"])
        for phone in ("444", "555"):
            resp = client.post(
                f"/api/v1/whatsapp/meta/project/{seed['project_a'].id}/connect",
                json={
                    "phone_number_id": phone,
                    "waba_id": "w",
                    "access_token": "tok",
                    "app_secret": "sec",
                },
            )
            assert resp.status_code == 200
        count = (
            db_session.query(models.WhatsAppInstance)
            .filter(
                models.WhatsAppInstance.project_id == seed["project_a"].id,
                models.WhatsAppInstance.provider_type == "meta_cloud_api",
            )
            .count()
        )
        assert count == 2

    def test_duplicate_phone_409(self, client, db_session, seed, monkeypatch):
        from app.services import meta_cloud_api_service as mcs

        async def fake_validate(phone_number_id, access_token):
            return {"valid": True}

        monkeypatch.setattr(mcs.meta_cloud_api_service, "validate_credentials", fake_validate)

        _instance(db_session, seed, seed["project_b"], "666")
        _auth_as(seed["admin_a"])
        resp = client.post(
            f"/api/v1/whatsapp/meta/project/{seed['project_a'].id}/connect",
            json={
                "phone_number_id": "666",
                "waba_id": "w",
                "access_token": "tok",
                "app_secret": "sec",
            },
        )
        assert resp.status_code == 409

    def test_connect_requires_app_secret(self, client, seed):
        _auth_as(seed["admin_a"])
        resp = client.post(
            f"/api/v1/whatsapp/meta/project/{seed['project_a'].id}/connect",
            json={
                "phone_number_id": "777",
                "waba_id": "w",
                "access_token": "tok",
                "app_secret": "",
            },
        )
        assert resp.status_code == 422


# ── Webhook fail-closed + routing ───────────────────────────────────────

class TestWebhook:
    def _payload(self):
        return {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "111"},
                                "messages": [
                                    {
                                        "from": "5511988887777",
                                        "id": "wamid.1",
                                        "type": "text",
                                        "timestamp": "1720000000",
                                        "text": {"body": "hi"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }

    def test_unsigned_rejected_when_secret_missing(self, client, db_session, seed):
        inst = _instance(db_session, seed, seed["project_a"], "111", with_secret=False)
        body = json.dumps(self._payload()).encode()
        resp = client.post(f"/api/v1/whatsapp/meta/webhook/{inst.id}", content=body)
        assert resp.status_code == 403

    def test_bad_signature_rejected(self, client, db_session, seed):
        inst = _instance(db_session, seed, seed["project_a"], "111")
        body = json.dumps(self._payload()).encode()
        resp = client.post(
            f"/api/v1/whatsapp/meta/webhook/{inst.id}",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert resp.status_code == 403

    def test_signed_payload_routes_to_instance_project(
        self, client, db_session, seed, monkeypatch
    ):
        inst = _instance(db_session, seed, seed["project_a"], "111")

        routed = []

        async def fake_route(self_router, message):
            routed.append(message)
            return {"success": True}

        from app.services.inbound.router import InboundRouter

        monkeypatch.setattr(InboundRouter, "route", fake_route)

        body = json.dumps(self._payload()).encode()
        sig = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        resp = client.post(
            f"/api/v1/whatsapp/meta/webhook/{inst.id}",
            content=body,
            headers={"X-Hub-Signature-256": sig},
        )
        assert resp.status_code == 200
        assert len(routed) == 1
        assert routed[0].instance_id == inst.id


# ── Inbound project resolution ──────────────────────────────────────────

class TestProjectFromInstance:
    def test_project_id_is_authoritative(self, db_session, seed):
        from app.services.inbound.router import InboundRouter

        inst = _instance(db_session, seed, seed["project_a"], "111")
        assert InboundRouter(db_session)._project_from_instance(inst) == seed["project_a"].id

    def test_legacy_single_project_fallback(self, db_session, seed):
        from app.services.inbound.router import InboundRouter

        # Deactivate project B so the workspace has a single active project
        seed["project_b"].is_active = False
        db_session.commit()
        inst = _instance(db_session, seed, None, "112", suffix="l")
        assert InboundRouter(db_session)._project_from_instance(inst) == seed["project_a"].id

    def test_ambiguous_returns_none_never_guesses(self, db_session, seed):
        from app.services.inbound.router import InboundRouter

        # Two active projects, no handler link, NULL project — must NOT route
        inst = _instance(db_session, seed, None, "113", suffix="m")
        assert InboundRouter(db_session)._project_from_instance(inst) is None


# ── Outbound resolution ─────────────────────────────────────────────────

class TestOutboundResolution:
    def test_explicit_cross_project_instance_rejected(self, db_session, seed):
        from app.services.channels.whatsapp_adapter import WhatsAppAdapter

        inst_a = _instance(db_session, seed, seed["project_a"], "111")
        resolved = WhatsAppAdapter._resolve_instance(
            db_session, seed["project_b"].id, {"instance_id": inst_a.id}
        )
        assert resolved is None

        same = WhatsAppAdapter._resolve_instance(
            db_session, seed["project_a"].id, {"instance_id": inst_a.id}
        )
        assert same is not None and same.id == inst_a.id

    def test_fallback_prefers_project_instance(self, db_session, seed):
        from app.services.channels.whatsapp_adapter import WhatsAppAdapter

        legacy = models.WhatsAppInstance(
            user_id=seed["admin_a"].id,
            workspace_id=seed["workspace"].id,
            project_id=None,
            provider_type="evolution_api",
            instance_name="evo_legacy_1",
            is_active=True,
        )
        db_session.add(legacy)
        db_session.commit()
        inst_a = _instance(db_session, seed, seed["project_a"], "111")

        resolved = WhatsAppAdapter._resolve_instance(db_session, seed["project_a"].id)
        assert resolved.id == inst_a.id

    def test_legacy_evolution_workspace_fallback_still_works(self, db_session, seed):
        from app.services.channels.whatsapp_adapter import WhatsAppAdapter

        legacy = models.WhatsAppInstance(
            user_id=seed["admin_a"].id,
            workspace_id=seed["workspace"].id,
            project_id=None,
            provider_type="evolution_api",
            instance_name="evo_legacy_2",
            is_active=True,
        )
        db_session.add(legacy)
        db_session.commit()

        resolved = WhatsAppAdapter._resolve_instance(db_session, seed["project_a"].id)
        assert resolved is not None and resolved.id == legacy.id

    def test_meta_null_project_not_used_as_fallback(self, db_session, seed):
        from app.services.channels.whatsapp_adapter import WhatsAppAdapter

        _instance(db_session, seed, None, "114", suffix="n")
        resolved = WhatsAppAdapter._resolve_instance(db_session, seed["project_a"].id)
        assert resolved is None


# ── Channel registry visibility ─────────────────────────────────────────

class TestRegistryVisibility:
    def test_meta_null_project_instance_not_visible_everywhere(self, db_session, seed):
        from app.services.channels.channel_registry_service import ChannelRegistryService

        _instance(db_session, seed, None, "115", suffix="r")
        svc = ChannelRegistryService(db_session)
        has_instances, count = svc._check_instances(seed["project_b"], "whatsapp")
        assert count == 0
