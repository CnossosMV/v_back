"""
Schema Auto-Discovery Service
Automatically creates/updates event schemas when events are ingested.
"""
import logging
from typing import Optional, Dict, Any, Set, Tuple
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.messaging import MessagingEventSchema

logger = logging.getLogger(__name__)

# Category inference based on event name prefixes/keywords
CATEGORY_RULES = {
    "ecommerce": [
        "purchase", "checkout", "cart", "add_to_cart", "remove_from_cart",
        "begin_checkout", "add_payment", "add_shipping", "refund",
        "view_item", "view_cart", "select_item", "select_promotion",
        "view_promotion", "add_to_wishlist", "order",
    ],
    "engagement": [
        "page_view", "scroll", "click", "view", "screen_view",
        "session_start", "first_visit", "search", "share",
        "select_content", "video", "file_download",
    ],
    "lifecycle": [
        "sign_up", "login", "logout", "register", "onboard",
        "activate", "deactivate", "subscribe", "unsubscribe",
        "upgrade", "downgrade", "cancel", "churn", "reactivate",
        "profile_update", "password",
    ],
    "conversion": [
        "generate_lead", "complete_registration", "submit",
        "convert", "goal", "trial", "demo", "book", "schedule",
        "request", "contact", "form_submit",
    ],
    "form": [
        "form_start", "form_complete", "form_abandon", "form_error",
        "form_field", "form_step",
    ],
    "channel": [
        "channel.",
    ],
}


def _infer_category(event_name: str) -> str:
    """Infer event category from event name."""
    name_lower = event_name.lower()
    for category, keywords in CATEGORY_RULES.items():
        for keyword in keywords:
            if keyword in name_lower:
                return category
    return "custom"


def _humanize_name(event_name: str) -> str:
    """Convert snake_case event name to a human-readable display name."""
    return event_name.replace("_", " ").replace("-", " ").title()


def _infer_json_type(value: Any) -> str:
    """Infer JSON Schema type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_properties_schema(properties: Dict[str, Any]) -> Dict[str, Any]:
    """Build a JSON Schema from event properties."""
    if not properties:
        return {"type": "object", "properties": {}}

    schema_props = {}
    for key, value in properties.items():
        schema_props[key] = {
            "type": _infer_json_type(value),
        }

    return {
        "type": "object",
        "properties": schema_props,
    }


class SchemaDiscoveryService:
    """
    Discovers and auto-creates event schemas from ingested events.
    Uses an in-memory cache to avoid DB hits on every event.
    """

    def __init__(self):
        self._seen_schemas: Set[Tuple[int, str]] = set()

    def discover_from_event(
        self,
        db: Session,
        project_id: int,
        event_name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Auto-discover schema from an ingested event.
        Creates schema if missing, merges new property keys if schema exists.
        """
        cache_key = (project_id, event_name)

        # Fast path: already seen and no properties to merge
        if cache_key in self._seen_schemas and not properties:
            return

        try:
            existing = db.query(MessagingEventSchema).filter(
                MessagingEventSchema.project_id == project_id,
                MessagingEventSchema.event_name == event_name,
            ).first()

            if existing:
                # Add to cache
                self._seen_schemas.add(cache_key)

                # Merge new property keys (additive only)
                if properties:
                    current_schema = existing.properties_schema or {"type": "object", "properties": {}}
                    current_props = current_schema.get("properties", {})
                    new_schema = _build_properties_schema(properties)
                    new_props = new_schema.get("properties", {})

                    merged = False
                    for key, type_info in new_props.items():
                        if key not in current_props:
                            current_props[key] = type_info
                            merged = True

                    if merged:
                        current_schema["properties"] = current_props
                        existing.properties_schema = current_schema
                        db.commit()
            else:
                # Create new schema entry
                schema = MessagingEventSchema(
                    project_id=project_id,
                    event_name=event_name,
                    display_name=_humanize_name(event_name),
                    category=_infer_category(event_name),
                    properties_schema=_build_properties_schema(properties) if properties else {"type": "object", "properties": {}},
                    is_standard=False,
                    is_active=True,
                )
                db.add(schema)
                db.commit()
                self._seen_schemas.add(cache_key)

        except IntegrityError:
            # Race condition: another worker created the same schema
            db.rollback()
            self._seen_schemas.add(cache_key)
        except Exception as e:
            logger.warning(f"Schema discovery failed for {event_name}: {e}")
            db.rollback()


# Singleton instance
schema_discovery = SchemaDiscoveryService()
