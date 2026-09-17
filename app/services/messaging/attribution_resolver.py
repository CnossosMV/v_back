"""Destination-time advertising attribution capture and resolution."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from sqlalchemy import and_, case, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.messaging import (
    MessagingAnonymousProfile,
    MessagingAttributionTouch,
    MessagingDestination,
    MessagingDestinationDelivery,
    MessagingEvent,
    MessagingTrackingDomain,
    MessagingUser,
)


TOUCH_TTL = timedelta(days=180)
AD_PROVIDER_BY_DESTINATION = {
    "meta_pixel": "meta",
    "google_ads": "google",
    "tiktok": "tiktok",
}
TOUCH_FIELDS = {
    "meta": (("_fbc", "cookies"), ("fbclid", "click_ids")),
    "google": (("gclid", "click_ids"), ("gbraid", "click_ids"), ("wbraid", "click_ids")),
    "tiktok": (("ttclid", "click_ids"),),
}
CAPTURE_SOURCES = {"url", "cookie", "local_storage", "proxy", "backend", "legacy"}


@dataclass
class AttributionResolution:
    attribution: Dict[str, Any]
    summary: Dict[str, Any]


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _utc(value: Optional[datetime]) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _hash_identifier(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _event_at(event: MessagingEvent) -> datetime:
    return _utc(event.client_ts or event.created_at)


def _touch_is_time_eligible(touch: MessagingAttributionTouch, event_at: datetime) -> bool:
    event_at = _utc(event_at)
    return _utc(touch.captured_at) <= event_at <= _utc(touch.expires_at)


def _tracking_domain_id(db: Session, event: MessagingEvent) -> Optional[int]:
    notes = _as_dict(event.processing_notes)
    proxy = _as_dict(notes.get("tracking_proxy"))
    raw = proxy.get("tracking_domain_id")
    try:
        if raw is not None:
            return int(raw)
    except (TypeError, ValueError):
        pass
    hostname = str(proxy.get("hostname") or "").strip().lower()
    if not hostname:
        return None
    row = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.project_id == event.project_id,
        MessagingTrackingDomain.hostname == hostname,
        MessagingTrackingDomain.cookie_keeper_enabled == True,
        MessagingTrackingDomain.proxy_status.in_(["seen", "active"]),
    ).first()
    return row.id if row else None


def _click_meta(attribution: Dict[str, Any], identifier_type: str) -> Dict[str, Any]:
    metadata = _as_dict(attribution.get("click_meta"))
    return _as_dict(metadata.get(identifier_type))


def _capture_source(
    attribution: Dict[str, Any],
    identifier_type: str,
    section: str,
    event: MessagingEvent,
    tracking_domain_id: Optional[int],
) -> str:
    metadata = _click_meta(attribution, identifier_type)
    source = str(metadata.get("source") or "").strip().lower()
    if source in CAPTURE_SOURCES:
        return source
    if tracking_domain_id:
        return "proxy"
    if event.source == "backend":
        return "backend"
    if section == "cookies":
        return "cookie"
    page_url = _as_dict(attribution.get("page")).get("url")
    try:
        if page_url and parse_qs(urlparse(page_url).query).get(identifier_type):
            return "url"
    except (TypeError, ValueError):
        pass
    return "legacy"


def record_attribution_touches(
    db: Session,
    *,
    event: MessagingEvent,
    user: Optional[MessagingUser],
    anonymous_id: Optional[str],
    require_valid_at: Optional[datetime] = None,
) -> List[MessagingAttributionTouch]:
    """Capture real paid-click identifiers without extending their original TTL."""
    attribution = _as_dict(event.attribution)
    if not attribution:
        return []

    click_ids = _as_dict(attribution.get("click_ids"))
    cookies = _as_dict(attribution.get("cookies"))
    observed_at = _event_at(event)
    stored: List[MessagingAttributionTouch] = []
    tracking_domain_id = _tracking_domain_id(db, event)

    for provider, fields in TOUCH_FIELDS.items():
        for identifier_type, section in fields:
            values = cookies if section == "cookies" else click_ids
            raw_value = values.get(identifier_type)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            value = raw_value.strip()
            metadata = _click_meta(attribution, identifier_type)
            captured_at = _parse_timestamp(metadata.get("captured_at")) or observed_at
            max_expires_at = captured_at + TOUCH_TTL
            expires_at = _parse_timestamp(metadata.get("expires_at")) or max_expires_at
            if expires_at <= captured_at or expires_at > max_expires_at:
                expires_at = max_expires_at
            if require_valid_at and expires_at < _utc(require_valid_at):
                continue

            identifier_hash = _hash_identifier(value)
            touch = db.query(MessagingAttributionTouch).filter(
                MessagingAttributionTouch.project_id == event.project_id,
                MessagingAttributionTouch.provider == provider,
                MessagingAttributionTouch.identifier_type == identifier_type,
                MessagingAttributionTouch.identifier_hash == identifier_hash,
            ).first()

            if touch:
                touch.last_seen_at = max(_utc(touch.last_seen_at), observed_at)
                # Legacy rows can reveal an earlier first occurrence during backfill.
                if captured_at < _utc(touch.captured_at):
                    touch.captured_at = captured_at
                    touch.expires_at = expires_at
                    touch.source_event_id = event.id
                if not touch.anonymous_id and anonymous_id:
                    touch.anonymous_id = anonymous_id
                if user and not touch.user_id:
                    touch.user_id = user.id
                elif user and touch.user_id and touch.user_id != user.id:
                    provenance = dict(touch.provenance or {})
                    provenance["identity_conflict"] = True
                    touch.provenance = provenance
                stored.append(touch)
                continue

            touch = MessagingAttributionTouch(
                project_id=event.project_id,
                user_id=user.id if user else None,
                anonymous_id=anonymous_id,
                provider=provider,
                identifier_type=identifier_type,
                identifier_value=value,
                identifier_hash=identifier_hash,
                source_event_id=event.id,
                capture_source=_capture_source(attribution, identifier_type, section, event, tracking_domain_id),
                tracking_domain_id=tracking_domain_id,
                page_url=_as_dict(attribution.get("page")).get("url"),
                utm=_as_dict(attribution.get("utm")) or None,
                provenance={"metadata_version": 1 if metadata else 0},
                captured_at=captured_at,
                expires_at=expires_at,
                last_seen_at=observed_at,
            )
            try:
                with db.begin_nested():
                    db.add(touch)
                    db.flush()
            except IntegrityError:
                touch = db.query(MessagingAttributionTouch).filter(
                    MessagingAttributionTouch.project_id == event.project_id,
                    MessagingAttributionTouch.provider == provider,
                    MessagingAttributionTouch.identifier_type == identifier_type,
                    MessagingAttributionTouch.identifier_hash == identifier_hash,
                ).first()
                if not touch:
                    raise
                if user and touch.user_id and touch.user_id != user.id:
                    provenance = dict(touch.provenance or {})
                    provenance["identity_conflict"] = True
                    touch.provenance = provenance
            stored.append(touch)

    return stored


def transfer_anonymous_touches(
    db: Session,
    *,
    project_id: int,
    anonymous_id: str,
    user_id: int,
) -> int:
    """Attach unclaimed touches after a deterministic anonymous-to-known merge."""
    return db.query(MessagingAttributionTouch).filter(
        MessagingAttributionTouch.project_id == project_id,
        MessagingAttributionTouch.anonymous_id == anonymous_id,
        MessagingAttributionTouch.user_id.is_(None),
    ).update({"user_id": user_id}, synchronize_session=False)


def _first_party_capability(db: Session, project_id: int) -> Dict[str, Any]:
    domains = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.project_id == project_id,
        MessagingTrackingDomain.cookie_keeper_enabled == True,
    ).all()
    configured = bool(domains)
    healthy = next(
        (domain for domain in domains if (domain.proxy_status or "").lower() in {"seen", "active"}),
        None,
    )
    return {
        "configured": configured,
        "healthy": bool(healthy),
        "tracking_domain_id": healthy.id if healthy else None,
    }


def _current_signal(attribution: Dict[str, Any], provider: str) -> Optional[Tuple[str, str]]:
    click_ids = _as_dict(attribution.get("click_ids"))
    cookies = _as_dict(attribution.get("cookies"))
    for identifier_type, section in TOUCH_FIELDS[provider]:
        value = (cookies if section == "cookies" else click_ids).get(identifier_type)
        if isinstance(value, str) and value.strip():
            return identifier_type, value.strip()
    return None


def _eligible_touch_query(
    db: Session,
    *,
    event: MessagingEvent,
    user: Optional[MessagingUser],
    provider: str,
    event_at: datetime,
    warnings: List[str],
):
    identity_filters = []
    if user:
        identity_filters.append(MessagingAttributionTouch.user_id == user.id)
        if event.anonymous_id:
            profile = db.query(MessagingAnonymousProfile).filter(
                MessagingAnonymousProfile.project_id == event.project_id,
                MessagingAnonymousProfile.anonymous_id == event.anonymous_id,
            ).first()
            if profile and profile.merged_to_user_id == user.id:
                identity_filters.append(
                    and_(
                        MessagingAttributionTouch.user_id.is_(None),
                        MessagingAttributionTouch.anonymous_id == event.anonymous_id,
                    )
                )
            elif profile and profile.merged_to_user_id not in (None, user.id):
                warnings.append("identity_conflict")
                return None
    elif event.anonymous_id:
        profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == event.project_id,
            MessagingAnonymousProfile.anonymous_id == event.anonymous_id,
        ).first()
        if not profile or profile.merged_to_user_id is None:
            identity_filters.append(MessagingAttributionTouch.anonymous_id == event.anonymous_id)
        else:
            warnings.append("identity_conflict")

    if not identity_filters:
        return None

    return db.query(MessagingAttributionTouch).filter(
        MessagingAttributionTouch.project_id == event.project_id,
        MessagingAttributionTouch.provider == provider,
        MessagingAttributionTouch.captured_at <= event_at,
        MessagingAttributionTouch.expires_at >= event_at,
        or_(*identity_filters),
    )


def _selected_touch_from_retry(
    db: Session,
    *,
    delivery: MessagingDestinationDelivery,
    provider: str,
) -> Optional[MessagingAttributionTouch]:
    previous = _as_dict(delivery.attribution_resolution)
    touch_id = previous.get("selected_touch_id")
    if not touch_id or previous.get("provider") != provider:
        return None
    return db.query(MessagingAttributionTouch).filter(
        MessagingAttributionTouch.id == touch_id,
        MessagingAttributionTouch.project_id == delivery.project_id,
    ).first()


def resolve_destination_attribution(
    db: Session,
    *,
    event: MessagingEvent,
    destination: MessagingDestination,
    delivery: MessagingDestinationDelivery,
    user: Optional[MessagingUser],
    config: Dict[str, Any],
    consent_granted: bool,
) -> AttributionResolution:
    """Resolve one provider's best eligible touch immediately before payload build."""
    current = dict(event.attribution or {})
    destination_type = destination.destination_type.value
    provider = AD_PROVIDER_BY_DESTINATION.get(destination_type)
    mode = str(config.get("attribution_enrichment") or "current_only").lower()
    if mode not in {"current_only", "historical"}:
        mode = "current_only"
    diagnostic_only = bool(config.get("attribution_diagnostic_only"))
    warnings: List[str] = []
    first_party = (
        _first_party_capability(db, event.project_id)
        if provider
        else {"configured": False, "healthy": False, "tracking_domain_id": None}
    )

    summary: Dict[str, Any] = {
        "version": 1,
        "mode": mode,
        "provider": provider,
        "canonical_user_id": user.id if user else None,
        "identity_basis": "canonical_contact" if user else ("anonymous_id" if event.anonymous_id else "current_event"),
        "selected_touch_id": None,
        "touch_age_seconds": None,
        "fields_added": [],
        "field_sources": {},
        "first_party": first_party,
        "consent_granted": consent_granted,
        "diagnostic_only": diagnostic_only,
        "payload_applied": False,
        "warnings": warnings,
    }

    if not provider:
        return AttributionResolution(current, summary)
    if not first_party["healthy"]:
        warnings.append("first_party_unavailable")
    if not consent_granted:
        warnings.append("consent_not_granted")
        return AttributionResolution(current, summary)

    current_signal = _current_signal(current, provider)
    if current_signal:
        summary["field_sources"][current_signal[0]] = "current_event"
        summary["payload_applied"] = True
        return AttributionResolution(current, summary)
    if mode != "historical":
        warnings.append("historical_enrichment_disabled")
        return AttributionResolution(current, summary)

    event_at = _event_at(event)
    previous_resolution = _as_dict(delivery.attribution_resolution)
    has_previous_resolution = (
        previous_resolution.get("version") == 1
        and previous_resolution.get("provider") == provider
    )
    touch = _selected_touch_from_retry(db, delivery=delivery, provider=provider)
    if has_previous_resolution and touch is None:
        if previous_resolution.get("selected_touch_id"):
            warnings.append("retry_touch_unavailable")
        else:
            warnings.append("retry_resolution_reused")
        warnings.append("no_eligible_historical_touch")
        return AttributionResolution(current, summary)
    if touch is None:
        query = _eligible_touch_query(
            db,
            event=event,
            user=user,
            provider=provider,
            event_at=event_at,
            warnings=warnings,
        )
        if query is not None:
            touch = query.order_by(
                MessagingAttributionTouch.captured_at.desc(),
                case((MessagingAttributionTouch.identifier_type == "_fbc", 0), else_=1).asc(),
                MessagingAttributionTouch.id.desc(),
            ).first()

    if touch and _as_dict(touch.provenance).get("identity_conflict"):
        warnings.append("identity_conflict")
        touch = None
    if touch and not _touch_is_time_eligible(touch, event_at):
        warnings.append("selected_touch_expired")
        touch = None
    if not touch:
        warnings.append("no_eligible_historical_touch")
        return AttributionResolution(current, summary)

    enriched = dict(current)
    resolved_type = touch.identifier_type
    resolved_value = touch.identifier_value
    section = "cookies" if resolved_type == "_fbc" else "click_ids"
    if provider == "meta" and resolved_type == "fbclid":
        resolved_type = "_fbc"
        resolved_value = (
            f"fb.1.{int(_utc(touch.captured_at).timestamp() * 1000)}."
            f"{touch.identifier_value}"
        )
        section = "cookies"
    values = dict(_as_dict(enriched.get(section)))
    values.setdefault(resolved_type, resolved_value)
    enriched[section] = values

    summary["selected_touch_id"] = touch.id
    summary["touch_age_seconds"] = max(0, int((event_at - _utc(touch.captured_at)).total_seconds()))
    summary["fields_added"] = [resolved_type]
    summary["field_sources"][resolved_type] = touch.capture_source
    summary["payload_applied"] = not diagnostic_only
    if diagnostic_only:
        warnings.append("diagnostic_only")
        return AttributionResolution(current, summary)
    return AttributionResolution(enriched, summary)
