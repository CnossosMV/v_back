"""Calendar recurrence helpers for campaign occurrences."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RecurrenceError(ValueError):
    pass


_MONTH_INTERVALS = {"monthly": 1, "semiannual": 6, "annual": 12}


def timezone_or_error(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise RecurrenceError(f"Unknown timezone: {name!r}") from exc


def parse_datetime(value, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise RecurrenceError(f"{field} must be an ISO-8601 datetime")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecurrenceError(f"Invalid {field}: {value!r}") from exc


def as_local(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        # Persistence uses naive UTC throughout this backend.
        return value.replace(tzinfo=timezone.utc).astimezone(tz)
    return value.astimezone(tz)


def as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _valid_local(value: datetime, tz: ZoneInfo) -> datetime:
    """Resolve DST gaps deterministically by moving to the first valid instant."""
    candidate = value.replace(tzinfo=tz, fold=0)
    roundtrip = candidate.astimezone(timezone.utc).astimezone(tz)
    if roundtrip.replace(tzinfo=None) != value:
        return roundtrip
    return candidate


def add_months(value: datetime, months: int, anchor_day: int | None = None) -> datetime:
    absolute = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(absolute, 12)
    month = month_index + 1
    day = min(anchor_day or value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def validate_recurrence(config: dict | None) -> dict:
    config = dict(config or {})
    frequency = str(config.get("frequency") or "").lower()
    if frequency not in {"weekly", "biweekly", "monthly", "semiannual", "annual"}:
        raise RecurrenceError(
            "frequency must be weekly, biweekly, monthly, semiannual or annual"
        )
    interval = int(config.get("interval") or 1)
    if interval < 1 or interval > 100:
        raise RecurrenceError("interval must be between 1 and 100")
    config["frequency"] = frequency
    config["interval"] = interval
    if config.get("until") is not None:
        parse_datetime(config["until"], field="until")
    return config


def next_occurrence(
    *,
    anchor: datetime,
    after: datetime,
    config: dict,
    timezone_name: str,
    max_iterations: int = 10000,
) -> datetime | None:
    config = validate_recurrence(config)
    tz = timezone_or_error(timezone_name)
    anchor_local = as_local(anchor, tz)
    after_utc = as_utc_naive(after)
    local_naive = anchor_local.replace(tzinfo=None)
    anchor_day = local_naive.day
    frequency = config["frequency"]
    interval = config["interval"]
    if frequency == "weekly":
        day_step = 7 * interval
    elif frequency == "biweekly":
        day_step = 14 * interval
    else:
        day_step = None
        month_step = _MONTH_INTERVALS[frequency] * interval

    until = config.get("until")
    until_utc = as_utc_naive(parse_datetime(until, field="until")) if until else None
    count = int(config.get("count") or 0)

    for occurrence_index in range(max_iterations):
        aware = _valid_local(local_naive, tz)
        candidate = as_utc_naive(aware)
        if candidate > after_utc:
            if until_utc is not None and candidate > until_utc:
                return None
            if count and occurrence_index >= count:
                return None
            return candidate
        if day_step is not None:
            from datetime import timedelta

            local_naive = local_naive + timedelta(days=day_step)
        else:
            local_naive = add_months(local_naive, month_step, anchor_day)
    raise RecurrenceError("Could not calculate the next occurrence")
