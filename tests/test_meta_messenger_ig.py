"""
Multi-tenant safety tests for the Messenger/Instagram Meta channel:
webhook signature + single-connection routing, OAuth session hygiene,
page-ownership uniqueness, cleanup worker, and outbound project scoping.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app import models
from app.main import app
from app.routers.auth import get_current_user

TEST_KEY = Fernet.generate_key().decode()
APP_SECRET = "test-meta-app-secret"
VERIFY_TOKEN = "test-verify-token"


@pytest.fixture(autouse=True)
def meta_env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setenv("META_APP_ID", "123456")
    monkeypatch.setenv("META_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("META_CONFIG_ID", "cfg-1")
    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("BACKEND_BASE_URL", "https://api.test.example")


@pytest.fixture
def seed(db_session):
    """Workspace with two projects; admin user is a member of project A only."""
    owner = models.User(email="owner@test.io", name="Owner")
    db_session.add(owner)
    db_session.flush()
    ws = models.Workspace(name="W1", owner_id=owner.id)
    db_session.add(ws)
    db_session.flush()
    # A second user is the workspace owner; the admin below is NOT the
    # workspace owner so require_project_role's bypass does not apply.
    admin = models.User(email="admin@test.io", name="Admin", workspace_id=ws.id)
    outsider = models.User(email="outsider@test.io", name="Outsider", workspace_id=ws.id)
    db_session.add_all([admin, outsider])
    db_session.flush()
    project_a = models.Project(name="Project A", workspace_id=ws.id)
    project_b = models.Project(name="Project B", workspace_id=ws.id)
    db_session.add_all([project_a, project_b])
    db_session.flush()
    db_session.add_all(
        [
            models.ProjectMember(project_id=project_a.id, user_id=admin.id, role="admin"),
            models.ProjectMember(project_id=project_b.id, user_id=admin.id, role="admin"),
        ]
    )
    db_session.commit()
    return {
        "workspace": ws,
        "admin": admin,
        "outsider": outsider,
        "project_a": project_a,
        "project_b": project_b,
    }


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _page_event(page_id: str) -> dict:
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": 1720000000,
                "messaging": [
                    {
                        "sender": {"id": "psid-1"},
                        "recipient": {"id": page_id},
                        "timestamp": 1720000000000,
                        "message": {"mid": "m.1", "text": "hello"},
                    }
                ],
            }
        ],
    }


def _connection(db, project_id, page_id="page-1", **kw):
    conn = models.MetaPageConnection(
        project_id=project_id,
        page_id=page_id,
        page_name=f"Page {page_id}",
        **kw,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


def _auth_as(user):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def clear_auth_override():
    yield
    app.dependency_overrides.pop(get_current_user, None)


# ── Webhook ─────────────────────────────────────────────────────────────

class TestMetaWebhook:
    def test_verify_handshake(self, client):
        resp = client.get(
            "/api/v1/meta/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "12345",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == 12345

    def test_verify_rejects_bad_token(self, client):
        resp = client.get(
            "/api/v1/meta/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "1",
            },
        )
        assert resp.status_code == 403

    def test_bad_signature_rejected(self, client):
        body = json.dumps(_page_event("page-1")).encode()
        resp = client.post(
            "/api/v1/meta/webhook",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert resp.status_code == 403

    def test_unknown_page_is_noop(self, client):
        body = json.dumps(_page_event("no-such-page")).encode()
        resp = client.post(
            "/api/v1/meta/webhook",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body)},
        )
        assert resp.status_code == 200

    def test_event_routes_to_exactly_one_connection(
        self, client, db_session, seed, monkeypatch
    ):
        """Two active connections for the same page (pre-index legacy data)
        must NOT fan out — exactly one dispatch, to the newest connection."""
        _connection(db_session, seed["project_a"].id, "page-1")
        conn_b = _connection(db_session, seed["project_b"].id, "page-1")

        dispatched = []

        async def fake_process(db, connection, channel, entry_id, event):
            dispatched.append(connection.id)

        from app.routers import meta_webhooks

        monkeypatch.setattr(meta_webhooks, "_process_messaging_event", fake_process)

        body = json.dumps(_page_event("page-1")).encode()
        resp = client.post(
            "/api/v1/meta/webhook",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body)},
        )
        assert resp.status_code == 200
        assert dispatched == [conn_b.id]


# ── Ownership uniqueness ────────────────────────────────────────────────

class TestPageOwnership:
    def _authorized_session(self, db, project_id, user_id, pages):
        from app.services.encryption_service import encrypt_value

        session = models.MetaOAuthSession(
            state="st-" + str(project_id),
            project_id=project_id,
            user_id=user_id,
            status="authorized",
            user_token_enc=encrypt_value("user-token"),
            pages_json=pages,
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    def test_finalize_rejects_page_active_in_other_project(
        self, client, db_session, seed, monkeypatch
    ):
        _connection(db_session, seed["project_a"].id, "page-1")
        session = self._authorized_session(
            db_session,
            seed["project_b"].id,
            seed["admin"].id,
            [
                {
                    "page_id": "page-1",
                    "name": "Shared Page",
                    "ig_account_id": None,
                    "ig_username": None,
                    "already_connected": False,
                }
            ],
        )

        from app.services import meta_messaging_service as mms

        async def fake_list_pages(token):
            return {
                "success": True,
                "pages": [
                    {
                        "page_id": "page-1",
                        "name": "Shared Page",
                        "access_token": "PAGE-TOKEN",
                        "ig_account_id": None,
                        "ig_username": None,
                    }
                ],
            }

        monkeypatch.setattr(mms.meta_messaging_service, "list_pages", fake_list_pages)

        _auth_as(seed["admin"])
        resp = client.post(
            f"/api/v1/projects/{seed['project_b'].id}/meta/oauth/sessions/{session.id}/finalize",
            json={
                "pages": [
                    {
                        "page_id": "page-1",
                        "messenger_enabled": True,
                        "instagram_enabled": False,
                        "fb_comments_enabled": False,
                        "ig_comments_enabled": False,
                    }
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["connections"] == []
        assert len(data["errors"]) == 1
        assert "already connected" in data["errors"][0]
        # No connection created in project B
        count_b = (
            db_session.query(models.MetaPageConnection)
            .filter(models.MetaPageConnection.project_id == seed["project_b"].id)
            .count()
        )
        assert count_b == 0

    def test_reactivation_conflict_409(self, client, db_session, seed):
        _connection(db_session, seed["project_a"].id, "page-1")
        inactive = _connection(
            db_session, seed["project_b"].id, "page-1", is_active=False, status="disconnected"
        )

        _auth_as(seed["admin"])
        resp = client.patch(
            f"/api/v1/projects/{seed['project_b'].id}/meta/connections/{inactive.id}",
            json={"is_active": True},
        )
        assert resp.status_code == 409


# ── OAuth session hygiene ───────────────────────────────────────────────

class TestOAuthSessionHygiene:
    def test_poll_response_never_contains_tokens(self, client, db_session, seed):
        from app.services.encryption_service import encrypt_value

        session = models.MetaOAuthSession(
            state="st-poll",
            project_id=seed["project_a"].id,
            user_id=seed["admin"].id,
            status="authorized",
            user_token_enc=encrypt_value("user-token"),
            pages_json=[
                {
                    "page_id": "page-9",
                    "name": "P9",
                    "ig_account_id": None,
                    "ig_username": None,
                    "already_connected": False,
                }
            ],
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        )
        db_session.add(session)
        db_session.commit()

        _auth_as(seed["admin"])
        resp = client.get(
            f"/api/v1/projects/{seed['project_a'].id}/meta/oauth/sessions/{session.id}"
        )
        assert resp.status_code == 200
        assert "access_token" not in resp.text
        assert "user_token" not in resp.text

    def test_cancel_purges_token(self, client, db_session, seed):
        from app.services.encryption_service import encrypt_value

        session = models.MetaOAuthSession(
            state="st-cancel",
            project_id=seed["project_a"].id,
            user_id=seed["admin"].id,
            status="authorized",
            user_token_enc=encrypt_value("user-token"),
            pages_json=[],
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        )
        db_session.add(session)
        db_session.commit()

        _auth_as(seed["admin"])
        resp = client.post(
            f"/api/v1/projects/{seed['project_a'].id}/meta/oauth/sessions/{session.id}/cancel"
        )
        assert resp.status_code == 200
        db_session.refresh(session)
        assert session.status == "cancelled"
        assert session.user_token_enc is None

    def test_expired_finalize_400_and_purges(self, client, db_session, seed):
        from app.services.encryption_service import encrypt_value

        session = models.MetaOAuthSession(
            state="st-exp",
            project_id=seed["project_a"].id,
            user_id=seed["admin"].id,
            status="authorized",
            user_token_enc=encrypt_value("user-token"),
            pages_json=[],
            expires_at=datetime.utcnow() - timedelta(minutes=1),
        )
        db_session.add(session)
        db_session.commit()

        _auth_as(seed["admin"])
        resp = client.post(
            f"/api/v1/projects/{seed['project_a'].id}/meta/oauth/sessions/{session.id}/finalize",
            json={"pages": []},
        )
        assert resp.status_code == 400
        db_session.refresh(session)
        assert session.user_token_enc is None
        assert session.status == "expired"

    def test_cleanup_worker_cycle(self, db_session, seed):
        from app.services.encryption_service import encrypt_value
        from app.services.meta_oauth_cleanup_worker import meta_oauth_cleanup_worker

        now = datetime.utcnow()
        expired_pending = models.MetaOAuthSession(
            state="st-w1",
            project_id=seed["project_a"].id,
            user_id=seed["admin"].id,
            status="pending",
            user_token_enc=encrypt_value("t1"),
            expires_at=now - timedelta(minutes=5),
        )
        completed_with_token = models.MetaOAuthSession(
            state="st-w2",
            project_id=seed["project_a"].id,
            user_id=seed["admin"].id,
            status="completed",
            user_token_enc=encrypt_value("t2"),
            expires_at=now + timedelta(minutes=30),
        )
        ancient = models.MetaOAuthSession(
            state="st-w3",
            project_id=seed["project_a"].id,
            user_id=seed["admin"].id,
            status="error",
            expires_at=now - timedelta(days=8),
        )
        db_session.add_all([expired_pending, completed_with_token, ancient])
        db_session.commit()
        ancient_id = ancient.id

        sanitized, deleted = meta_oauth_cleanup_worker._process_cycle(db_session)

        assert sanitized >= 2
        assert deleted == 1
        db_session.expire_all()
        assert expired_pending.user_token_enc is None
        assert expired_pending.status == "expired"
        assert completed_with_token.user_token_enc is None
        assert (
            db_session.query(models.MetaOAuthSession)
            .filter(models.MetaOAuthSession.id == ancient_id)
            .first()
            is None
        )


# ── RBAC ────────────────────────────────────────────────────────────────

class TestCommentReplyRBAC:
    def test_non_member_403(self, client, seed):
        _auth_as(seed["outsider"])
        resp = client.post(
            f"/api/v1/projects/{seed['project_a'].id}/support/tickets/1/comment-reply",
            json={"mode": "public", "content": "hi"},
        )
        assert resp.status_code == 403

    def test_member_unknown_ticket_404(self, client, seed):
        _auth_as(seed["admin"])
        resp = client.post(
            f"/api/v1/projects/{seed['project_a'].id}/support/tickets/99999/comment-reply",
            json={"mode": "public", "content": "hi"},
        )
        assert resp.status_code == 404


# ── Outbound scoping ────────────────────────────────────────────────────

class TestOutboundScoping:
    def test_explicit_instance_from_other_project_resolves_none(self, db_session, seed):
        from app.services.channels.meta_messaging_adapter import MessengerAdapter

        conn_a = _connection(db_session, seed["project_a"].id, "page-1")
        adapter = MessengerAdapter()
        resolved = adapter._resolve_connection(
            db_session, seed["project_b"].id, {"instance_id": conn_a.id}
        )
        assert resolved is None

        same_project = adapter._resolve_connection(
            db_session, seed["project_a"].id, {"instance_id": conn_a.id}
        )
        assert same_project is not None and same_project.id == conn_a.id
