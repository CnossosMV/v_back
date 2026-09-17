from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.messaging import (
    DestinationType,
    MessagingDestination,
    MessagingDestinationDelivery,
    MessagingEvent,
    MessagingEventMapping,
    MessagingEventSchema,
)
from app.services.messaging.destination_dispatcher import (
    AD_RELEVANT_DEFAULTS,
    DestinationDispatcher,
    _ga_client_id,
    _google_ads_datetime,
    _google_ads_api_version,
)
from app.services.messaging.event_attribution import normalize_attribution


class _NestedTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def join(self, *_args, **_kwargs):
        return self

    def filter(self, *criteria):
        rows = self._rows
        for criterion in criteria:
            rows = [row for row in rows if _matches_criterion(row, criterion)]
        self._rows = rows
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, *, destinations, schemas, mappings):
        self.destinations = destinations
        self.schemas = schemas
        self.mappings = mappings
        self.deliveries = []
        self._next_delivery_id = 1

    def query(self, model):
        if model is MessagingDestination:
            return _FakeQuery(self.destinations)
        if model is MessagingEventSchema:
            return _FakeQuery(self.schemas)
        if model is MessagingEventMapping:
            return _FakeQuery(self.mappings)
        if model is MessagingDestinationDelivery:
            return _FakeQuery(self.deliveries)
        return _FakeQuery([])

    def add(self, row):
        if isinstance(row, MessagingDestinationDelivery) and row not in self.deliveries:
            self.deliveries.append(row)

    def flush(self):
        for delivery in self.deliveries:
            if delivery.id is None:
                delivery.id = self._next_delivery_id
                self._next_delivery_id += 1

    def commit(self):
        pass

    def begin_nested(self):
        return _NestedTransaction()


def _criterion_value(criterion):
    right = getattr(criterion, "right", None)
    if hasattr(right, "value"):
        return right.value
    text = str(right).lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _matches_criterion(row, criterion):
    left = getattr(criterion, "left", None)
    key = getattr(left, "key", None)
    table_name = getattr(getattr(left, "table", None), "name", None)
    if not key:
        return True
    target = row.destination if table_name == "messaging_destinations" and hasattr(row, "destination") else row
    return getattr(target, key, None) == _criterion_value(criterion)


def _create_ads_fixture(
    *,
    event_name="purchase",
    destination_event_name="Purchase",
    destination_type=DestinationType.meta_pixel,
):
    project = SimpleNamespace(id=101)
    schema = MessagingEventSchema(
        id=201,
        project_id=project.id,
        event_name=event_name,
        display_name=event_name,
        is_active=True,
    )
    destination = MessagingDestination(
        id=301,
        project_id=project.id,
        destination_type=destination_type,
        name=f"{destination_type.value} destination",
        is_active=True,
    )
    mapping = MessagingEventMapping(
        id=401,
        project_id=project.id,
        event_schema_id=schema.id,
        destination_id=destination.id,
        destination_event_name=destination_event_name,
        property_mappings=[],
        is_active=True,
    )
    mapping.destination = destination
    session = _FakeSession(destinations=[destination], schemas=[schema], mappings=[mapping])
    return session, project, schema, destination, mapping


def _create_event(event_id, project_id, *, event_name="purchase", external_event_id=None, properties=None):
    return MessagingEvent(
        id=event_id,
        project_id=project_id,
        event_name=event_name,
        external_event_id=external_event_id,
        properties=properties or {},
        source="backend",
    )


def test_attribution_click_id_wins_over_provided_origin():
    result = normalize_attribution(
        properties={
            "campaign_origin": "direct",
            "event_id": "evt_123",
            "url": "https://tabloide.pro/pricing?gclid=g-1&utm_source=facebook",
        },
        context={},
        ip_address="203.0.113.1",
        user_agent="UnitTest",
        session_id="session-1",
    )

    assert result.campaign_origin == "google"
    assert result.external_event_id == "evt_123"
    assert result.attribution["click_ids"]["gclid"] == "g-1"


def test_attribution_utm_and_referrer_fallbacks():
    utm_result = normalize_attribution(
        properties={"utm_source": "instagram"},
        context={},
        ip_address=None,
        user_agent=None,
        session_id=None,
    )
    referrer_result = normalize_attribution(
        properties={"referrer": "https://www.tiktok.com/@brand"},
        context={},
        ip_address=None,
        user_agent=None,
        session_id=None,
    )

    assert utm_result.campaign_origin == "meta"
    assert referrer_result.campaign_origin == "tiktok"


def test_ga_cookie_client_id_is_stripped_for_measurement_protocol():
    assert _ga_client_id("GA1.1.123456789.987654321") == "123456789.987654321"
    assert _ga_client_id("plain-client-id") == "plain-client-id"


def test_google_ads_conversion_datetime_has_colon_timezone():
    event = SimpleNamespace(
        client_ts=datetime(2026, 5, 15, 12, 30, tzinfo=timezone.utc),
        created_at=None,
    )

    assert _google_ads_datetime(event) == "2026-05-15 12:30:00+00:00"


def test_google_ads_api_version_upgrades_retired_v19():
    assert _google_ads_api_version("v19") == "v24"
    assert _google_ads_api_version(None) == "v24"
    assert _google_ads_api_version("v22") == "v22"


def test_google_ads_payload_uses_click_id_and_conversion_action(monkeypatch):
    dispatcher = DestinationDispatcher()
    captured = {}

    def fake_post(url, payload, *, headers=None, request_summary=None):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        captured["request_summary"] = request_summary
        return SimpleNamespace(status="sent")

    monkeypatch.setattr(dispatcher, "_post_json", fake_post)

    result = dispatcher._send_google_ads(
        SimpleNamespace(
            client_ts=datetime(2026, 5, 15, 12, 30, tzinfo=timezone.utc),
            created_at=None,
            properties={"value": 99.9, "currency": "BRL", "transaction_id": "order-1"},
            attribution={"click_ids": {"gclid": "click-1"}},
        ),
        SimpleNamespace(destination_event_name="purchase"),
        {
            "config": {
                "customer_id": "1234567890",
                "developer_token": "dev-token",
                "access_token": "access-token",
                "default_conversion_action_id": "555",
            },
            "properties": {"value": 99.9, "currency": "BRL", "transaction_id": "order-1"},
            "attribution": {"click_ids": {"gclid": "click-1"}},
            "user": None,
            "event_id": "evt-1",
            "mapping_settings": {},
        },
    )

    assert result.status == "sent"
    assert captured["url"] == "https://googleads.googleapis.com/v24/customers/1234567890:uploadClickConversions"
    assert captured["payload"]["conversions"][0]["gclid"] == "click-1"
    assert captured["payload"]["conversions"][0]["conversionAction"] == "customers/1234567890/conversionActions/555"
    assert captured["request_summary"]["click_id_type"] == "gclid"


def test_google_ads_skips_without_click_id():
    dispatcher = DestinationDispatcher()

    result = dispatcher._send_google_ads(
        SimpleNamespace(
            client_ts=datetime(2026, 5, 15, 12, 30, tzinfo=timezone.utc),
            created_at=None,
            properties={"value": 99.9},
            attribution={"click_ids": {}},
        ),
        SimpleNamespace(destination_event_name="purchase"),
        {
            "config": {
                "customer_id": "1234567890",
                "developer_token": "dev-token",
                "access_token": "access-token",
                "default_conversion_action_id": "555",
            },
            "properties": {"value": 99.9},
            "attribution": {"click_ids": {}},
            "user": None,
            "event_id": "evt-1",
            "mapping_settings": {},
        },
    )

    assert result.status == "skipped"
    assert "click ID missing" in result.error_message


def test_meta_test_event_code_is_top_level(monkeypatch):
    dispatcher = DestinationDispatcher()
    captured = {}

    def fake_post(url, payload, *, headers=None, request_summary=None):
        captured["payload"] = payload
        return SimpleNamespace(status="sent")

    monkeypatch.setattr(dispatcher, "_post_json", fake_post)

    result = dispatcher._send_meta(
        SimpleNamespace(
            client_ts=datetime(2026, 5, 15, 12, 30, tzinfo=timezone.utc),
            created_at=None,
            properties={"email": "buyer@example.com", "value": 10, "currency": "BRL"},
            attribution={
                "cookies": {"_fbp": "fb.1.1.1"},
                "page": {"url": "https://tabloide.pro/pricing"},
                "ip_address": "203.0.113.1",
                "user_agent": "UnitTest",
            },
        ),
        SimpleNamespace(destination_event_name="Purchase"),
        {
            "config": {"pixel_id": "123", "access_token": "token", "test_event_code": "TEST123"},
            "properties": {"email": "buyer@example.com", "value": 10, "currency": "BRL"},
            "attribution": {
                "cookies": {"_fbp": "fb.1.1.1"},
                "page": {"url": "https://tabloide.pro/pricing"},
                "ip_address": "203.0.113.1",
                "user_agent": "UnitTest",
            },
            "user": None,
            "event_id": "evt-1",
            "mapping_settings": {},
        },
    )

    assert result.status == "sent"
    assert captured["payload"]["test_event_code"] == "TEST123"
    assert "test_event_code" not in captured["payload"]["data"][0]


def test_funnel_score_default_mapping_is_meta_only():
    assert AD_RELEVANT_DEFAULTS["funnel.score"] == {
        "meta_pixel": {"event_name": "FunnelScore", "is_active": True},
    }


def test_queue_for_event_dedupes_mapped_deliveries_by_effective_external_event_id():
    dispatcher = DestinationDispatcher()
    session, project, _, _, _ = _create_ads_fixture()
    event_a = _create_event(
        501,
        project.id,
        external_event_id="tabloide:purchase:order-1",
        properties={"value": 10},
    )
    event_b = _create_event(
        502,
        project.id,
        properties={"event_id": "tabloide:purchase:order-1", "value": 10},
    )

    first = dispatcher.queue_for_event(session, event_a)
    second = dispatcher.queue_for_event(session, event_b)

    deliveries = session.deliveries
    assert len(deliveries) == 1
    assert first[0].id == second[0].id == deliveries[0].id
    assert deliveries[0].dedupe_key.endswith(":purchase:Purchase:tabloide:purchase:order-1")


def test_queue_for_event_keeps_distinct_external_event_ids_separate():
    dispatcher = DestinationDispatcher()
    session, project, _, _, _ = _create_ads_fixture()
    event_a = _create_event(501, project.id, external_event_id="tabloide:purchase:order-1")
    event_b = _create_event(502, project.id, external_event_id="tabloide:purchase:order-2")

    dispatcher.queue_for_event(session, event_a)
    dispatcher.queue_for_event(session, event_b)

    deliveries = session.deliveries
    assert len(deliveries) == 2
    assert {delivery.event_id for delivery in deliveries} == {event_a.id, event_b.id}


def test_queue_for_event_without_external_event_id_falls_back_to_event_id():
    dispatcher = DestinationDispatcher()
    session, project, _, destination, mapping = _create_ads_fixture()
    event_a = _create_event(501, project.id, properties={"value": 10})
    event_b = _create_event(502, project.id, properties={"value": 10})

    dispatcher.queue_for_event(session, event_a)
    dispatcher.queue_for_event(session, event_b)

    deliveries = session.deliveries
    assert len(deliveries) == 2
    assert {delivery.dedupe_key for delivery in deliveries} == {
        f"{project.id}:{destination.id}:{event_a.id}:{mapping.id}",
        f"{project.id}:{destination.id}:{event_b.id}:{mapping.id}",
    }


def test_queue_for_event_keeps_destinations_separate_for_same_external_event_id():
    dispatcher = DestinationDispatcher()
    session, project, schema, first_destination, _ = _create_ads_fixture()
    second_destination = MessagingDestination(
        id=302,
        project_id=project.id,
        destination_type=DestinationType.tiktok,
        name="TikTok destination",
        is_active=True,
    )
    second_mapping = MessagingEventMapping(
        id=402,
        project_id=project.id,
        event_schema_id=schema.id,
        destination_id=second_destination.id,
        destination_event_name="CompletePayment",
        property_mappings=[],
        is_active=True,
    )
    second_mapping.destination = second_destination
    session.destinations.append(second_destination)
    session.mappings.append(second_mapping)
    event_a = _create_event(501, project.id, external_event_id="tabloide:purchase:order-1")
    event_b = _create_event(502, project.id, external_event_id="tabloide:purchase:order-1")

    dispatcher.queue_for_event(session, event_a)
    dispatcher.queue_for_event(session, event_b)

    deliveries = session.deliveries
    assert len(deliveries) == 2
    assert {delivery.destination_id for delivery in deliveries} == {
        first_destination.id,
        second_mapping.destination_id,
    }
