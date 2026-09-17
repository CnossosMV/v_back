"""
Event Schema Validator Service
Validates events against defined schemas and provides GA4 standard event definitions.
"""
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from app.models.messaging import MessagingEventSchema


class EventSchemaValidator:
    """
    Validates event properties against defined schemas.
    Also provides predefined GA4 standard event schemas.
    """

    # GA4 Standard Events with their schemas
    GA4_STANDARD_EVENTS = [
        # E-commerce events
        {
            "event_name": "add_to_cart",
            "display_name": "Add to Cart",
            "category": "ecommerce",
            "description": "User adds an item to shopping cart",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "currency": {"type": "string", "description": "Currency code (e.g., USD)"},
                    "value": {"type": "number", "description": "Total value of items added"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "string"},
                                "item_name": {"type": "string"},
                                "price": {"type": "number"},
                                "quantity": {"type": "integer"}
                            }
                        }
                    }
                }
            },
            "required_properties": ["items"]
        },
        {
            "event_name": "remove_from_cart",
            "display_name": "Remove from Cart",
            "category": "ecommerce",
            "description": "User removes an item from shopping cart",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "currency": {"type": "string"},
                    "value": {"type": "number"},
                    "items": {"type": "array"}
                }
            },
            "required_properties": ["items"]
        },
        {
            "event_name": "view_item",
            "display_name": "View Item",
            "category": "ecommerce",
            "description": "User views an item/product",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "currency": {"type": "string"},
                    "value": {"type": "number"},
                    "items": {"type": "array"}
                }
            },
            "required_properties": ["items"]
        },
        {
            "event_name": "view_item_list",
            "display_name": "View Item List",
            "category": "ecommerce",
            "description": "User views a list of items/products",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "item_list_id": {"type": "string"},
                    "item_list_name": {"type": "string"},
                    "items": {"type": "array"}
                }
            },
            "required_properties": []
        },
        {
            "event_name": "begin_checkout",
            "display_name": "Begin Checkout",
            "category": "ecommerce",
            "description": "User initiates checkout process",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "currency": {"type": "string"},
                    "value": {"type": "number"},
                    "coupon": {"type": "string"},
                    "items": {"type": "array"}
                }
            },
            "required_properties": ["items"]
        },
        {
            "event_name": "purchase",
            "display_name": "Purchase",
            "category": "ecommerce",
            "description": "User completes a purchase",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string"},
                    "currency": {"type": "string"},
                    "value": {"type": "number"},
                    "tax": {"type": "number"},
                    "shipping": {"type": "number"},
                    "coupon": {"type": "string"},
                    "items": {"type": "array"}
                }
            },
            "required_properties": ["transaction_id", "value"]
        },
        {
            "event_name": "refund",
            "display_name": "Refund",
            "category": "ecommerce",
            "description": "A refund is issued",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string"},
                    "currency": {"type": "string"},
                    "value": {"type": "number"},
                    "items": {"type": "array"}
                }
            },
            "required_properties": ["transaction_id"]
        },
        # Engagement events
        {
            "event_name": "page_view",
            "display_name": "Page View",
            "category": "engagement",
            "description": "User views a page",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "page_title": {"type": "string"},
                    "page_location": {"type": "string"},
                    "page_referrer": {"type": "string"}
                }
            },
            "required_properties": []
        },
        {
            "event_name": "scroll",
            "display_name": "Scroll",
            "category": "engagement",
            "description": "User scrolls down page",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "percent_scrolled": {"type": "integer"}
                }
            },
            "required_properties": []
        },
        {
            "event_name": "click",
            "display_name": "Click",
            "category": "engagement",
            "description": "User clicks an element",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "link_text": {"type": "string"},
                    "link_url": {"type": "string"},
                    "link_domain": {"type": "string"},
                    "outbound": {"type": "boolean"}
                }
            },
            "required_properties": []
        },
        {
            "event_name": "search",
            "display_name": "Search",
            "category": "engagement",
            "description": "User performs a search",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "search_term": {"type": "string"}
                }
            },
            "required_properties": ["search_term"]
        },
        {
            "event_name": "form_submit",
            "display_name": "Form Submit",
            "category": "engagement",
            "description": "User submits a form",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "form_id": {"type": "string"},
                    "form_name": {"type": "string"},
                    "form_destination": {"type": "string"}
                }
            },
            "required_properties": []
        },
        # Lifecycle events
        {
            "event_name": "sign_up",
            "display_name": "Sign Up",
            "category": "lifecycle",
            "description": "User creates an account",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "description": "Signup method (email, google, etc.)"}
                }
            },
            "required_properties": []
        },
        {
            "event_name": "login",
            "display_name": "Login",
            "category": "lifecycle",
            "description": "User logs in",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "description": "Login method"}
                }
            },
            "required_properties": []
        },
        {
            "event_name": "share",
            "display_name": "Share",
            "category": "engagement",
            "description": "User shares content",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "method": {"type": "string"},
                    "content_type": {"type": "string"},
                    "item_id": {"type": "string"}
                }
            },
            "required_properties": []
        },
        # Lead gen events
        {
            "event_name": "generate_lead",
            "display_name": "Generate Lead",
            "category": "conversion",
            "description": "User submits lead information",
            "properties_schema": {
                "type": "object",
                "properties": {
                    "currency": {"type": "string"},
                    "value": {"type": "number"}
                }
            },
            "required_properties": []
        }
    ]

    def validate_event_properties(
        self,
        schema: Dict[str, Any],
        properties: Dict[str, Any]
    ) -> List[str]:
        """
        Validate event properties against a schema.
        Returns list of validation errors (empty if valid).
        """
        errors = []

        if not schema or not properties:
            return errors

        properties_schema = schema.get("properties", {})
        required = schema.get("required", [])

        # Check required properties
        for req_prop in required:
            if req_prop not in properties:
                errors.append(f"Missing required property: {req_prop}")

        # Check property types
        for prop_name, prop_value in properties.items():
            if prop_name in properties_schema:
                expected_type = properties_schema[prop_name].get("type")
                if expected_type:
                    if not self._check_type(prop_value, expected_type):
                        errors.append(
                            f"Property '{prop_name}' should be type '{expected_type}', "
                            f"got '{type(prop_value).__name__}'"
                        )

        return errors

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Check if value matches expected JSON Schema type."""
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict
        }

        expected = type_map.get(expected_type)
        if expected is None:
            return True  # Unknown type, allow

        return isinstance(value, expected)

    def get_ga4_standard_schemas(self) -> List[Dict[str, Any]]:
        """Get all predefined GA4 standard event schemas."""
        return self.GA4_STANDARD_EVENTS

    async def import_standard_events(
        self,
        db: Session,
        project_id: int
    ) -> List[MessagingEventSchema]:
        """
        Import GA4 standard events into a project.
        Skips events that already exist.
        """
        imported = []

        for event_def in self.GA4_STANDARD_EVENTS:
            # Check if already exists
            existing = db.query(MessagingEventSchema).filter(
                MessagingEventSchema.project_id == project_id,
                MessagingEventSchema.event_name == event_def["event_name"]
            ).first()

            if existing:
                continue

            schema = MessagingEventSchema(
                project_id=project_id,
                event_name=event_def["event_name"],
                display_name=event_def["display_name"],
                description=event_def["description"],
                category=event_def["category"],
                properties_schema=event_def["properties_schema"],
                required_properties=event_def["required_properties"],
                is_standard=True
            )
            db.add(schema)
            imported.append(schema)

        db.commit()

        for schema in imported:
            db.refresh(schema)

        return imported


# Singleton instance
event_schema_validator = EventSchemaValidator()
