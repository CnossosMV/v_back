"""
Server-side destination dispatcher for ads/analytics providers.

The dispatcher has two responsibilities:
1. Create one delivery log row per active mapped destination for a Versya event.
2. Process queued rows and send provider-specific payloads with retry logging.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.messaging import (
    DestinationType,
    MessagingDestination,
    MessagingDestinationDelivery,
    MessagingEvent,
    MessagingEventMapping,
    MessagingEventSchema,
    MessagingUser,
)
from app.services.messaging.consent_manager import consent_manager
from app.services.messaging.destination_config import destination_config
from app.services.messaging.attribution_resolver import resolve_destination_attribution

logger = logging.getLogger(__name__)


AD_RELEVANT_DEFAULTS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "trial_started": {
        "meta_pixel": {"event_name": "StartTrial", "is_active": True},
        "tiktok": {"event_name": "StartTrial", "is_active": True},
        "ga4": {"event_name": "trial_started", "is_active": True},
        "google_ads": {"event_name": "trial_started", "is_active": True},
    },
    "billing.checkout_started": {
        "meta_pixel": {"event_name": "InitiateCheckout", "is_active": True},
        "tiktok": {"event_name": "InitiateCheckout", "is_active": True},
        "ga4": {"event_name": "begin_checkout", "is_active": True},
        "google_ads": {"event_name": "begin_checkout", "is_active": False},
    },
    "purchase": {
        "meta_pixel": {"event_name": "Purchase", "is_active": True},
        "tiktok": {"event_name": "Purchase", "is_active": True},
        "ga4": {"event_name": "purchase", "is_active": True},
        "google_ads": {"event_name": "purchase", "is_active": True},
    },
    "funnel.score": {
        "meta_pixel": {"event_name": "FunnelScore", "is_active": True},
    },
}


@dataclass
class ProviderResult:
    status: str
    request_summary: Optional[Dict[str, Any]] = None
    response_summary: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    retryable: bool = False


def _sha256(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _digits(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or None


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


def _event_time(event: MessagingEvent) -> int:
    dt = event.client_ts or event.created_at or datetime.utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _google_ads_datetime(event: MessagingEvent) -> str:
    dt = event.client_ts or event.created_at or datetime.utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    formatted = dt.strftime("%Y-%m-%d %H:%M:%S%z")
    return f"{formatted[:-2]}:{formatted[-2:]}" if len(formatted) >= 5 else formatted


def _google_ads_api_version(configured_version: Optional[str]) -> str:
    version = (configured_version or "").strip() or "v24"
    # v19 was sunset in February 2026. Google returns a generic 404 HTML page
    # for retired REST endpoints, so keep older saved destination configs moving.
    if version == "v19":
        return "v24"
    return version


def _ga_client_id(ga_cookie: Optional[str]) -> Optional[str]:
    if not ga_cookie:
        return None
    parts = ga_cookie.split(".")
    if len(parts) >= 4 and parts[0].startswith("GA"):
        return ".".join(parts[-2:])
    return ga_cookie


def _meta_fbc(existing_fbc: Optional[str], fbclid: Optional[str], event: MessagingEvent) -> Optional[str]:
    if existing_fbc:
        return existing_fbc
    if not fbclid:
        return None
    return f"fb.1.{_event_time(event) * 1000}.{fbclid}"


def _value_and_currency(properties: Dict[str, Any], config: Dict[str, Any]) -> Tuple[Optional[float], str]:
    raw_value = _first_non_empty(
        properties.get("value"),
        properties.get("amount"),
        properties.get("price"),
        properties.get("revenue"),
    )
    value: Optional[float] = None
    if raw_value is not None:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = None
    currency = _first_non_empty(properties.get("currency"), config.get("default_currency")) or "BRL"
    return value, currency


def _user_identifiers(user: Optional[MessagingUser], properties: Dict[str, Any]) -> Dict[str, Optional[str]]:
    email = _first_non_empty(properties.get("email"), user.email if user else None)
    phone = _first_non_empty(properties.get("phone"), user.phone_e164 if user else None, user.phone if user else None)
    external_id = _first_non_empty(properties.get("user_id"), properties.get("external_id"), user.external_id if user else None)
    return {
        "email_hash": _sha256(email),
        "phone_hash": _sha256(_digits(phone)),
        "external_id_hash": _sha256(external_id),
        "external_id": external_id,
    }


class DestinationDispatcher:
    """Queues and sends server-side destination deliveries."""

    def ensure_default_mappings(self, db: Session, destination: MessagingDestination) -> None:
        defaults_for_type = {
            event_name: provider_map.get(destination.destination_type.value)
            for event_name, provider_map in AD_RELEVANT_DEFAULTS.items()
        }
        for event_name, default in defaults_for_type.items():
            if not default:
                continue
            schema = db.query(MessagingEventSchema).filter(
                MessagingEventSchema.project_id == destination.project_id,
                MessagingEventSchema.event_name == event_name,
            ).first()
            if not schema:
                schema = MessagingEventSchema(
                    project_id=destination.project_id,
                    event_name=event_name,
                    display_name=event_name.replace(".", " ").replace("_", " ").title(),
                    category="conversion",
                    is_standard=event_name in ("trial_started", "purchase"),
                    is_active=True,
                )
                db.add(schema)
                db.flush()

            existing = db.query(MessagingEventMapping).filter(
                MessagingEventMapping.project_id == destination.project_id,
                MessagingEventMapping.event_schema_id == schema.id,
                MessagingEventMapping.destination_id == destination.id,
            ).first()
            if existing:
                continue

            provider_settings: Dict[str, Any] = {}
            if destination.destination_type.value == DestinationType.google_ads.value:
                provider_settings["conversion_action_id"] = None

            db.add(MessagingEventMapping(
                project_id=destination.project_id,
                event_schema_id=schema.id,
                destination_id=destination.id,
                destination_event_name=default["event_name"],
                property_mappings=[],
                provider_settings=provider_settings,
                is_active=bool(default["is_active"]),
            ))

    def queue_for_event(self, db: Session, event: MessagingEvent) -> List[MessagingDestinationDelivery]:
        active_destinations = db.query(MessagingDestination).filter(
            MessagingDestination.project_id == event.project_id,
            MessagingDestination.is_active == True,
        ).all()

        schema = db.query(MessagingEventSchema).filter(
            MessagingEventSchema.project_id == event.project_id,
            MessagingEventSchema.event_name == event.event_name,
            MessagingEventSchema.is_active == True,
        ).first()
        if not schema:
            return self._queue_unmapped_skips(db, event, active_destinations, "Event schema is not mapped to destinations")

        mappings = db.query(MessagingEventMapping).join(MessagingDestination).filter(
            MessagingEventMapping.project_id == event.project_id,
            MessagingEventMapping.event_schema_id == schema.id,
            MessagingDestination.is_active == True,
        ).all()

        mapped_destination_ids = {mapping.destination_id for mapping in mappings}
        unmapped_destinations = [
            destination for destination in active_destinations
            if destination.id not in mapped_destination_ids
        ]

        deliveries: List[MessagingDestinationDelivery] = []
        user = db.query(MessagingUser).filter(MessagingUser.id == event.user_id).first() if event.user_id else None

        for mapping in mappings:
            destination = mapping.destination
            status = "queued" if mapping.is_active else "skipped"
            error_message = None if mapping.is_active else "Event mapping is inactive"
            request_summary = {"reason": error_message} if error_message else None
            config = destination_config.decrypt_config(destination.config_encrypted) or {}

            if status == "queued" and destination.consent_required:
                if not user:
                    status = "skipped"
                    error_message = "Missing user consent context"
                elif not consent_manager.check_consent(user, destination.consent_required):
                    status = "skipped"
                    error_message = "Required consent not granted"
                if error_message:
                    properties = event.properties or {}
                    context = {
                        "config": config,
                        "properties": properties,
                        "attribution": event.attribution or {},
                        "user": user,
                        "event_id": event.external_event_id or properties.get("event_id") or str(event.id),
                        "mapping_settings": mapping.provider_settings or {},
                    }
                    request_summary = {
                        "reason": error_message,
                        "blocked_by": "consent",
                        "required_consent": destination.consent_required,
                        "user_id": event.user_id,
                        "payload_preview": self._dry_run_payload_preview(destination, event, mapping, context),
                    }

            dedupe_key = self._delivery_dedupe_key(event, destination, mapping)
            existing = db.query(MessagingDestinationDelivery).filter(
                MessagingDestinationDelivery.dedupe_key == dedupe_key
            ).first()
            if existing:
                deliveries.append(existing)
                continue

            delivery = MessagingDestinationDelivery(
                project_id=event.project_id,
                destination_id=destination.id,
                event_id=event.id,
                event_mapping_id=mapping.id,
                destination_type=destination.destination_type.value,
                provider_event_name=mapping.destination_event_name,
                status=status,
                dedupe_key=dedupe_key,
                request_summary=request_summary,
                error_message=error_message,
                skipped_at=datetime.utcnow() if status == "skipped" else None,
            )
            if status == "skipped":
                consent_granted = not destination.consent_required or bool(
                    user and consent_manager.check_consent(user, destination.consent_required)
                )
                resolution = resolve_destination_attribution(
                    db,
                    event=event,
                    destination=destination,
                    delivery=delivery,
                    user=user,
                    config=config,
                    consent_granted=consent_granted,
                )
                resolution.summary["payload_applied"] = False
                resolution.summary["warnings"].append("delivery_skipped_before_dispatch")
                delivery.attribution_resolution = resolution.summary
            try:
                with db.begin_nested():
                    db.add(delivery)
                    db.flush()
            except IntegrityError:
                existing = db.query(MessagingDestinationDelivery).filter(
                    MessagingDestinationDelivery.dedupe_key == dedupe_key
                ).first()
                if existing:
                    deliveries.append(existing)
                    continue
                raise
            deliveries.append(delivery)

        deliveries.extend(self._queue_unmapped_skips(
            db,
            event,
            unmapped_destinations,
            "Event is not mapped for this destination",
        ))
        db.commit()
        return deliveries

    @staticmethod
    def _effective_external_event_id(event: MessagingEvent) -> Optional[str]:
        properties = event.properties or {}
        external_event_id = event.external_event_id or properties.get("event_id")
        if external_event_id is None:
            return None
        external_event_id = str(external_event_id).strip()
        return external_event_id or None

    def _delivery_dedupe_key(
        self,
        event: MessagingEvent,
        destination: MessagingDestination,
        mapping: MessagingEventMapping,
    ) -> str:
        external_event_id = self._effective_external_event_id(event)
        if external_event_id:
            return (
                f"ads:{event.project_id}:{destination.id}:{mapping.id}:"
                f"{event.event_name}:{mapping.destination_event_name}:{external_event_id}"
            )
        return f"{event.project_id}:{destination.id}:{event.id}:{mapping.id}"

    def _queue_unmapped_skips(
        self,
        db: Session,
        event: MessagingEvent,
        destinations: List[MessagingDestination],
        reason: str,
    ) -> List[MessagingDestinationDelivery]:
        deliveries: List[MessagingDestinationDelivery] = []
        user = db.query(MessagingUser).filter(
            MessagingUser.id == event.user_id
        ).first() if event.user_id else None
        for destination in destinations:
            dedupe_key = f"{event.project_id}:{destination.id}:{event.id}:unmapped"
            existing = db.query(MessagingDestinationDelivery).filter(
                MessagingDestinationDelivery.dedupe_key == dedupe_key
            ).first()
            if existing:
                deliveries.append(existing)
                continue
            delivery = MessagingDestinationDelivery(
                project_id=event.project_id,
                destination_id=destination.id,
                event_id=event.id,
                event_mapping_id=None,
                destination_type=destination.destination_type.value,
                provider_event_name=event.event_name,
                status="skipped",
                dedupe_key=dedupe_key,
                request_summary={"reason": reason},
                error_message=reason,
                skipped_at=datetime.utcnow(),
            )
            config = destination_config.decrypt_config(destination.config_encrypted) or {}
            consent_granted = not destination.consent_required or bool(
                user and consent_manager.check_consent(user, destination.consent_required)
            )
            resolution = resolve_destination_attribution(
                db,
                event=event,
                destination=destination,
                delivery=delivery,
                user=user,
                config=config,
                consent_granted=consent_granted,
            )
            resolution.summary["payload_applied"] = False
            resolution.summary["warnings"].append("delivery_skipped_before_dispatch")
            delivery.attribution_resolution = resolution.summary
            db.add(delivery)
            db.flush()
            deliveries.append(delivery)
        if deliveries:
            db.commit()
        return deliveries

    def process_due_deliveries(self, db: Session, batch_size: int = 50) -> int:
        now = datetime.utcnow()
        deliveries = db.query(MessagingDestinationDelivery).filter(
            MessagingDestinationDelivery.status.in_(["queued", "retrying"]),
            or_(
                MessagingDestinationDelivery.next_retry_at.is_(None),
                MessagingDestinationDelivery.next_retry_at <= now,
            ),
        ).order_by(MessagingDestinationDelivery.created_at.asc()).limit(batch_size).all()

        processed = 0
        for delivery in deliveries:
            self.process_delivery(db, delivery)
            processed += 1
        return processed

    def process_delivery(self, db: Session, delivery: MessagingDestinationDelivery) -> MessagingDestinationDelivery:
        delivery.attempt_count += 1
        db.flush()

        try:
            result = self._send(db, delivery)
        except Exception as exc:
            logger.exception("Destination delivery %s failed unexpectedly", delivery.id)
            result = ProviderResult(
                status="failed",
                error_message=f"Unexpected dispatcher error: {exc}",
                retryable=True,
            )

        delivery.request_summary = result.request_summary
        delivery.response_summary = result.response_summary
        delivery.error_message = result.error_message

        if result.status == "sent":
            delivery.status = "sent"
            delivery.sent_at = datetime.utcnow()
            delivery.next_retry_at = None
        elif result.status == "skipped":
            delivery.status = "skipped"
            delivery.skipped_at = datetime.utcnow()
            delivery.next_retry_at = None
        elif result.retryable and delivery.attempt_count < delivery.max_attempts:
            delivery.status = "retrying"
            delivery.next_retry_at = datetime.utcnow() + timedelta(minutes=2 ** delivery.attempt_count)
        else:
            delivery.status = "failed"
            delivery.next_retry_at = None

        db.commit()
        db.refresh(delivery)
        return delivery

    def _send(self, db: Session, delivery: MessagingDestinationDelivery) -> ProviderResult:
        destination = delivery.destination
        event = delivery.event
        mapping = delivery.event_mapping
        if not destination or not event or not mapping:
            return ProviderResult(status="skipped", error_message="Missing destination, event, or mapping")

        config = destination_config.decrypt_config(destination.config_encrypted) or {}
        properties = event.properties or {}
        user = db.query(MessagingUser).filter(MessagingUser.id == event.user_id).first() if event.user_id else None
        required_consent = list(destination.consent_required or [])
        consent_granted = not required_consent or bool(
            user and consent_manager.check_consent(user, required_consent)
        )
        resolution = resolve_destination_attribution(
            db,
            event=event,
            destination=destination,
            delivery=delivery,
            user=user,
            config=config,
            consent_granted=consent_granted,
        )
        delivery.attribution_resolution = resolution.summary
        db.flush()
        if not consent_granted:
            return ProviderResult(
                status="skipped",
                request_summary={"reason": "Required consent not granted"},
                error_message="Required consent not granted",
            )
        context = {
            "config": config,
            "properties": properties,
            "attribution": resolution.attribution,
            "user": user,
            "event_id": event.external_event_id or properties.get("event_id") or str(event.id),
            "mapping_settings": mapping.provider_settings or {},
        }

        if config.get("dry_run"):
            return ProviderResult(
                status="skipped",
                request_summary={
                    "dry_run": True,
                    "provider": destination.destination_type.value,
                    "event_name": mapping.destination_event_name,
                    "event_id": context["event_id"],
                    "campaign_origin": event.campaign_origin,
                    "has_attribution": bool(event.attribution),
                    "payload_preview": self._dry_run_payload_preview(destination, event, mapping, context),
                },
                error_message="Dry run enabled; provider call not sent",
            )

        if destination.destination_type == DestinationType.meta_pixel:
            return self._send_meta(event, mapping, context)
        if destination.destination_type == DestinationType.ga4:
            return self._send_ga4(event, mapping, context)
        if destination.destination_type == DestinationType.google_ads:
            return self._send_google_ads(event, mapping, context)
        if destination.destination_type == DestinationType.tiktok:
            return self._send_tiktok(event, mapping, context)
        if destination.destination_type == DestinationType.custom:
            return self._send_custom(event, mapping, context)
        return ProviderResult(status="skipped", error_message=f"Unsupported destination type: {destination.destination_type}")

    def _dry_run_payload_preview(
        self,
        destination: MessagingDestination,
        event: MessagingEvent,
        mapping: MessagingEventMapping,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = context["config"]
        props = context["properties"]
        attr = context["attribution"]
        cookies = attr.get("cookies") or {}
        click_ids = attr.get("click_ids") or {}
        page = attr.get("page") or {}
        ids = _user_identifiers(context["user"], props)
        value, currency = _value_and_currency(props, config)
        order_id = _first_non_empty(props.get("transaction_id"), props.get("order_id"), props.get("order_number"), props.get("payment_id"))
        base = {
            "source_event_name": event.event_name,
            "provider_event_name": mapping.destination_event_name,
            "event_id": context["event_id"],
            "event_time": _event_time(event),
            "campaign_origin": event.campaign_origin,
            "tracking_proxy": (event.processing_notes or {}).get("tracking_proxy"),
            "value": value,
            "currency": currency,
            "order_id": order_id,
            "page_url": page.get("url"),
            "referrer": page.get("referrer"),
            "has_ip": bool(attr.get("ip_address")),
            "has_user_agent": bool(attr.get("user_agent")),
            "identity": {
                "has_email_hash": bool(ids.get("email_hash")),
                "has_phone_hash": bool(ids.get("phone_hash")),
                "has_external_id_hash": bool(ids.get("external_id_hash")),
            },
            "click_ids": {k: v for k, v in click_ids.items() if k in ("gclid", "gbraid", "wbraid", "fbclid", "ttclid")},
            "cookies": {
                "_fbp": bool(cookies.get("_fbp")),
                "_fbc": bool(cookies.get("_fbc")),
                "_ga": bool(cookies.get("_ga")),
                "_ttp": bool(cookies.get("_ttp") or cookies.get("ttp")),
            },
        }

        if destination.destination_type == DestinationType.meta_pixel:
            fbc = _meta_fbc(cookies.get("_fbc"), click_ids.get("fbclid"), event)
            return {
                **base,
                "provider": "meta",
                "action_source": "website",
                "pixel_id": config.get("pixel_id"),
                "would_include": {
                    "fbp": bool(cookies.get("_fbp")),
                    "fbc": bool(fbc),
                    "fbc_derived_from_fbclid": bool(fbc and not cookies.get("_fbc") and click_ids.get("fbclid")),
                    "hashed_email": bool(config.get("advanced_matching") and ids.get("email_hash")),
                    "hashed_phone": bool(config.get("advanced_matching") and ids.get("phone_hash")),
                    "hashed_external_id": bool(config.get("advanced_matching") and ids.get("external_id_hash")),
                },
            }
        if destination.destination_type == DestinationType.ga4:
            ga_cookie = cookies.get("_ga")
            client_id = _first_non_empty(props.get("client_id"), props.get("ga_client_id"), _ga_client_id(ga_cookie), event.anonymous_id)
            return {
                **base,
                "provider": "ga4",
                "measurement_id": config.get("measurement_id"),
                "client_id_present": bool(client_id),
                "user_id_present": bool(_first_non_empty(props.get("user_id"), context["user"].external_id if context["user"] else None)),
                "session_id_present": bool(_first_non_empty(props.get("ga_session_id"), props.get("session_id"), event.session_id)),
            }
        if destination.destination_type == DestinationType.google_ads:
            action_id = _first_non_empty(
                context["mapping_settings"].get("conversion_action_id"),
                config.get("default_conversion_action_id"),
                config.get("conversion_action_id"),
            )
            click_key = "gclid" if click_ids.get("gclid") else "gbraid" if click_ids.get("gbraid") else "wbraid" if click_ids.get("wbraid") else None
            customer_id = _first_non_empty(config.get("customer_id"), config.get("conversion_id"))
            return {
                **base,
                "provider": "google_ads",
                "customer_id": customer_id,
                "conversion_action_id": action_id,
                "conversion_action": f"customers/{customer_id}/conversionActions/{action_id}" if customer_id and action_id else None,
                "click_id_type": click_key,
                "has_google_click_id": bool(click_key),
            }
        if destination.destination_type == DestinationType.tiktok:
            return {
                **base,
                "provider": "tiktok",
                "pixel_id": config.get("pixel_id"),
                "would_include": {
                    "ttclid": bool(click_ids.get("ttclid")),
                    "ttp": bool(cookies.get("_ttp") or cookies.get("ttp")),
                    "hashed_email": bool(config.get("advanced_matching") and ids.get("email_hash")),
                    "hashed_phone": bool(config.get("advanced_matching") and ids.get("phone_hash")),
                    "hashed_external_id": bool(config.get("advanced_matching") and ids.get("external_id_hash")),
                },
            }
        return {**base, "provider": destination.destination_type.value}

    def _send_meta(self, event: MessagingEvent, mapping: MessagingEventMapping, context: Dict[str, Any]) -> ProviderResult:
        config = context["config"]
        pixel_id = config.get("pixel_id")
        token = config.get("access_token")
        if not pixel_id or not token:
            return ProviderResult(status="skipped", error_message="Meta pixel_id or access_token missing")

        props = context["properties"]
        attr = context["attribution"]
        cookies = attr.get("cookies") or {}
        click_ids = attr.get("click_ids") or {}
        page = attr.get("page") or {}
        ids = _user_identifiers(context["user"], props)
        value, currency = _value_and_currency(props, config)
        fbc = _meta_fbc(cookies.get("_fbc"), click_ids.get("fbclid"), event)

        user_data = {
            "client_ip_address": attr.get("ip_address"),
            "client_user_agent": attr.get("user_agent"),
            "fbp": cookies.get("_fbp"),
            "fbc": fbc,
        }
        if config.get("advanced_matching"):
            user_data.update({
                "em": [ids["email_hash"]] if ids.get("email_hash") else None,
                "ph": [ids["phone_hash"]] if ids.get("phone_hash") else None,
                "external_id": [ids["external_id_hash"]] if ids.get("external_id_hash") else None,
            })
        user_data = {k: v for k, v in user_data.items() if v}
        if not user_data:
            return ProviderResult(status="skipped", error_message="Meta user_data missing browser IDs, IP/UA, and hashed identifiers")

        custom_data = {
            # FunnelScore is also used by Meta Custom Conversions as a
            # parameterized event. Keep the source value when present, but
            # default it for the server-side FunnelScore mapping so backend
            # events remain consistent with the browser/GTM payload.
            "meta_event_name": _first_non_empty(
                props.get("meta_event_name"),
                mapping.destination_event_name
                if mapping.destination_event_name == "FunnelScore"
                else None,
            ),
            "value": value,
            "currency": currency,
            "order_id": _first_non_empty(props.get("transaction_id"), props.get("order_id"), props.get("order_number"), props.get("payment_id")),
            "content_name": props.get("plan_id"),
        }
        custom_data = {k: v for k, v in custom_data.items() if v is not None}

        event_payload = {
            "event_name": mapping.destination_event_name,
            "event_time": _event_time(event),
            "event_id": context["event_id"],
            "event_source_url": page.get("url"),
            "action_source": "website",
            "user_data": user_data,
            "custom_data": custom_data,
        }
        api_version = config.get("api_version") or "v20.0"
        url = f"https://graph.facebook.com/{api_version}/{pixel_id}/events"
        payload = {"data": [event_payload], "access_token": token}
        if config.get("test_event_code"):
            payload["test_event_code"] = config.get("test_event_code")
        return self._post_json(url, payload, request_summary={
            "provider": "meta",
            "pixel_id": pixel_id,
            "event_name": mapping.destination_event_name,
            "event_id": context["event_id"],
            "has_fbp": bool(cookies.get("_fbp")),
            "has_fbc": bool(fbc),
            "fbc_derived_from_fbclid": bool(fbc and not cookies.get("_fbc") and click_ids.get("fbclid")),
        })

    def _send_ga4(self, event: MessagingEvent, mapping: MessagingEventMapping, context: Dict[str, Any]) -> ProviderResult:
        config = context["config"]
        measurement_id = config.get("measurement_id")
        api_secret = config.get("api_secret")
        if not measurement_id or not api_secret:
            return ProviderResult(status="skipped", error_message="GA4 measurement_id or api_secret missing")

        props = context["properties"]
        attr = context["attribution"]
        cookies = attr.get("cookies") or {}
        ga_cookie = cookies.get("_ga")
        client_id = _first_non_empty(props.get("client_id"), props.get("ga_client_id"), _ga_client_id(ga_cookie), event.anonymous_id)
        user_id = _first_non_empty(props.get("user_id"), context["user"].external_id if context["user"] else None)
        if not client_id and not user_id:
            return ProviderResult(status="skipped", error_message="GA4 requires client_id or user_id")

        value, currency = _value_and_currency(props, config)
        session_id = _first_non_empty(props.get("ga_session_id"), props.get("session_id"), event.session_id)
        warnings = []
        if not ga_cookie:
            warnings.append("No _ga cookie; using anonymous/user fallback")
        if not props.get("ga_session_id"):
            warnings.append("No GA session ID; using Versya session fallback")
        params = {
            **{k: v for k, v in props.items() if isinstance(v, (str, int, float, bool))},
            "session_id": session_id,
            "engagement_time_msec": 1,
            "currency": currency,
            "value": value,
            "transaction_id": _first_non_empty(props.get("transaction_id"), props.get("order_id"), props.get("order_number"), props.get("payment_id")),
            "campaign_origin": event.campaign_origin,
        }
        params = {k: v for k, v in params.items() if v is not None}
        payload = {
            "client_id": client_id,
            "user_id": user_id,
            "timestamp_micros": _event_time(event) * 1000000,
            "events": [{"name": mapping.destination_event_name, "params": params}],
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        endpoint = "debug/mp/collect" if config.get("debug_mode") else "mp/collect"
        url = f"https://www.google-analytics.com/{endpoint}?measurement_id={measurement_id}&api_secret={api_secret}"
        return self._post_json(url, payload, request_summary={
            "provider": "ga4",
            "measurement_id": measurement_id,
            "event_name": mapping.destination_event_name,
            "event_id": context["event_id"],
            "warnings": warnings,
        })

    def _send_google_ads(self, event: MessagingEvent, mapping: MessagingEventMapping, context: Dict[str, Any]) -> ProviderResult:
        config = context["config"]
        customer_id = _first_non_empty(config.get("customer_id"), config.get("conversion_id"))
        developer_token = config.get("developer_token")
        access_token = self._google_access_token(config)
        action_id = _first_non_empty(
            context["mapping_settings"].get("conversion_action_id"),
            config.get("default_conversion_action_id"),
            config.get("conversion_action_id"),
        )
        if not customer_id or not developer_token or not access_token or not action_id:
            return ProviderResult(status="skipped", error_message="Google Ads customer_id, developer_token, access token, or conversion_action_id missing")

        props = context["properties"]
        attr = context["attribution"]
        click_ids = attr.get("click_ids") or {}
        click_key = "gclid" if click_ids.get("gclid") else "gbraid" if click_ids.get("gbraid") else "wbraid" if click_ids.get("wbraid") else None
        if not click_key:
            return ProviderResult(status="skipped", error_message="Google Ads click ID missing (gclid, gbraid, or wbraid)")

        value, currency = _value_and_currency(props, config)
        conversion = {
            "conversionAction": f"customers/{customer_id}/conversionActions/{action_id}",
            click_key: click_ids.get(click_key),
            "conversionDateTime": _google_ads_datetime(event),
            "conversionValue": value or 0,
            "currencyCode": currency,
            "orderId": _first_non_empty(props.get("transaction_id"), props.get("order_id"), props.get("order_number"), props.get("payment_id")),
        }
        conversion = {k: v for k, v in conversion.items() if v is not None}
        enhanced_identifiers = []
        if config.get("enhanced_conversions"):
            ids = _user_identifiers(context["user"], props)
            if ids.get("email_hash"):
                enhanced_identifiers.append({"hashedEmail": ids["email_hash"]})
            if ids.get("phone_hash"):
                enhanced_identifiers.append({"hashedPhoneNumber": ids["phone_hash"]})
            if enhanced_identifiers:
                conversion["userIdentifiers"] = enhanced_identifiers

        api_version = _google_ads_api_version(config.get("api_version"))
        url = f"https://googleads.googleapis.com/{api_version}/customers/{customer_id}:uploadClickConversions"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "developer-token": developer_token,
        }
        if config.get("login_customer_id"):
            headers["login-customer-id"] = str(config["login_customer_id"])

        return self._post_json(url, {"conversions": [conversion], "partialFailure": True}, headers=headers, request_summary={
            "provider": "google_ads",
            "customer_id": customer_id,
            "conversion_action_id": action_id,
            "event_name": mapping.destination_event_name,
            "click_id_type": click_key,
            "event_id": context["event_id"],
            "enhanced_conversions": bool(config.get("enhanced_conversions")),
            "enhanced_identifier_count": len(enhanced_identifiers),
        })

    def _google_access_token(self, config: Dict[str, Any]) -> Optional[str]:
        if config.get("access_token"):
            return config.get("access_token")
        if not (config.get("refresh_token") and config.get("client_id") and config.get("client_secret")):
            return None
        data = {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": config["refresh_token"],
            "grant_type": "refresh_token",
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post("https://oauth2.googleapis.com/token", data=data)
            if response.status_code >= 400:
                return None
            return response.json().get("access_token")
        except Exception:
            return None

    def _send_tiktok(self, event: MessagingEvent, mapping: MessagingEventMapping, context: Dict[str, Any]) -> ProviderResult:
        config = context["config"]
        pixel_id = config.get("pixel_id")
        token = config.get("access_token")
        if not pixel_id or not token:
            return ProviderResult(status="skipped", error_message="TikTok pixel_id or access_token missing")

        props = context["properties"]
        attr = context["attribution"]
        cookies = attr.get("cookies") or {}
        click_ids = attr.get("click_ids") or {}
        page = attr.get("page") or {}
        ids = _user_identifiers(context["user"], props)
        has_advanced_match = bool(config.get("advanced_matching")) and bool(
            ids.get("email_hash") or ids.get("phone_hash") or ids.get("external_id_hash")
        )
        if not (click_ids.get("ttclid") or cookies.get("_ttp") or cookies.get("ttp") or has_advanced_match):
            return ProviderResult(status="skipped", error_message="TikTok matching data missing")

        value, currency = _value_and_currency(props, config)
        user = {
            "ttclid": click_ids.get("ttclid"),
            "ttp": _first_non_empty(cookies.get("_ttp"), cookies.get("ttp")),
            "ip": attr.get("ip_address"),
            "user_agent": attr.get("user_agent"),
        }
        if config.get("advanced_matching"):
            user.update({
                "email": ids.get("email_hash"),
                "phone": ids.get("phone_hash"),
                "external_id": ids.get("external_id_hash"),
            })
        user = {k: v for k, v in user.items() if v}
        event_payload = {
            "event": mapping.destination_event_name,
            "event_time": _event_time(event),
            "event_id": context["event_id"],
            "user": user,
            "page": {"url": page.get("url"), "referrer": page.get("referrer")},
            "properties": {
                "value": value,
                "currency": currency,
                "order_id": _first_non_empty(props.get("transaction_id"), props.get("order_id"), props.get("order_number"), props.get("payment_id")),
            },
        }
        event_payload["page"] = {k: v for k, v in event_payload["page"].items() if v}
        event_payload["properties"] = {k: v for k, v in event_payload["properties"].items() if v is not None}

        api_version = config.get("api_version") or "v1.3"
        url = f"https://business-api.tiktok.com/open_api/{api_version}/event/track/"
        headers = {"Access-Token": token}
        payload = {
            "event_source": "web",
            "event_source_id": pixel_id,
            "data": [event_payload],
        }
        return self._post_json(url, payload, headers=headers, request_summary={
            "provider": "tiktok",
            "pixel_id": pixel_id,
            "event_name": mapping.destination_event_name,
            "event_id": context["event_id"],
            "has_ttclid": bool(click_ids.get("ttclid")),
            "has_ttp": bool(cookies.get("_ttp") or cookies.get("ttp")),
        })

    def _send_custom(self, event: MessagingEvent, mapping: MessagingEventMapping, context: Dict[str, Any]) -> ProviderResult:
        config = context["config"]
        url = _first_non_empty(config.get("endpoint_url"), config.get("webhook_url"))
        if not url:
            return ProviderResult(status="skipped", error_message="Custom endpoint_url missing")
        headers = config.get("custom_headers") or {}
        if config.get("auth_header"):
            headers["Authorization"] = config["auth_header"]
        payload = {
            "event": mapping.destination_event_name,
            "event_id": context["event_id"],
            "properties": context["properties"],
            "attribution": context["attribution"],
        }
        return self._post_json(url, payload, headers=headers, request_summary={
            "provider": "custom",
            "event_name": mapping.destination_event_name,
            "event_id": context["event_id"],
        })

    def _post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        headers: Optional[Dict[str, str]] = None,
        request_summary: Optional[Dict[str, Any]] = None,
    ) -> ProviderResult:
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, json=payload, headers=headers)
            response_summary = {
                "status_code": response.status_code,
                "body": response.text[:2000] if response.text else None,
            }
            if 200 <= response.status_code < 300:
                return ProviderResult(status="sent", request_summary=request_summary, response_summary=response_summary)
            return ProviderResult(
                status="failed",
                request_summary=request_summary,
                response_summary=response_summary,
                error_message=f"Provider returned HTTP {response.status_code}",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        except httpx.RequestError as exc:
            return ProviderResult(
                status="failed",
                request_summary=request_summary,
                error_message=str(exc),
                retryable=True,
            )


destination_dispatcher = DestinationDispatcher()
