import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers.messaging.public_api import (
    _backend_external_event_id,
    _deep_merge_properties,
    _existing_backend_event,
    get_domain_from_write_key,
)
from app.schemas.messaging import BackendEventRequest, TrackRequest
from app.services.messaging.event_sources import (
    BACKEND,
    FRONTEND,
    PUBLIC_API,
    origin_matches_domain,
    source_for_public_request,
)


def _request(origin=None, referer=None):
    headers = []
    if origin:
        headers.append((b"origin", origin.encode()))
    if referer:
        headers.append((b"referer", referer.encode()))
    return Request({"type": "http", "headers": headers})


def _domain():
    return SimpleNamespace(
        id=1,
        project_id=7,
        domain="shop.example.com",
        allowed_origins=["https://checkout.example.com"],
        rate_limit_per_minute=100,
        rate_limit_per_day=10000,
    )


def test_public_source_is_frontend_only_with_registered_browser_origin():
    domain = _domain()

    assert source_for_public_request(_request(origin="https://shop.example.com"), domain) == FRONTEND
    assert source_for_public_request(_request(origin="https://www.shop.example.com"), domain) == FRONTEND
    assert source_for_public_request(_request(referer="https://checkout.example.com/order/1"), domain) == FRONTEND
    assert source_for_public_request(_request(), domain) == PUBLIC_API
    assert not origin_matches_domain("https://shop.example.com.attacker.test", domain)


def test_invalid_present_origin_remains_forbidden():
    class QueryStub:
        def filter(self, *args):
            return self

        def first(self):
            return _domain()

    class DBStub:
        def query(self, *args):
            return QueryStub()

    with pytest.raises(HTTPException) as exc:
        get_domain_from_write_key(
            request=_request(origin="https://attacker.test"),
            x_write_key="pk_live_test",
            authorization=None,
            db=DBStub(),
        )

    assert exc.value.status_code == 403


def test_payload_cannot_override_server_owned_provenance():
    public_payload = TrackRequest(event="evento_customizado", source=BACKEND)
    backend_payload = BackendEventRequest(event="server_event", user_id="u-1", source=FRONTEND)

    assert not hasattr(public_payload, "source")
    assert not hasattr(backend_payload, "source")


def test_backend_event_separates_occurrence_and_current_contact_facts():
    payload = BackendEventRequest(
        event="subscription.started",
        event_id="source:subscription:123",
        user_id="contact-1",
        properties={"plan": "free", "event_id": "legacy-value"},
        contact_properties={
            "source": {"commercial": {"active_trial": True, "active_paid": False}},
        },
    )

    assert _backend_external_event_id(payload) == "source:subscription:123"
    assert payload.properties["plan"] == "free"
    assert payload.contact_properties["source"]["commercial"]["active_trial"] is True


def test_contact_property_merge_preserves_namespaces_and_explicit_clears():
    current = {
        "attribution": {"utm_source": "newsletter"},
        "source": {"commercial": {"active_trial": True, "active_paid": False}},
    }
    incoming = {
        "source": {"commercial": {"active_trial": False, "active_paid": True, "next_billing_at": None}},
    }

    merged = _deep_merge_properties(current, incoming)

    assert merged["attribution"] == {"utm_source": "newsletter"}
    assert merged["source"]["commercial"] == {
        "active_trial": False,
        "active_paid": True,
        "next_billing_at": None,
    }


def test_older_namespaced_snapshot_cannot_regress_current_truth():
    current = {
        "source": {
            "_observed_at": "2026-08-27T14:00:00Z",
            "commercial": {"active_trial": False, "active_paid": True},
        },
    }
    delayed_retry = {
        "source": {
            "_observed_at": "2026-08-27T13:00:00Z",
            "commercial": {"active_trial": True, "active_paid": False},
        },
    }

    merged = _deep_merge_properties(current, delayed_retry)

    assert merged == current


def test_backend_event_id_uses_postgres_transaction_lock_before_lookup():
    executed = []

    class QueryStub:
        def filter(self, *args):
            return self

        def order_by(self, *args):
            return self

        def first(self):
            return None

    class DBStub:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement, params):
            executed.append((str(statement), params))

        def query(self, *args):
            return QueryStub()

    payload = BackendEventRequest(
        event="subscription.started",
        event_id="source:event:123",
        user_id="contact-1",
    )

    assert _existing_backend_event(
        DBStub(), 7, payload, SimpleNamespace(id=11),
    ) is None
    assert "pg_advisory_xact_lock" in executed[0][0]
    assert executed[0][1] == {"project_id": 7, "event_id": "source:event:123"}


def test_every_orm_event_producer_declares_source_explicitly():
    missing = []
    app_root = Path(__file__).resolve().parents[1] / "app"
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name == "MessagingEvent" and not any(
                keyword.arg == "source" for keyword in node.keywords
            ):
                missing.append(f"{path.relative_to(app_root)}:{node.lineno}")

    assert missing == []
