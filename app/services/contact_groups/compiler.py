"""Compile contact-group rules into one set-based SQLAlchemy query.

The compiler deliberately returns contact ids and uses correlated ``EXISTS``
predicates for auxiliary tables.  This keeps counts stable when a contact has
many events, scores or identities and makes preview/evaluate use the exact same
selection semantics.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import Float, String, and_, cast, exists, func, literal, not_, or_, select
from sqlalchemy.orm import Query, Session

from app.models import ContactPosition, UserFeatureStore, UserScoreSnapshot
from app.models.journey import JourneySnapshot
from app.models.messaging import ContactVerificationState, MessagingEvent, MessagingUser


class GroupFilterError(ValueError):
    """Raised for a rule that cannot be represented safely in SQL."""


_USER_FIELDS = {
    "email",
    "phone",
    "phone_e164",
    "lifecycle_stage",
    "segment",
    "segment_name",
    "segment_rule_id",
    "locale",
    "timezone",
    "created_via",
    "is_subscribed",
    "consent_marketing",
    "first_seen_at",
    "last_seen_at",
    "created_at",
    "updated_at",
}
_DATE_FIELDS = {"first_seen_at", "last_seen_at", "created_at", "updated_at"}


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _json_property(path: str, value: Any = None):
    key = path.split(".", 1)[1]
    if not key or any(not part for part in key.split(".")):
        raise GroupFilterError(f"Invalid property path: {path!r}")
    expression = MessagingUser.properties
    for part in key.split("."):
        expression = expression[part]
    candidates = [item for item in _values(value) if item is not None]
    sample = candidates[0] if candidates else value
    # SQLAlchemy's JSON comparator must match the value's SQL type. Comparing
    # ``as_string()`` directly with a Python bool generates varchar = boolean
    # on PostgreSQL, while numeric ordering would otherwise be lexicographic.
    if isinstance(sample, bool):
        return expression.as_boolean()
    if isinstance(sample, (int, float)) and not isinstance(sample, bool):
        return expression.as_float()
    return expression.as_string()


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif not isinstance(value, str):
        raise GroupFilterError("Date filter values must be ISO-8601 strings")
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GroupFilterError(f"Invalid ISO-8601 datetime: {value!r}") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _compare(expression, operator: str, value: Any, *, numeric: bool = False):
    operator = operator.lower().strip()
    if operator in {"exists", "present", "is_set"}:
        return expression.isnot(None)
    if operator in {"missing", "not_exists", "is_not_set"}:
        return expression.is_(None)

    values = _values(value)
    if operator in {"in", "any"}:
        return expression.in_(values)
    if operator in {"not_in", "none"}:
        return not_(expression.in_(values))
    if operator in {"equals", "eq", "="}:
        return expression == value
    if operator in {"not_equals", "neq", "ne", "!="}:
        return expression != value
    if operator in {"contains"}:
        return cast(expression, String).contains(str(value))
    if operator in {"not_contains"}:
        return not_(cast(expression, String).contains(str(value)))

    comparable = cast(expression, Float) if numeric else expression
    if operator in {"gt", ">"}:
        return comparable > value
    if operator in {"gte", ">="}:
        return comparable >= value
    if operator in {"lt", "<"}:
        return comparable < value
    if operator in {"lte", "<="}:
        return comparable <= value
    if operator == "between":
        if len(values) != 2:
            raise GroupFilterError("between requires exactly two values")
        return comparable.between(values[0], values[1])
    raise GroupFilterError(f"Unsupported operator: {operator!r}")


def _date_compare(expression, operator: str, value: Any, now: datetime):
    operator = operator.lower().strip()
    if operator in {"within_last_days", "recent"}:
        return expression >= now - timedelta(days=int(value))
    if operator in {"older_than_days", "not_within_last_days"}:
        return expression < now - timedelta(days=int(value))
    if operator in {"before", "lt", "<"}:
        return expression < _coerce_datetime(value)
    if operator in {"after", "gt", ">"}:
        return expression > _coerce_datetime(value)
    if operator == "between":
        values = _values(value)
        if len(values) != 2:
            raise GroupFilterError("between requires exactly two dates")
        return expression.between(_coerce_datetime(values[0]), _coerce_datetime(values[1]))
    return _compare(expression, operator, value)


def _tag_predicate(operator: str, value: Any):
    # ``tags`` is a JSON string array.  Including JSON quotes in the pattern
    # prevents substring matches ("vip" does not match "vip-plus") while
    # remaining portable to PostgreSQL and SQLite tests.
    predicates = [cast(MessagingUser.tags, String).contains(f'"{str(item)}"') for item in _values(value)]
    if not predicates:
        return literal(False)
    if operator in {"contains", "in", "any", "equals", "eq"}:
        return or_(*predicates)
    if operator in {"not_contains", "not_in", "none", "not_equals", "neq"}:
        return and_(*(not_(predicate) for predicate in predicates))
    if operator in {"exists", "present", "is_set"}:
        return MessagingUser.tags.isnot(None)
    if operator in {"missing", "not_exists", "is_not_set"}:
        return MessagingUser.tags.is_(None)
    raise GroupFilterError(f"Unsupported tag operator: {operator!r}")


def _event_predicate(project_id: int, flt: dict[str, Any], now: datetime):
    event_name = flt.get("event_name") or flt.get("value")
    if not event_name:
        raise GroupFilterError("event filters require event_name or value")
    statement = select(MessagingEvent.id).where(
        MessagingEvent.project_id == project_id,
        MessagingEvent.user_id == MessagingUser.id,
        MessagingEvent.event_name == str(event_name),
    )
    within_days = flt.get("within_days")
    if within_days is not None:
        statement = statement.where(MessagingEvent.created_at >= now - timedelta(days=int(within_days)))
    event_exists = exists(statement)
    operator = str(flt.get("operator") or "performed").lower()
    return not_(event_exists) if operator in {"not_performed", "missing", "not_exists"} else event_exists


def _feature_predicate(project_id: int, flt: dict[str, Any]):
    event_name = flt.get("event_name") or flt.get("value")
    metric = str(flt.get("metric") or "count_total")
    if metric not in {"count_total", "count_1d", "count_7d", "count_30d", "sum_value", "last_seen_at"}:
        raise GroupFilterError(f"Unsupported event aggregate metric: {metric!r}")
    target = getattr(UserFeatureStore, metric)
    predicate = _compare(target, str(flt.get("operator") or "gte"), flt.get("threshold", 1), numeric=metric != "last_seen_at")
    return exists(select(UserFeatureStore.id).where(
        UserFeatureStore.project_id == project_id,
        UserFeatureStore.user_id == MessagingUser.id,
        UserFeatureStore.event_name == str(event_name),
        predicate,
    ))


def _verification_predicate(project_id: int, field: str, operator: str, value: Any, now: datetime):
    verification_type = "whatsapp" if "whatsapp" in field else "email"
    hash_column = MessagingUser.phone_hash if verification_type == "whatsapp" else MessagingUser.email_hash
    predicates = [
        ContactVerificationState.project_id == project_id,
        ContactVerificationState.user_id == MessagingUser.id,
        ContactVerificationState.verification_type == verification_type,
        ContactVerificationState.identifier_hash == hash_column,
    ]
    if field.endswith(".fresh"):
        predicates.append(ContactVerificationState.expires_at > now)
        if value is False or operator in {"not_equals", "neq", "missing"}:
            return not_(exists(select(ContactVerificationState.id).where(*predicates)))
    elif field.endswith(".expired"):
        predicates.append(ContactVerificationState.expires_at <= now)
    else:
        predicates.append(_compare(ContactVerificationState.canonical_status, operator, value))
    return exists(select(ContactVerificationState.id).where(*predicates))


def compile_condition(
    project_id: int,
    flt: dict[str, Any],
    now: datetime,
    lifecycle_model_id: int | None = None,
):
    field = str(flt.get("field") or "").strip()
    operator = str(flt.get("operator") or "equals").lower()
    value = flt.get("value")
    if not field:
        raise GroupFilterError("Every filter requires a field")

    if field in {"tag", "tags"}:
        return _tag_predicate(operator, value)
    if field in {"event", "event_performed"}:
        return _event_predicate(project_id, flt, now)
    if field in {"event_aggregate", "feature"}:
        return _feature_predicate(project_id, flt)
    if field.startswith("property."):
        return _compare(_json_property(field, value), operator, value)
    if field.startswith("verification."):
        return _verification_predicate(project_id, field, operator, value, now)

    if field.startswith("position."):
        column_name = field.split(".", 1)[1]
        if column_name == "age_days":
            cutoff = now - timedelta(days=int(value))
            position_predicate = (
                ContactPosition.position_entered_at <= cutoff
                if operator in {"gte", "gt", ">", ">="}
                else ContactPosition.position_entered_at > cutoff
            )
        elif column_name in {"type", "stage", "age_bucket"}:
            position_predicate = _compare(getattr(ContactPosition, column_name), operator, value)
        else:
            raise GroupFilterError(f"Unsupported position field: {field!r}")
        return exists(select(ContactPosition.id).where(
            ContactPosition.project_id == project_id,
            ContactPosition.user_id == MessagingUser.id,
            *(
                [ContactPosition.lifecycle_model_id == lifecycle_model_id]
                if lifecycle_model_id is not None
                else []
            ),
            position_predicate,
        ))

    if field in {"thermal_state", "journey.thermal_state"}:
        return exists(select(JourneySnapshot.id).where(
            JourneySnapshot.project_id == project_id,
            JourneySnapshot.user_id == MessagingUser.id,
            _compare(JourneySnapshot.thermal_state, operator, value),
        ))

    if field.startswith("score."):
        parts = field.split(".")
        if len(parts) != 3 or parts[2] not in {"score", "tier"}:
            raise GroupFilterError("Score fields use score.<definition_id>.<score|tier>")
        try:
            definition_id = int(parts[1])
        except ValueError as exc:
            raise GroupFilterError("Score definition id must be an integer") from exc
        score_column = getattr(UserScoreSnapshot, parts[2])
        return exists(select(UserScoreSnapshot.id).where(
            UserScoreSnapshot.project_id == project_id,
            UserScoreSnapshot.user_id == MessagingUser.id,
            UserScoreSnapshot.score_definition_id == definition_id,
            _compare(score_column, operator, value, numeric=parts[2] == "score"),
        ))

    normalized_field = "segment_name" if field == "segment" else field
    if normalized_field in _USER_FIELDS:
        expression = getattr(MessagingUser, normalized_field)
        return (
            _date_compare(expression, operator, value, now)
            if normalized_field in _DATE_FIELDS
            else _compare(expression, operator, value)
        )
    raise GroupFilterError(f"Unsupported contact group field: {field!r}")


def normalized_rule(rule_config: dict[str, Any] | None) -> tuple[str, list[dict[str, Any]]]:
    rule_config = rule_config or {}
    conditions = rule_config.get("filters", rule_config.get("conditions", []))
    if not isinstance(conditions, list) or any(not isinstance(item, dict) for item in conditions):
        raise GroupFilterError("filters must be an array of objects")
    mode = str(rule_config.get("match", rule_config.get("match_mode", "all"))).lower()
    if mode not in {"all", "any"}:
        raise GroupFilterError("match must be 'all' or 'any'")
    return mode, conditions


def base_contact_query(db: Session, project_id: int) -> Query:
    """The purpose-neutral universe for groups.

    Consent and channel eligibility intentionally are *not* part of a generic
    group.  Campaign/Ads callers apply their own purpose-specific policy.
    """
    return db.query(MessagingUser.id).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.status == "active",
        MessagingUser.is_sandbox == False,  # noqa: E712
    )


def compile_group_query(
    db: Session,
    project_id: int,
    rule_config: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    lifecycle_model_id: int | None = None,
) -> Query:
    query = base_contact_query(db, project_id)
    if lifecycle_model_id is not None:
        query = query.filter(exists(select(ContactPosition.id).where(
            ContactPosition.project_id == project_id,
            ContactPosition.user_id == MessagingUser.id,
            ContactPosition.lifecycle_model_id == lifecycle_model_id,
        )))
    mode, conditions = normalized_rule(rule_config)
    if not conditions:
        return query
    predicates = [
        compile_condition(
            project_id,
            item,
            now or datetime.utcnow(),
            lifecycle_model_id=lifecycle_model_id,
        )
        for item in conditions
    ]
    return query.filter(or_(*predicates) if mode == "any" else and_(*predicates))


def chunked(values: Iterable[int], size: int = 1000) -> Iterable[list[int]]:
    batch: list[int] = []
    for value in values:
        batch.append(int(value))
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
