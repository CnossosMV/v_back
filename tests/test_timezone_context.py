from types import SimpleNamespace

from app.services.messaging.timezone_context import (
    apply_timezone_to_properties,
    apply_timezone_to_user,
    enrich_properties_with_timezone,
    is_valid_timezone,
    normalize_timezone_context,
)


def test_browser_context_is_promoted_to_event_properties():
    result = enrich_properties_with_timezone(
        {"plan": "trial"},
        {
            "timezone": "America/Sao_Paulo",
            "utc_offset_minutes": -180,
            "observed_at": "2026-07-24T15:00:00Z",
        },
    )

    assert result["plan"] == "trial"
    assert result["timezone"] == "America/Sao_Paulo"
    assert result["utc_offset_minutes"] == -180
    assert result["timezone_source"] == "browser"



def test_valid_context_replaces_invalid_timezone_property():
    result = enrich_properties_with_timezone(
        {"timezone": "Mars/Olympus"},
        {
            "timezone": "Europe/Madrid",
            "utc_offset_minutes": 120,
            "observed_at": "2026-07-24T15:00:00Z",
        },
    )

    assert result["timezone"] == "Europe/Madrid"

def test_invalid_timezone_is_ignored_without_rejecting_payload():
    assert not is_valid_timezone("Mars/Olympus")
    assert normalize_timezone_context({"timezone": "Mars/Olympus"}) == {}


def test_stale_retry_does_not_rewind_profile_timezone():
    current = {
        "timezone": "America/New_York",
        "timezone_source": "browser",
        "timezone_observed_at": "2026-07-24T15:00:00Z",
    }
    stale = {
        "timezone": "America/Sao_Paulo",
        "timezone_source": "browser",
        "timezone_observed_at": "2026-07-23T15:00:00Z",
    }

    assert apply_timezone_to_properties(current, stale) == current


def test_explicit_timezone_is_not_overwritten_by_browser():
    user = SimpleNamespace(
        timezone="Europe/Lisbon",
        properties={
            "timezone": "Europe/Lisbon",
            "timezone_source": "explicit",
            "timezone_observed_at": "2026-07-20T12:00:00Z",
        },
    )
    apply_timezone_to_user(
        user,
        {
            "timezone": "America/Sao_Paulo",
            "timezone_source": "browser",
            "timezone_observed_at": "2026-07-24T15:00:00Z",
        },
    )

    assert user.timezone == "Europe/Lisbon"
