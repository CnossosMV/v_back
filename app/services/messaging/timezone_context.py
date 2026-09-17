"""Normalize browser timezone context at messaging ingestion boundaries."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_VALID_SOURCES = {"browser", "observed", "explicit", "market_default", "legacy_default"}
_MAX_OFFSET_MINUTES = 14 * 60


def _parse_observed_at(value: Any, *, received_at: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = received_at
    else:
        parsed = received_at

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    if parsed > received_at.replace(tzinfo=timezone.utc):
        return received_at.replace(tzinfo=timezone.utc)
    return parsed


def is_valid_timezone(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        ZoneInfo(value)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def normalize_timezone_context(
    context: Optional[Dict[str, Any]],
    properties: Optional[Dict[str, Any]] = None,
    *,
    trusted_properties: bool = False,
    received_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return a validated, flat timezone snapshot."""
    received = received_at or datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    else:
        received = received.astimezone(timezone.utc)

    ctx = context if isinstance(context, dict) else {}
    props = properties if isinstance(properties, dict) else {}
    candidate = props if trusted_properties and is_valid_timezone(props.get("timezone")) else ctx
    if not is_valid_timezone(candidate.get("timezone")):
        if candidate is not ctx and is_valid_timezone(ctx.get("timezone")):
            candidate = ctx
        elif is_valid_timezone(props.get("timezone")):
            candidate = props
        else:
            return {}

    source = str(candidate.get("timezone_source") or "").strip().lower()
    if source not in _VALID_SOURCES:
        source = "browser" if candidate is ctx else "observed"
    if source == "explicit" and not trusted_properties:
        source = "observed"

    snapshot: Dict[str, Any] = {
        "timezone": candidate["timezone"],
        "timezone_source": source,
        "timezone_observed_at": _parse_observed_at(
            candidate.get("observed_at") or candidate.get("timezone_observed_at"),
            received_at=received,
        ).isoformat().replace("+00:00", "Z"),
    }
    offset = candidate.get("utc_offset_minutes")
    if isinstance(offset, bool):
        offset = None
    try:
        offset = int(offset) if offset is not None else None
    except (TypeError, ValueError):
        offset = None
    if offset is not None and -_MAX_OFFSET_MINUTES <= offset <= _MAX_OFFSET_MINUTES:
        snapshot["utc_offset_minutes"] = offset
    return snapshot


def enrich_properties_with_timezone(
    properties: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]],
    *,
    trusted_properties: bool = False,
) -> Dict[str, Any]:
    enriched = dict(properties or {})
    snapshot = normalize_timezone_context(
        context,
        enriched,
        trusted_properties=trusted_properties,
    )
    enriched.update(snapshot)
    return enriched


def _observation_timestamp(properties: Dict[str, Any]) -> datetime:
    value = properties.get("timezone_observed_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def apply_timezone_to_properties(
    existing: Optional[Dict[str, Any]],
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge a snapshot without allowing old retries to rewind profile state."""
    merged = dict(existing or {})
    if not snapshot:
        return merged

    current_source = merged.get("timezone_source")
    incoming_source = snapshot.get("timezone_source")
    if current_source == "explicit" and incoming_source != "explicit":
        return merged
    if incoming_source != "explicit" and _observation_timestamp(snapshot) < _observation_timestamp(merged):
        return merged

    merged.update(snapshot)
    return merged


def apply_timezone_to_user(user: Any, snapshot: Dict[str, Any]) -> None:
    if not snapshot:
        return
    merged = apply_timezone_to_properties(user.properties, snapshot)
    user.properties = merged
    if merged.get("timezone"):
        user.timezone = merged["timezone"]
