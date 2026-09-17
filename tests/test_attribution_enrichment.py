from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.messaging import (
    DestinationType,
    MessagingAnonymousProfile,
    MessagingAttributionTouch,
    MessagingDestination,
    MessagingDestinationDelivery,
    MessagingEvent,
)
from app.services.messaging import attribution_resolver as resolver
from app.services.messaging.attribution_resolver import (
    record_attribution_touches,
    resolve_destination_attribution,
)
from app.services.messaging.destination_dispatcher import DestinationDispatcher
from app.services.messaging.event_attribution import normalize_attribution


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class _CandidateQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class _CaptureQuery:
    def filter(self, *_args):
        return self

    def first(self):
        return None


class _IdentityConflictSession:
    def query(self, model):
        if model is MessagingAnonymousProfile:
            return _CandidateQuery([SimpleNamespace(merged_to_user_id=999)])
        raise AssertionError("historical touches must not be queried after identity conflict")


class _CaptureSession:
    def __init__(self):
        self.rows = []

    def query(self, _model):
        return _CaptureQuery()

    def begin_nested(self):
        return nullcontext()

    def add(self, row):
        self.rows.append(row)

    def flush(self):
        for index, row in enumerate(self.rows, start=1):
            row.id = row.id or index


def _destination(destination_type):
    return MessagingDestination(
        id=10,
        project_id=1,
        destination_type=destination_type,
        name="Ads",
        is_active=True,
        consent_required=[],
    )


def _event(attribution=None, *, user_id=7, anonymous_id="anon-1", event_name="purchase", value=10):
    return MessagingEvent(
        id=20,
        project_id=1,
        user_id=user_id,
        anonymous_id=anonymous_id,
        event_name=event_name,
        properties={"value": value, "currency": "USD", "email": "buyer@example.com"},
        source="backend",
        attribution=attribution or {},
        client_ts=NOW,
        created_at=NOW,
        external_event_id=f"event-{event_name}",
    )


def _delivery(previous=None):
    return SimpleNamespace(
        id=30,
        project_id=1,
        attribution_resolution=previous,
    )


def _touch(provider, identifier_type, value, *, captured_at=None, expires_at=None):
    return MessagingAttributionTouch(
        id=40,
        project_id=1,
        user_id=7,
        anonymous_id="anon-old",
        provider=provider,
        identifier_type=identifier_type,
        identifier_value=value,
        identifier_hash="hash",
        capture_source="url",
        captured_at=captured_at or (NOW - timedelta(days=3)),
        expires_at=expires_at or (NOW + timedelta(days=177)),
        last_seen_at=NOW,
    )


@pytest.fixture
def healthy_first_party(monkeypatch):
    monkeypatch.setattr(
        resolver,
        "_first_party_capability",
        lambda _db, _project_id: {
            "configured": True,
            "healthy": True,
            "tracking_domain_id": 99,
        },
    )
    monkeypatch.setattr(
        resolver,
        "_selected_touch_from_retry",
        lambda _db, **_kwargs: None,
    )


def test_delivery_model_persists_attribution_resolution():
    assert "attribution_resolution" in MessagingDestinationDelivery.__table__.columns


def test_current_event_signal_has_priority(monkeypatch, healthy_first_party):
    def fail_historical(*_args, **_kwargs):
        raise AssertionError("historical lookup must not run")

    monkeypatch.setattr(resolver, "_eligible_touch_query", fail_historical)
    event = _event({
        "cookies": {"_fbc": "current-fbc", "_fbp": "current-fbp"},
        "page": {"url": "https://example.com/checkout"},
    })

    result = resolve_destination_attribution(
        object(),
        event=event,
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    assert result.attribution == event.attribution
    assert result.summary["selected_touch_id"] is None
    assert result.summary["field_sources"] == {"_fbc": "current_event"}


@pytest.mark.parametrize(
    ("destination_type", "provider", "identifier_type", "section"),
    [
        (DestinationType.meta_pixel, "meta", "_fbc", "cookies"),
        (DestinationType.google_ads, "google", "gclid", "click_ids"),
        (DestinationType.tiktok, "tiktok", "ttclid", "click_ids"),
    ],
)
def test_historical_touch_is_selected_per_provider(
    monkeypatch,
    healthy_first_party,
    destination_type,
    provider,
    identifier_type,
    section,
):
    touch = _touch(provider, identifier_type, f"{provider}-click")
    monkeypatch.setattr(
        resolver,
        "_eligible_touch_query",
        lambda *_args, **_kwargs: _CandidateQuery([touch]),
    )
    event = _event({
        "cookies": {"_fbp": "current-browser"},
        "page": {"url": "https://example.com/checkout"},
        "ip_address": "203.0.113.1",
    })

    result = resolve_destination_attribution(
        object(),
        event=event,
        destination=_destination(destination_type),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    assert result.attribution[section][identifier_type] == f"{provider}-click"
    assert result.attribution["page"] == event.attribution["page"]
    assert result.attribution["ip_address"] == event.attribution["ip_address"]
    assert result.summary["selected_touch_id"] == touch.id
    assert result.summary["fields_added"] == [identifier_type]


def test_historical_meta_fbclid_derives_fbc_from_click_time(monkeypatch, healthy_first_party):
    captured_at = NOW - timedelta(days=4)
    touch = _touch("meta", "fbclid", "meta-click", captured_at=captured_at)
    monkeypatch.setattr(
        resolver,
        "_eligible_touch_query",
        lambda *_args, **_kwargs: _CandidateQuery([touch]),
    )

    result = resolve_destination_attribution(
        object(),
        event=_event({"cookies": {"_fbp": "current-browser"}}),
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    expected_ms = int(captured_at.timestamp() * 1000)
    assert result.attribution["cookies"]["_fbc"] == f"fb.1.{expected_ms}.meta-click"
    assert result.summary["fields_added"] == ["_fbc"]


def test_diagnostic_mode_logs_but_does_not_apply(monkeypatch, healthy_first_party):
    touch = _touch("meta", "_fbc", "historical-fbc")
    monkeypatch.setattr(
        resolver,
        "_eligible_touch_query",
        lambda *_args, **_kwargs: _CandidateQuery([touch]),
    )
    event = _event({"cookies": {"_fbp": "current-browser"}})

    result = resolve_destination_attribution(
        object(),
        event=event,
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={
            "attribution_enrichment": "historical",
            "attribution_diagnostic_only": True,
        },
        consent_granted=True,
    )

    assert result.attribution == event.attribution
    assert result.summary["selected_touch_id"] == touch.id
    assert result.summary["payload_applied"] is False
    assert "diagnostic_only" in result.summary["warnings"]


def test_expired_touch_is_not_applied(monkeypatch, healthy_first_party):
    touch = _touch(
        "meta",
        "_fbc",
        "expired-fbc",
        captured_at=NOW - timedelta(days=181),
        expires_at=NOW - timedelta(days=1),
    )
    monkeypatch.setattr(
        resolver,
        "_eligible_touch_query",
        lambda *_args, **_kwargs: _CandidateQuery([touch]),
    )

    result = resolve_destination_attribution(
        object(),
        event=_event(),
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    assert result.attribution == {}
    assert "selected_touch_expired" in result.summary["warnings"]
    assert "no_eligible_historical_touch" in result.summary["warnings"]


def test_consent_denial_blocks_enrichment(monkeypatch, healthy_first_party):
    result = resolve_destination_attribution(
        object(),
        event=_event(),
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=False,
    )

    assert result.attribution == {}
    assert result.summary["consent_granted"] is False
    assert "consent_not_granted" in result.summary["warnings"]


def test_capture_uses_sdk_timestamps_and_ignores_browser_ids():
    db = _CaptureSession()
    captured_at = NOW - timedelta(days=10)
    expires_at = NOW + timedelta(days=170)
    event = _event({
        "click_ids": {"gclid": "google-click"},
        "cookies": {"_fbp": "browser-only", "_ttp": "browser-only"},
        "click_meta": {
            "gclid": {
                "captured_at": captured_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "source": "local_storage",
            }
        },
    })

    touches = record_attribution_touches(
        db,
        event=event,
        user=SimpleNamespace(id=7),
        anonymous_id="anon-1",
    )

    assert len(touches) == 1
    assert touches[0].identifier_type == "gclid"
    assert touches[0].capture_source == "local_storage"
    assert touches[0].captured_at == captured_at
    assert touches[0].expires_at == expires_at


def test_normalizer_preserves_click_capture_metadata():
    result = normalize_attribution(
        properties={
            "attribution": {
                "click_ids": {"gclid": "google-click"},
                "click_meta": {
                    "gclid": {
                        "captured_at": "2026-07-10T12:00:00Z",
                        "expires_at": "2027-01-06T12:00:00Z",
                        "source": "url",
                        "ignored": "not-persisted",
                    },
                    "unknown": {"captured_at": "2026-07-10T12:00:00Z"},
                },
            },
        },
        context={},
        ip_address=None,
        user_agent=None,
        session_id=None,
    )

    assert result.attribution["click_meta"] == {
        "gclid": {
            "captured_at": "2026-07-10T12:00:00Z",
            "expires_at": "2027-01-06T12:00:00Z",
            "source": "url",
        },
    }


def test_capture_caps_client_expiry_at_180_days():
    db = _CaptureSession()
    captured_at = NOW - timedelta(days=1)
    event = _event({
        "click_ids": {"gclid": "long-lived-click"},
        "click_meta": {
            "gclid": {
                "captured_at": captured_at.isoformat(),
                "expires_at": (captured_at + timedelta(days=365)).isoformat(),
                "source": "url",
            },
        },
    })

    touches = record_attribution_touches(
        db,
        event=event,
        user=None,
        anonymous_id=event.anonymous_id,
    )

    assert touches[0].expires_at == captured_at + timedelta(days=180)


def test_browser_ids_alone_do_not_define_campaign_origin():
    result = normalize_attribution(
        properties={
            "attribution": {
                "cookies": {"_fbp": "fb-browser", "_ttp": "tt-browser"},
                "page": {"url": "https://example.com/pricing"},
            }
        },
        context={},
        ip_address=None,
        user_agent=None,
        session_id=None,
    )

    assert result.campaign_origin == "direct"




def test_retry_reuses_the_original_resolution(monkeypatch, healthy_first_party):
    touch = _touch("meta", "_fbc", "retry-fbc")
    monkeypatch.setattr(
        resolver,
        "_selected_touch_from_retry",
        lambda *_args, **_kwargs: touch,
    )

    def fail_new_lookup(*_args, **_kwargs):
        raise AssertionError("retry must not choose a new touch")

    monkeypatch.setattr(resolver, "_eligible_touch_query", fail_new_lookup)
    previous = {"provider": "meta", "selected_touch_id": touch.id}

    result = resolve_destination_attribution(
        object(),
        event=_event(),
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(previous),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    assert result.attribution["cookies"]["_fbc"] == "retry-fbc"
    assert result.summary["selected_touch_id"] == touch.id


def test_retry_without_touch_does_not_select_a_new_one(monkeypatch, healthy_first_party):
    monkeypatch.setattr(
        resolver,
        "_selected_touch_from_retry",
        lambda *_args, **_kwargs: None,
    )

    def fail_reselection(*_args, **_kwargs):
        raise AssertionError("a retry must not select a different touch")

    monkeypatch.setattr(resolver, "_eligible_touch_query", fail_reselection)
    previous = {
        "version": 1,
        "provider": "meta",
        "selected_touch_id": 999,
    }
    result = resolve_destination_attribution(
        object(),
        event=_event(),
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(previous),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    assert result.attribution == {}
    assert result.summary["selected_touch_id"] is None
    assert "retry_touch_unavailable" in result.summary["warnings"]


def test_actual_identity_conflict_blocks_historical_query(healthy_first_party):
    warnings = []
    query = resolver._eligible_touch_query(
        _IdentityConflictSession(),
        event=_event(),
        user=SimpleNamespace(id=7),
        provider="meta",
        event_at=NOW,
        warnings=warnings,
    )

    assert query is None
    assert warnings == ["identity_conflict"]


def test_conflicted_touch_is_not_reused(monkeypatch, healthy_first_party):
    touch = _touch("meta", "_fbc", "conflicted-fbc")
    touch.provenance = {"identity_conflict": True}
    monkeypatch.setattr(
        resolver,
        "_eligible_touch_query",
        lambda *_args, **_kwargs: _CandidateQuery([touch]),
    )

    result = resolve_destination_attribution(
        object(),
        event=_event(),
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    assert result.attribution == {}
    assert "identity_conflict" in result.summary["warnings"]


def test_identity_conflict_uses_only_current_event(monkeypatch, healthy_first_party):
    def conflict_query(*_args, **kwargs):
        kwargs["warnings"].append("identity_conflict")
        return None

    monkeypatch.setattr(resolver, "_eligible_touch_query", conflict_query)
    event = _event({"cookies": {"_fbp": "current-browser"}})

    result = resolve_destination_attribution(
        object(),
        event=event,
        destination=_destination(DestinationType.meta_pixel),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    assert result.attribution == event.attribution
    assert "identity_conflict" in result.summary["warnings"]


def test_first_party_unavailable_does_not_block_captured_current_signal(monkeypatch):
    monkeypatch.setattr(
        resolver,
        "_first_party_capability",
        lambda *_args: {
            "configured": False,
            "healthy": False,
            "tracking_domain_id": None,
        },
    )
    event = _event({"click_ids": {"ttclid": "current-tiktok-click"}})

    result = resolve_destination_attribution(
        object(),
        event=event,
        destination=_destination(DestinationType.tiktok),
        delivery=_delivery(),
        user=SimpleNamespace(id=7),
        config={"attribution_enrichment": "historical"},
        consent_granted=True,
    )

    assert result.attribution == event.attribution
    assert result.summary["payload_applied"] is True
    assert "first_party_unavailable" in result.summary["warnings"]


def test_meta_preserves_purchase_and_funnel_score_values(monkeypatch):
    dispatcher = DestinationDispatcher()
    payloads = []

    def fake_post(_url, payload, **_kwargs):
        payloads.append(payload)
        return SimpleNamespace(status="sent")

    monkeypatch.setattr(dispatcher, "_post_json", fake_post)
    mapping = SimpleNamespace(destination_event_name="Purchase")
    for event_name, value in (("purchase", 29.9), ("funnel.score", 1000)):
        event = _event(
            {
                "cookies": {"_fbp": "fb.1.1.1"},
                "ip_address": "203.0.113.1",
                "user_agent": "UnitTest",
            },
            event_name=event_name,
            value=value,
        )
        context = {
            "config": {"pixel_id": "123", "access_token": "token"},
            "properties": event.properties,
            "attribution": event.attribution,
            "user": None,
            "event_id": event.external_event_id,
            "mapping_settings": {},
        }
        mapping.destination_event_name = "FunnelScore" if event_name == "funnel.score" else "Purchase"
        if event_name == "funnel.score":
            event.properties["meta_event_name"] = "FunnelScore"
            context["properties"] = event.properties
        dispatcher._send_meta(event, mapping, context)

    assert payloads[0]["data"][0]["custom_data"]["value"] == 29.9
    assert payloads[1]["data"][0]["custom_data"]["value"] == 1000
    assert payloads[1]["data"][0]["custom_data"]["meta_event_name"] == "FunnelScore"

def test_meta_advanced_matching_is_configuration_gated(monkeypatch):
    dispatcher = DestinationDispatcher()
    payloads = []

    def fake_post(_url, payload, **_kwargs):
        payloads.append(payload)
        return SimpleNamespace(status="sent")

    monkeypatch.setattr(dispatcher, "_post_json", fake_post)
    event = _event({
        "cookies": {"_fbp": "fb.1.1.1"},
        "ip_address": "203.0.113.1",
        "user_agent": "UnitTest",
    })
    context = {
        "config": {"pixel_id": "123", "access_token": "token"},
        "properties": event.properties,
        "attribution": event.attribution,
        "user": None,
        "event_id": event.external_event_id,
        "mapping_settings": {},
    }

    dispatcher._send_meta(event, SimpleNamespace(destination_event_name="Purchase"), context)
    assert "em" not in payloads[-1]["data"][0]["user_data"]

    context["config"]["advanced_matching"] = True
    dispatcher._send_meta(event, SimpleNamespace(destination_event_name="Purchase"), context)
    assert payloads[-1]["data"][0]["user_data"]["em"]


def test_google_enhanced_conversions_and_conversion_values(monkeypatch):
    dispatcher = DestinationDispatcher()
    payloads = []

    def fake_post(_url, payload, **_kwargs):
        payloads.append(payload)
        return SimpleNamespace(status="sent")

    monkeypatch.setattr(dispatcher, "_post_json", fake_post)
    base_context = {
        "config": {
            "customer_id": "123",
            "developer_token": "dev",
            "access_token": "access",
            "default_conversion_action_id": "456",
            "enhanced_conversions": True,
        },
        "attribution": {"click_ids": {"gclid": "click"}},
        "user": None,
        "event_id": "evt",
        "mapping_settings": {},
    }

    event = _event(event_name="purchase", value=29.9)
    context = {**base_context, "properties": event.properties}
    dispatcher._send_google_ads(event, SimpleNamespace(destination_event_name="purchase"), context)

    conversion = payloads[-1]["conversions"][0]
    assert conversion["conversionValue"] == 29.9
    assert conversion["userIdentifiers"][0].get("hashedEmail")

    funnel_event = _event(event_name="funnel.score", value=1000)
    funnel_context = {**base_context, "properties": funnel_event.properties, "event_id": "score"}
    dispatcher._send_google_ads(
        funnel_event,
        SimpleNamespace(destination_event_name="FunnelScore"),
        funnel_context,
    )
    assert payloads[-1]["conversions"][0]["conversionValue"] == 1000
