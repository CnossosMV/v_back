"""
Condition Evaluator for Event Actions

Evaluates conditions against event properties and user data.
Supports various operators for flexible rule matching.
"""
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime
import re

logger = logging.getLogger(__name__)


class ConditionEvaluator:
    """
    Evaluates conditions against event and user data.

    Condition format:
    {
        "field": "properties.cart_value",
        "operator": ">=",
        "value": 100
    }

    Supported operators:
    - Comparison: ==, !=, <, <=, >, >=
    - String: contains, not_contains, starts_with, ends_with, matches (regex)
    - Array: in, not_in
    - Existence: exists, not_exists
    - Type: is_empty, is_not_empty
    """

    OPERATORS = {
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        "<": lambda a, b: a < b if a is not None and b is not None else False,
        "<=": lambda a, b: a <= b if a is not None and b is not None else False,
        ">": lambda a, b: a > b if a is not None and b is not None else False,
        ">=": lambda a, b: a >= b if a is not None and b is not None else False,
        "contains": lambda a, b: str(b).lower() in str(a).lower() if a else False,
        "not_contains": lambda a, b: str(b).lower() not in str(a).lower() if a else True,
        "starts_with": lambda a, b: str(a).lower().startswith(str(b).lower()) if a else False,
        "ends_with": lambda a, b: str(a).lower().endswith(str(b).lower()) if a else False,
        "matches": lambda a, b: bool(re.search(b, str(a), re.IGNORECASE)) if a else False,
        "not_matches": lambda a, b: not bool(re.search(b, str(a), re.IGNORECASE)) if a else True,
        "in": lambda a, b: a in b if isinstance(b, (list, tuple)) else False,
        "not_in": lambda a, b: a not in b if isinstance(b, (list, tuple)) else True,
        "exists": lambda a, b: a is not None,
        "not_exists": lambda a, b: a is None,
        "is_empty": lambda a, b: not a or (isinstance(a, (list, dict, str)) and len(a) == 0),
        "is_not_empty": lambda a, b: bool(a) and (not isinstance(a, (list, dict, str)) or len(a) > 0),
    }

    def evaluate_all(
        self,
        conditions: List[Dict],
        event_data: Dict[str, Any],
        user_data: Optional[Dict[str, Any]] = None,
        match_mode: str = "all",
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Evaluate all conditions against event and user data.

        Args:
            conditions: List of condition dicts
            event_data: Event properties
            user_data: User properties (optional)
            match_mode: "all" (AND) or "any" (OR)
            extra_context: Additional context (e.g. score data)

        Returns:
            True if conditions are satisfied
        """
        if not conditions:
            return True

        context = self._build_context(event_data, user_data, extra_context)

        results = []
        self._last_details: List[Dict[str, Any]] = []
        for condition in conditions:
            try:
                result = self.evaluate_single(condition, context)
                results.append(result)
                self._last_details.append({
                    "field": condition.get("field", ""),
                    "operator": condition.get("operator", "=="),
                    "expected": condition.get("value"),
                    "actual": self._get_nested_value(context, condition.get("field", "")),
                    "result": result,
                })
            except Exception as e:
                logger.warning(f"Condition evaluation failed: {condition}, error: {e}")
                results.append(False)
                self._last_details.append({
                    "field": condition.get("field", ""),
                    "operator": condition.get("operator", "=="),
                    "expected": condition.get("value"),
                    "actual": None,
                    "result": False,
                    "error": str(e),
                })

        if match_mode == "any":
            return any(results)
        return all(results)

    def evaluate_single(self, condition: Dict, context: Dict[str, Any]) -> bool:
        """
        Evaluate a single condition against context.

        Args:
            condition: {field, operator, value}
            context: Merged event and user data

        Returns:
            True if condition is satisfied
        """
        field = condition.get("field", "")
        operator = condition.get("operator", "==")
        expected = condition.get("value")

        # Get actual value from context using dot notation
        actual = self._get_nested_value(context, field)

        # Get operator function
        op_func = self.OPERATORS.get(operator)
        if not op_func:
            logger.warning(f"Unknown operator: {operator}")
            return False

        try:
            # Handle type coercion for numeric comparisons
            if operator in ("<", "<=", ">", ">=") and actual is not None:
                actual = self._to_number(actual)
                expected = self._to_number(expected)

            return op_func(actual, expected)
        except Exception as e:
            logger.warning(f"Comparison failed: {actual} {operator} {expected}: {e}")
            return False

    def _build_context(
        self,
        event_data: Dict[str, Any],
        user_data: Optional[Dict[str, Any]] = None,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build evaluation context from event and user data.

        Context structure:
        {
            "event_name": "...",
            "properties": {...},
            "user": {...},
            "score": {...}   # if extra_context provided
        }
        """
        context = {
            "event_name": event_data.get("event_name", ""),
            "properties": event_data.get("properties", {}),
            "source": event_data.get("source", ""),
            "user": user_data or {}
        }

        # Flatten properties for easy access
        if isinstance(context["properties"], dict):
            for key, value in context["properties"].items():
                context[key] = value

        # Flatten user properties
        if isinstance(context["user"], dict):
            for key, value in context["user"].items():
                context[f"user.{key}"] = value
                # Also allow direct access for common fields
                if key in ("email", "phone", "name", "external_id"):
                    context[f"user_{key}"] = value

        # Merge extra context (e.g. score data)
        if extra_context:
            context.update(extra_context)

        return context

    def _get_nested_value(self, data: Dict, field: str) -> Any:
        """
        Get value from nested dict using dot notation.
        e.g., "properties.cart_value" or "user.name"

        Also checks for flat keys with dots (e.g. "event.xyz" stored as a single key).
        """
        if not field:
            return None

        # Check flat key first (e.g. extra_context sets "event.xyz": True)
        if field in data:
            return data[field]

        parts = field.split(".")
        value = data

        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None

            if value is None:
                return None

        return value

    def _to_number(self, value: Any) -> Optional[float]:
        """Convert value to number for comparison."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
