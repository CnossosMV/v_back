from types import SimpleNamespace

from app.services.messaging.event_attribution import normalize_attribution
from app.services.messaging.tracking_domain_service import (
    dns_instructions,
    edge_config_for_domain,
    normalize_hostname,
    parent_cookie_domain,
)


def test_normalize_tracking_hostname():
    assert normalize_hostname("https://Track.Example.com/path") == "track.example.com"
    assert normalize_hostname("track.example.com:443") == "track.example.com"


def test_parent_cookie_domain_for_subdomain():
    assert parent_cookie_domain("track.example.com") == ".example.com"
    assert parent_cookie_domain("track.example.co.uk") == ".example.co.uk"
    assert parent_cookie_domain("example.com") is None


def test_tracking_dns_instructions():
    domain = SimpleNamespace(
        hostname="track.example.com",
        cname_target="tracking.versya.io",
        verification_token="trk_test",
    )

    instructions = dns_instructions(domain)

    assert instructions["cname_name"] == "track.example.com"
    assert instructions["cname_target"] == "tracking.versya.io"
    assert instructions["txt_name"] == "_versya.track.example.com"
    assert instructions["txt_value"] == "versya-tracking-verify=trk_test"


def test_edge_config_uses_linked_write_key(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "https://api.versya.test")
    monkeypatch.setenv("SDK_CDN_URL", "https://cdn.versya.test")
    monkeypatch.delenv("TRACKING_SDK_ORIGIN", raising=False)
    domain = SimpleNamespace(
        id=12,
        project_id=3,
        hostname="track.example.com",
        domain=SimpleNamespace(write_key="pk_live_test"),
        cookie_keeper_enabled=True,
        config_version=4,
    )

    config = edge_config_for_domain(domain)

    assert config["write_key"] == "pk_live_test"
    assert config["api_origin"] == "https://api.versya.test"
    assert config["sdk_origin"] == "https://api.versya.test"
    assert config["cookie_domain"] == ".example.com"
    assert "/api/v1/track" in config["proxy_paths"]


def test_edge_config_allows_explicit_tracking_sdk_origin(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "https://api.versya.test")
    monkeypatch.setenv("SDK_CDN_URL", "https://cdn.versya.test")
    monkeypatch.setenv("TRACKING_SDK_ORIGIN", "https://sdk-proxy.versya.test")
    domain = SimpleNamespace(
        id=12,
        project_id=3,
        hostname="track.example.com",
        domain=SimpleNamespace(write_key="pk_live_test"),
        cookie_keeper_enabled=True,
        config_version=4,
    )

    config = edge_config_for_domain(domain)

    assert config["api_origin"] == "https://api.versya.test"
    assert config["sdk_origin"] == "https://sdk-proxy.versya.test"


def test_attribution_reads_extended_click_ids_and_proxy_cookies():
    result = normalize_attribution(
        properties={"url": "https://example.com/?twclid=tw-1"},
        context={"cookies": {"_vsya_epik": "pin-1"}},
        ip_address=None,
        user_agent=None,
        session_id=None,
    )

    assert result.attribution["click_ids"]["twclid"] == "tw-1"
    assert result.attribution["click_ids"]["epik"] == "pin-1"
    assert result.campaign_origin == "x"
