"""
Webhook Event Transformers

Transforms raw webhook payloads from third-party services into normalized
Versya events that flow through the standard event pipeline.

Built-in transformers: Stripe, Calendly, Typeform
Custom transformer: JSONPath-based field extraction
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class NormalizedEvent:
    """Standard event format produced by all transformers."""
    event_name: str
    contact_ref: Dict[str, Optional[str]]  # {external_id, email, phone}
    properties: Dict[str, Any]
    occurred_at: Optional[datetime] = None
    source: str = ""
    raw_webhook_id: Optional[int] = None


# ── Stripe ─────────────────────────────────────────────────────────────

STRIPE_EVENT_MAP = {
    "invoice.paid": "payment_completed",
    "invoice.payment_failed": "payment_failed",
    "customer.subscription.created": "subscription_started",
    "customer.subscription.deleted": "subscription_cancelled",
    "customer.subscription.updated": "subscription_updated",
    "checkout.session.completed": "checkout_completed",
}


class StripeTransformer:
    """Transforms Stripe webhook events into normalized events."""

    def transform(self, raw_payload: Dict[str, Any], ingest_id: int) -> Optional[NormalizedEvent]:
        event_type = raw_payload.get("type", "")
        event_name = STRIPE_EVENT_MAP.get(event_type)
        if not event_name:
            event_name = f"stripe.{event_type.replace('.', '_')}"

        data_obj = raw_payload.get("data", {}).get("object", {})

        # Extract contact reference
        email = (
            data_obj.get("customer_email")
            or data_obj.get("email")
            or data_obj.get("receipt_email")
        )
        customer_id = data_obj.get("customer") or data_obj.get("id", "")

        # Build properties based on event type
        properties: Dict[str, Any] = {
            "stripe_event_type": event_type,
            "stripe_event_id": raw_payload.get("id", ""),
        }

        if event_type.startswith("invoice."):
            properties.update({
                "amount": data_obj.get("amount_paid", data_obj.get("amount_due", 0)) / 100,
                "currency": data_obj.get("currency", "usd"),
                "invoice_id": data_obj.get("id", ""),
                "subscription_id": data_obj.get("subscription", ""),
            })
        elif event_type.startswith("customer.subscription."):
            properties.update({
                "subscription_id": data_obj.get("id", ""),
                "plan_id": data_obj.get("plan", {}).get("id", ""),
                "status": data_obj.get("status", ""),
                "interval": data_obj.get("plan", {}).get("interval", ""),
            })
        elif event_type == "checkout.session.completed":
            properties.update({
                "session_id": data_obj.get("id", ""),
                "amount_total": data_obj.get("amount_total", 0) / 100,
                "currency": data_obj.get("currency", "usd"),
                "payment_status": data_obj.get("payment_status", ""),
            })
            email = email or data_obj.get("customer_details", {}).get("email")

        # Timestamp
        created = raw_payload.get("created")
        occurred_at = datetime.utcfromtimestamp(created) if created else None

        return NormalizedEvent(
            event_name=event_name,
            contact_ref={"external_id": customer_id, "email": email, "phone": None},
            properties=properties,
            occurred_at=occurred_at,
            source="",  # Set by processor
            raw_webhook_id=ingest_id,
        )

    def get_idempotency_key(self, raw_payload: Dict[str, Any]) -> Optional[str]:
        return raw_payload.get("id")


# ── Calendly ───────────────────────────────────────────────────────────

CALENDLY_EVENT_MAP = {
    "invitee.created": "meeting_scheduled",
    "invitee.canceled": "meeting_cancelled",
}


class CalendlyTransformer:
    """Transforms Calendly webhook events into normalized events."""

    def transform(self, raw_payload: Dict[str, Any], ingest_id: int) -> Optional[NormalizedEvent]:
        event_type = raw_payload.get("event", "")
        event_name = CALENDLY_EVENT_MAP.get(event_type, f"calendly.{event_type.replace('.', '_')}")

        payload = raw_payload.get("payload", {})
        invitee = payload.get("invitee", {}) or payload

        email = invitee.get("email")
        name = invitee.get("name", "")

        properties: Dict[str, Any] = {
            "calendly_event_type": event_type,
            "invitee_name": name,
            "event_type_name": payload.get("event_type", {}).get("name", ""),
            "event_type_slug": payload.get("event_type", {}).get("slug", ""),
            "scheduled_at": payload.get("event", {}).get("start_time", ""),
        }

        # Tracking UTM if present
        tracking = invitee.get("tracking", {})
        if tracking:
            for key in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"):
                val = tracking.get(key)
                if val:
                    properties[key] = val

        occurred_at_str = raw_payload.get("created_at") or payload.get("created_at")
        occurred_at = None
        if occurred_at_str:
            try:
                occurred_at = datetime.fromisoformat(occurred_at_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return NormalizedEvent(
            event_name=event_name,
            contact_ref={"external_id": None, "email": email, "phone": None},
            properties=properties,
            occurred_at=occurred_at,
            source="",
            raw_webhook_id=ingest_id,
        )

    def get_idempotency_key(self, raw_payload: Dict[str, Any]) -> Optional[str]:
        payload = raw_payload.get("payload", {})
        uri = payload.get("uri") or payload.get("invitee", {}).get("uri", "")
        event = raw_payload.get("event", "")
        if uri:
            return f"{event}:{uri}"
        return None


# ── Typeform ───────────────────────────────────────────────────────────


class TypeformTransformer:
    """Transforms Typeform webhook events into normalized events."""

    def transform(self, raw_payload: Dict[str, Any], ingest_id: int) -> Optional[NormalizedEvent]:
        event_type = raw_payload.get("event_type", "form_response")
        event_name = "form_submitted"

        form_response = raw_payload.get("form_response", {})
        answers = form_response.get("answers", [])
        hidden = form_response.get("hidden", {})

        # Extract email from answers (look for email field type)
        email = None
        phone = None
        properties: Dict[str, Any] = {
            "typeform_event_id": raw_payload.get("event_id", ""),
            "form_id": form_response.get("form_id", ""),
        }

        for answer in answers:
            field_type = answer.get("type", "")
            field_ref = answer.get("field", {}).get("ref", "")
            field_title = answer.get("field", {}).get("title", "")

            if field_type == "email":
                email = answer.get("email", "")
                properties[field_ref or "email"] = email
            elif field_type == "phone_number":
                phone = answer.get("phone_number", "")
                properties[field_ref or "phone"] = phone
            elif field_type == "text":
                properties[field_ref or field_title] = answer.get("text", "")
            elif field_type == "choice":
                properties[field_ref or field_title] = answer.get("choice", {}).get("label", "")
            elif field_type == "number":
                properties[field_ref or field_title] = answer.get("number")
            elif field_type == "boolean":
                properties[field_ref or field_title] = answer.get("boolean")
            elif field_type == "date":
                properties[field_ref or field_title] = answer.get("date", "")

        # Fallback to hidden fields for email
        if not email:
            email = hidden.get("email") or hidden.get("user_email")

        # Include relevant hidden fields in properties
        for key, val in hidden.items():
            if key not in ("email", "user_email"):
                properties[f"hidden_{key}"] = val

        # Calculate score if present
        calculated = form_response.get("calculated", {})
        if calculated.get("score") is not None:
            properties["score"] = calculated["score"]

        submitted_at_str = form_response.get("submitted_at") or form_response.get("landed_at")
        occurred_at = None
        if submitted_at_str:
            try:
                occurred_at = datetime.fromisoformat(submitted_at_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return NormalizedEvent(
            event_name=event_name,
            contact_ref={"external_id": None, "email": email, "phone": phone},
            properties=properties,
            occurred_at=occurred_at,
            source="",
            raw_webhook_id=ingest_id,
        )

    def get_idempotency_key(self, raw_payload: Dict[str, Any]) -> Optional[str]:
        return raw_payload.get("event_id")


# ── Custom (JSONPath) ──────────────────────────────────────────────────


class CustomTransformer:
    """
    Transforms arbitrary webhook payloads using a user-defined JSONPath config.

    Config format:
    {
        "event_name": "my_event" | "$..jsonpath",
        "contact_ref": {
            "external_id": "$.data.user_id",
            "email": "$.data.email",
            "phone": "$.data.phone"
        },
        "properties_map": {
            "amount": "$.data.amount",
            "status": "$.data.status"
        },
        "occurred_at": "$.data.timestamp",
        "idempotency_key": "$.data.id"
    }
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}

    def _extract(self, payload: Dict[str, Any], path_or_literal: str) -> Any:
        """Extract a value using JSONPath or return literal."""
        if not path_or_literal:
            return None
        if not path_or_literal.startswith("$"):
            return path_or_literal  # literal value

        try:
            from jsonpath_ng import parse
            expr = parse(path_or_literal)
            matches = expr.find(payload)
            if matches:
                return matches[0].value
        except Exception as e:
            logger.debug("JSONPath extraction failed for %s: %s", path_or_literal, e)
        return None

    def transform(self, raw_payload: Dict[str, Any], ingest_id: int) -> Optional[NormalizedEvent]:
        # Event name
        event_name_cfg = self.config.get("event_name", "custom_webhook")
        event_name = self._extract(raw_payload, event_name_cfg) or "custom_webhook"
        if not isinstance(event_name, str):
            event_name = str(event_name)

        # Contact reference
        contact_cfg = self.config.get("contact_ref", {})
        contact_ref = {
            "external_id": self._extract(raw_payload, contact_cfg.get("external_id", "")),
            "email": self._extract(raw_payload, contact_cfg.get("email", "")),
            "phone": self._extract(raw_payload, contact_cfg.get("phone", "")),
        }
        # Ensure string values
        for k, v in contact_ref.items():
            if v is not None and not isinstance(v, str):
                contact_ref[k] = str(v)

        # Properties
        properties: Dict[str, Any] = {}
        props_map = self.config.get("properties_map", {})
        for prop_name, path in props_map.items():
            val = self._extract(raw_payload, path)
            if val is not None:
                properties[prop_name] = val

        # Occurred at
        occurred_at = None
        ts_path = self.config.get("occurred_at", "")
        if ts_path:
            ts_val = self._extract(raw_payload, ts_path)
            if isinstance(ts_val, (int, float)):
                occurred_at = datetime.utcfromtimestamp(ts_val)
            elif isinstance(ts_val, str):
                try:
                    occurred_at = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass

        return NormalizedEvent(
            event_name=event_name,
            contact_ref=contact_ref,
            properties=properties,
            occurred_at=occurred_at,
            source="",
            raw_webhook_id=ingest_id,
        )

    def get_idempotency_key(self, raw_payload: Dict[str, Any]) -> Optional[str]:
        key_path = self.config.get("idempotency_key", "")
        if key_path:
            val = self._extract(raw_payload, key_path)
            return str(val) if val is not None else None
        return None


# ── Factory ────────────────────────────────────────────────────────────

_BUILT_IN_TRANSFORMERS = {
    "stripe": StripeTransformer,
    "calendly": CalendlyTransformer,
    "typeform": TypeformTransformer,
}


def get_transformer(source_type: str, source_slug: str, config: Optional[Dict[str, Any]] = None):
    """
    Factory function to get the appropriate transformer.

    Args:
        source_type: 'built_in' or 'custom'
        source_slug: Source identifier (e.g., 'stripe', 'calendly', 'typeform')
        config: Transformer config (required for custom sources)

    Returns:
        A transformer instance with transform() and get_idempotency_key() methods
    """
    if source_type == "built_in":
        cls = _BUILT_IN_TRANSFORMERS.get(source_slug.lower())
        if cls:
            return cls()
        logger.warning("No built-in transformer for slug '%s', falling back to custom", source_slug)

    return CustomTransformer(config or {})
