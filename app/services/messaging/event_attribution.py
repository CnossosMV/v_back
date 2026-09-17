"""
Marketing attribution normalization for Versya event ingest.

The SDK and first-party apps may send attribution in slightly different shapes.
This module turns those hints into one compact structure used by destination
dispatchers and by first/last-touch profile storage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session

from app.models.messaging import MessagingAnonymousProfile, MessagingEvent, MessagingUser


UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
CLICK_ID_KEYS = ("gclid", "gbraid", "wbraid", "fbclid", "ttclid", "li_fat_id", "msclkid", "twclid", "epik")
COOKIE_KEYS = (
    "_fbp", "_fbc", "_ga", "_gcl_aw", "_gcl_gb", "_ttp", "ttp",
    "_vsya_gclid", "_vsya_gbraid", "_vsya_wbraid", "_vsya_fbclid",
    "_vsya_ttclid", "_vsya_li_fat_id", "_vsya_msclkid", "_vsya_twclid", "_vsya_epik",
)
@dataclass
class AttributionResult:
    attribution: Dict[str, Any]
    campaign_origin: str
    external_event_id: Optional[str]


def _first_non_empty(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        elif value:
            return str(value)
    return None


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _deep_get(source: Dict[str, Any], path: Iterable[str]) -> Any:
    current: Any = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _query_params(url: Optional[str]) -> Dict[str, str]:
    if not url:
        return {}
    try:
        parsed = urlparse(url)
        return {k: v[-1] for k, v in parse_qs(parsed.query).items() if v}
    except Exception:
        return {}


def _normalize_source(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    source = value.strip().lower()
    replacements = {
        "facebook": "meta",
        "fb": "meta",
        "instagram": "meta",
        "ig": "meta",
        "google ads": "google",
        "adwords": "google",
        "tiktok ads": "tiktok",
    }
    return replacements.get(source, source)


def _origin_from_referrer(referrer: Optional[str]) -> Optional[str]:
    if not referrer:
        return None
    try:
        hostname = urlparse(referrer).hostname or ""
    except Exception:
        return None
    host = hostname.lower()
    if "google." in host:
        return "google"
    if "facebook." in host or "instagram." in host or "meta." in host:
        return "meta"
    if "tiktok." in host:
        return "tiktok"
    if "linkedin." in host:
        return "linkedin"
    if host:
        return host.replace("www.", "")
    return None


def _campaign_origin(attribution: Dict[str, Any]) -> str:
    click_ids = attribution.get("click_ids") or {}
    cookies = attribution.get("cookies") or {}
    utm = attribution.get("utm") or {}
    page = attribution.get("page") or {}

    if click_ids.get("gclid") or click_ids.get("gbraid") or click_ids.get("wbraid") or cookies.get("_gcl_aw") or cookies.get("_gcl_gb"):
        return "google"
    if click_ids.get("fbclid") or cookies.get("_fbc"):
        return "meta"
    if click_ids.get("ttclid"):
        return "tiktok"
    if click_ids.get("li_fat_id"):
        return "linkedin"
    if click_ids.get("twclid"):
        return "x"
    if click_ids.get("epik"):
        return "pinterest"

    source = _normalize_source(utm.get("utm_source"))
    if source:
        return source

    referrer_origin = _origin_from_referrer(page.get("referrer"))
    if referrer_origin:
        return referrer_origin

    return "direct" if page.get("url") else "unknown"


def normalize_attribution(
    *,
    properties: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]],
    ip_address: Optional[str],
    user_agent: Optional[str],
    session_id: Optional[str],
) -> AttributionResult:
    props = _as_dict(properties)
    ctx = _as_dict(context)
    nested = (
        _as_dict(props.get("attribution"))
        or _as_dict(props.get("_attribution"))
        or _as_dict(props.get("marketing_attribution"))
        or _as_dict(ctx.get("attribution"))
    )
    ctx_page = _as_dict(ctx.get("page"))
    nested_page = _as_dict(nested.get("page"))

    page_url = _first_non_empty(
        nested_page.get("url"),
        nested.get("url"),
        props.get("url"),
        props.get("page_url"),
        ctx_page.get("url"),
    )
    query = _query_params(page_url)

    utm: Dict[str, str] = {}
    for key in UTM_KEYS:
        value = _first_non_empty(
            nested.get(key),
            _deep_get(nested, ("utm", key)),
            props.get(key),
            query.get(key),
        )
        if value:
            utm[key] = value

    click_ids: Dict[str, str] = {}
    for key in CLICK_ID_KEYS:
        value = _first_non_empty(
            nested.get(key),
            _deep_get(nested, ("click_ids", key)),
            props.get(key),
            query.get(key),
            _deep_get(nested, ("cookies", f"_vsya_{key}")),
            _deep_get(ctx, ("cookies", f"_vsya_{key}")),
        )
        if value:
            click_ids[key] = value

    click_meta: Dict[str, Dict[str, str]] = {}
    nested_click_meta = _as_dict(nested.get("click_meta"))
    for key in (*CLICK_ID_KEYS, "_fbc"):
        raw_meta = _as_dict(nested_click_meta.get(key))
        clean_meta = {
            field: value.strip()
            for field in ("captured_at", "expires_at", "source")
            if isinstance((value := raw_meta.get(field)), str) and value.strip()
        }
        if clean_meta:
            click_meta[key] = clean_meta

    cookies: Dict[str, str] = {}
    for key in COOKIE_KEYS:
        value = _first_non_empty(
            nested.get(key),
            _deep_get(nested, ("cookies", key)),
            _deep_get(ctx, ("cookies", key)),
            props.get(key),
        )
        if value:
            cookies[key] = value

    page = {
        "url": page_url,
        "path": _first_non_empty(nested_page.get("path"), props.get("path"), ctx_page.get("path")),
        "title": _first_non_empty(nested_page.get("title"), props.get("title"), ctx_page.get("title")),
        "referrer": _first_non_empty(
            nested_page.get("referrer"),
            nested.get("referrer"),
            props.get("referrer"),
            ctx_page.get("referrer"),
        ),
        "landing_page": _first_non_empty(nested.get("landing_page"), props.get("landing_page"), page_url),
    }
    page = {k: v for k, v in page.items() if v}

    attribution: Dict[str, Any] = {}
    if utm:
        attribution["utm"] = utm
    if click_ids:
        attribution["click_ids"] = click_ids
    if cookies:
        attribution["cookies"] = cookies
    if click_meta:
        attribution["click_meta"] = click_meta
    if page:
        attribution["page"] = page
    if ip_address:
        attribution["ip_address"] = ip_address
    if user_agent:
        attribution["user_agent"] = user_agent
    if session_id:
        attribution["session_id"] = session_id

    computed_origin = _campaign_origin(attribution)
    campaign_origin = computed_origin
    attribution["campaign_origin"] = campaign_origin

    external_event_id = _first_non_empty(
        props.get("event_id"),
        props.get("external_event_id"),
        nested.get("event_id"),
        ctx.get("event_id"),
    )

    return AttributionResult(
        attribution=attribution,
        campaign_origin=campaign_origin,
        external_event_id=external_event_id,
    )


def resolve_internal_send(
    db: Session,
    *,
    attribution: Dict[str, Any],
    project_id: int,
) -> Optional[int]:
    """Resolve a Versya tracked-link touch back to its SendLog.

    Outbound link rewriting appends utm_source=versya&utm_content=<tracking_token>
    to destination URLs. When an ingested event carries that token, the event
    is deterministically attributable to one specific send — MES gives it full
    credit and excludes it from other sends' temporal attribution.
    """
    utm = _as_dict(attribution.get("utm"))
    if _normalize_source(utm.get("utm_source")) != "versya":
        return None
    token = utm.get("utm_content")
    if not token or len(token) > 64:
        return None
    try:
        from app.models import SendLog
        row = (
            db.query(SendLog.id)
            .filter(
                SendLog.tracking_token == token,
                SendLog.project_id == project_id,
            )
            .first()
        )
        return row[0] if row else None
    except Exception:
        return None


def merge_profile_attribution(
    db: Session,
    *,
    event: MessagingEvent,
    user: Optional[MessagingUser],
    anonymous_id: Optional[str],
) -> None:
    if not event.attribution:
        return

    touch = {
        "campaign_origin": event.campaign_origin,
        "attribution": event.attribution,
        "event_id": event.external_event_id or str(event.id),
        "event_name": event.event_name,
        "at": datetime.utcnow().isoformat() + "Z",
    }

    if user:
        props = dict(user.properties or {})
        props.setdefault("first_touch_attribution", touch)
        props["last_touch_attribution"] = touch
        user.properties = props

    if anonymous_id:
        anon = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == event.project_id,
            MessagingAnonymousProfile.anonymous_id == anonymous_id,
        ).first()
        if anon:
            props = dict(anon.properties or {})
            props.setdefault("first_touch_attribution", touch)
            props["last_touch_attribution"] = touch
            anon.properties = props
