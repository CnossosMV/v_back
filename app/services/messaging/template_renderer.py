"""
Template Rendering Service for Messaging Middleware
Handles variable substitution in message templates using {{variable}} syntax
"""
import re
import logging
from datetime import date, datetime
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger(__name__)


def _babel_locale(locale: Optional[str]) -> Optional[str]:
    """BCP-47 'pt-BR' → Babel locale 'pt_BR'. None ⇒ no locale formatting."""
    if not locale:
        return None
    return locale.replace("-", "_")


def _parse_dt(value):
    """Best-effort parse of an ISO date/datetime. Returns date/datetime or None.
    A date-only string ('YYYY-MM-DD') returns a `date` so it is never tz-shifted."""
    if isinstance(value, (date, datetime)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    if len(raw) == 10:  # date-only — do not promote to midnight datetime
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


class TemplateRenderer:
    """
    Renders message templates by replacing {{variable}} placeholders with values.

    Supports:
    - Simple variables: {{name}}, {{email}}
    - Nested properties: {{user.name}}, {{order.items.0.name}}
    - Default values: {{name|default:Guest}}
    - Locale formatting filters (content i18n):
        {{price|money}}       value = {amount, currency}; formats per locale, NEVER converts
        {{price|money:BRL}}   currency fallback when value is a bare number
        {{when|date}}         {{when|date:short|medium|long|full}} — formats in contact tz
        {{qty|number}}        locale decimal/grouping
      Filters are no-ops (raw str) when no locale is supplied — backward compatible.
    """

    # Regex patterns
    VARIABLE_PATTERN = re.compile(r'\{\{([^}]+)\}\}')
    DEFAULT_SEPARATOR = '|default:'
    _FILTER_NAMES = {"money", "date", "number"}

    def render_template(
        self,
        body: str,
        variables: Dict[str, Any],
        subject: Optional[str] = None,
        locale: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> Tuple[str, Optional[str], List[str], List[str]]:
        """
        Render a template by replacing variables with their values.

        Args:
            body: The template body with {{variable}} placeholders
            variables: Dictionary of variable values
            subject: Optional template subject (for emails)
            locale: BCP-47 locale of the contact (drives |money/|date/|number)
            timezone: IANA timezone of the contact (drives |date)

        Returns:
            Tuple[str, Optional[str], List[str], List[str]]:
            - rendered_body, rendered_subject, variables_used, missing_variables
        """
        variables_used: List[str] = []
        missing_variables: List[str] = []

        def replace_variable(match) -> str:
            var_name, filters, default_value = self._parse_spec(match.group(1).strip())
            value = self._get_nested_value(variables, var_name)

            if value is not None:
                variables_used.append(var_name)
                return self._apply_filters(value, filters, locale, timezone)
            elif default_value is not None:
                variables_used.append(var_name)
                return default_value
            else:
                missing_variables.append(var_name)
                return match.group(0)  # Keep the placeholder

        rendered_body = self.VARIABLE_PATTERN.sub(replace_variable, body)

        rendered_subject = None
        if subject:
            rendered_subject = self.VARIABLE_PATTERN.sub(replace_variable, subject)

        return rendered_body, rendered_subject, variables_used, missing_variables

    # ---- spec parsing + filters --------------------------------------------

    def _parse_spec(self, inner: str):
        """Parse '{{ name|filter:arg|default:x }}' → (var_name, [(fn, arg)], default)."""
        default_value = None
        if self.DEFAULT_SEPARATOR in inner:
            left, default_value = inner.split(self.DEFAULT_SEPARATOR, 1)
            default_value = default_value.strip()
        else:
            left = inner
        parts = [p.strip() for p in left.split('|')]
        var_name = parts[0]
        filters = []
        for f in parts[1:]:
            if not f:
                continue
            if ':' in f:
                fn, fa = f.split(':', 1)
                filters.append((fn.strip(), fa.strip()))
            else:
                filters.append((f, None))
        return var_name, filters, default_value

    def _apply_filters(self, value, filters, locale, timezone) -> str:
        for fname, farg in filters:
            if fname == "money":
                value = self._fmt_money(value, farg, locale)
            elif fname == "date":
                value = self._fmt_date(value, farg, locale, timezone)
            elif fname == "number":
                value = self._fmt_number(value, locale)
            # unknown filters are ignored (value untouched)
        return value if isinstance(value, str) else str(value)

    def _fmt_money(self, value, currency_arg, locale) -> str:
        """Format money per locale. NEVER converts. Currency must travel with the
        amount ({amount, currency}) or be given as :ARG; if absent, render raw."""
        amount, currency = None, None
        if isinstance(value, dict):
            amount = value.get("amount", value.get("value"))
            currency = value.get("currency", value.get("cur"))
        else:
            amount = value
        currency = currency or currency_arg
        if amount is None:
            return ""
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return str(value)
        if not currency:
            return str(amount)  # never guess a currency
        bl = _babel_locale(locale)
        if bl:
            try:
                from babel.numbers import format_currency
                return format_currency(amount, currency, locale=bl)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("money format failed (%s/%s): %s", currency, locale, e)
        return f"{currency} {amount}"

    def _fmt_date(self, value, fmt_arg, locale, timezone) -> str:
        dt = _parse_dt(value)
        if dt is None:
            return str(value)
        fmt = fmt_arg or "medium"
        bl = _babel_locale(locale)
        if bl:
            try:
                if isinstance(dt, datetime):
                    from babel.dates import format_datetime
                    tzinfo = None
                    if timezone:
                        try:
                            from zoneinfo import ZoneInfo
                            tzinfo = ZoneInfo(timezone)
                        except Exception:
                            tzinfo = None
                    return format_datetime(dt, format=fmt, tzinfo=tzinfo, locale=bl)
                from babel.dates import format_date
                return format_date(dt, format=fmt, locale=bl)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("date format failed (%s): %s", locale, e)
        return dt.isoformat()

    def _fmt_number(self, value, locale) -> str:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return str(value)
        bl = _babel_locale(locale)
        if bl:
            try:
                from babel.numbers import format_decimal
                return format_decimal(num, locale=bl)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("number format failed (%s): %s", locale, e)
        return str(value)

    def extract_variables(self, body: str, subject: Optional[str] = None) -> List[str]:
        """
        Extract all variable names from a template.

        Args:
            body: The template body
            subject: Optional template subject

        Returns:
            List[str]: List of unique variable names found
        """
        variables = set()

        for text in (body, subject):
            if not text:
                continue
            for match in self.VARIABLE_PATTERN.finditer(text):
                var_name, _filters, _default = self._parse_spec(match.group(1).strip())
                variables.add(var_name)

        return sorted(list(variables))

    def validate_variables(
        self,
        body: str,
        variables: Dict[str, Any],
        subject: Optional[str] = None,
        required_variables: Optional[List[str]] = None
    ) -> Tuple[bool, List[str]]:
        """
        Validate that all required variables are present.

        Args:
            body: The template body
            variables: Dictionary of variable values
            subject: Optional template subject
            required_variables: Optional list of required variable names

        Returns:
            Tuple[bool, List[str]]: (is_valid, missing_variables)
        """
        if required_variables is None:
            # Extract variables from template and consider all as required
            required_variables = self.extract_variables(body, subject)

        missing = []
        for var_name in required_variables:
            value = self._get_nested_value(variables, var_name)
            if value is None:
                missing.append(var_name)

        return len(missing) == 0, missing

    def _get_nested_value(self, data: Dict[str, Any], key: str) -> Any:
        """
        Get a value from a nested dictionary using dot notation.

        Args:
            data: The dictionary to search
            key: The key in dot notation (e.g., "user.name")

        Returns:
            Any: The value or None if not found
        """
        if not data:
            return None

        keys = key.split('.')
        value = data

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            elif isinstance(value, list):
                try:
                    idx = int(k)
                    value = value[idx] if 0 <= idx < len(value) else None
                except (ValueError, IndexError):
                    return None
            else:
                return None

            if value is None:
                return None

        return value

    def preview_template(
        self,
        body: str,
        subject: Optional[str] = None,
        sample_variables: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Generate a preview of a template with sample data.

        Args:
            body: The template body
            subject: Optional template subject
            sample_variables: Optional sample data for preview

        Returns:
            Dict[str, Any]: Preview result with rendered content and variable info
        """
        variables = self.extract_variables(body, subject)

        # Generate sample data if not provided
        if sample_variables is None:
            sample_variables = {}
            for var in variables:
                if 'first_name' in var.lower():
                    sample_variables[var] = 'John'
                elif 'email' in var.lower():
                    sample_variables[var] = 'user@example.com'
                elif 'name' in var.lower():
                    sample_variables[var] = 'John Doe'
                elif 'phone' in var.lower():
                    sample_variables[var] = '+1234567890'
                elif 'url' in var.lower() or 'link' in var.lower():
                    sample_variables[var] = 'https://example.com'
                else:
                    sample_variables[var] = f'[{var}]'

        rendered_body, rendered_subject, used, missing = self.render_template(
            body, sample_variables, subject
        )

        return {
            'rendered_body': rendered_body,
            'rendered_subject': rendered_subject,
            'variables': variables,
            'variables_used': used,
            'missing_variables': missing,
            'sample_data': sample_variables
        }


# Singleton instance
template_renderer = TemplateRenderer()
