"""Tenant-owned commercial calendar and deterministic opportunity previews."""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.models.campaigns import CommercialOpportunity
from app.schemas.commercial_calendar import (
    CommercialOpportunityInput,
    OpportunityPreviewInput,
    OpportunityRuleInput,
)


class CommercialCalendarError(ValueError):
    pass


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _zone(name: str) -> tzinfo:
    if name in {"UTC", "Etc/UTC", "GMT"}:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise CommercialCalendarError(f"Unknown timezone: {name!r}") from exc


def _local_utc(day: date, clock: str, timezone_name: str) -> datetime:
    try:
        parsed = time.fromisoformat(clock)
    except (TypeError, ValueError) as exc:
        raise CommercialCalendarError(f"Invalid local time: {clock!r}") from exc
    aware = datetime.combine(day, parsed, tzinfo=_zone(timezone_name))
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _row_payload(row: CommercialOpportunity) -> dict[str, Any]:
    return {
        "id": row.id,
        "external_key": row.external_key,
        "name": row.name,
        "opportunity_type": row.opportunity_type,
        "status": row.status,
        "country_code": row.country_code,
        "region_code": row.region_code,
        "timezone": row.timezone,
        "starts_at": row.starts_at.isoformat(),
        "peak_at": row.peak_at.isoformat() if row.peak_at else None,
        "expires_at": row.expires_at.isoformat(),
        "priority": row.priority,
        "priority_source": row.priority_source,
        "priority_reason": row.priority_reason,
        "purpose_keys": list(row.purpose_keys or []),
        "context": row.context or {},
        "source": "persisted",
    }


class CommercialCalendarService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def contract() -> dict[str, Any]:
        return {
            "version": "1.0",
            "semantics": {
                "locale": "content language; never sufficient to choose a holiday calendar",
                "country_region": "calendar scope",
                "timezone": "local scheduling scope",
                "priority": "tenant-declared business relevance inside the promotional lane",
                "priority_source": (
                    "tenant_policy, historical_evidence, bounded_learning, or tie_requires_decision; "
                    "worker order is never a knowledge source"
                ),
                "selection": "opportunities inform candidates; project-wide Selection chooses attention",
            },
            "persisted_occurrences": ["holiday", "season", "payday", "custom"],
            "generated_rule_types": {
                "weekly": {
                    "config": {"weekdays": [4], "start_time": "10:00", "duration_hours": 72},
                    "weekday_numbering": "ISO-8601 Monday=1 ... Sunday=7",
                },
                "month_boundary": {
                    "config": {"days_before": 2, "days_after": 5, "start_time": "09:00", "end_time": "23:00"},
                    "meaning": "one window crossing the turn of the month",
                },
            },
            "invariants": [
                "an opportunity never authorizes a send",
                "known occurrences must be materialized before their decision lead time",
                "overlap is explained; campaigns never silently race",
                "learning may recommend priority changes but never auto-reorder declared intent",
            ],
        }

    def list(
        self,
        project_id: int,
        *,
        horizon_start: datetime | None = None,
        horizon_end: datetime | None = None,
        include_archived: bool = False,
    ) -> list[CommercialOpportunity]:
        query = self.db.query(CommercialOpportunity).filter(
            CommercialOpportunity.project_id == project_id,
        )
        if not include_archived:
            query = query.filter(CommercialOpportunity.status != "archived")
        if horizon_start is not None:
            query = query.filter(CommercialOpportunity.expires_at >= _utc_naive(horizon_start))
        if horizon_end is not None:
            query = query.filter(CommercialOpportunity.starts_at <= _utc_naive(horizon_end))
        return query.order_by(CommercialOpportunity.starts_at, CommercialOpportunity.id).all()

    def upsert(
        self,
        project_id: int,
        payload: CommercialOpportunityInput,
        actor_user_id: int | None,
        *,
        commit: bool = True,
    ) -> CommercialOpportunity:
        _zone(payload.timezone)
        values = payload.model_dump(mode="python")
        values["starts_at"] = _utc_naive(values["starts_at"])
        values["peak_at"] = _utc_naive(values["peak_at"]) if values.get("peak_at") else None
        values["expires_at"] = _utc_naive(values["expires_at"])
        row = self.db.query(CommercialOpportunity).filter(
            CommercialOpportunity.project_id == project_id,
            CommercialOpportunity.external_key == payload.external_key,
        ).first()
        if row is None:
            row = CommercialOpportunity(
                project_id=project_id,
                created_by_user_id=actor_user_id,
                **values,
            )
            self.db.add(row)
        else:
            for field, value in values.items():
                setattr(row, field, value)
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(row)
        return row

    def preview(self, project_id: int, request: OpportunityPreviewInput) -> dict[str, Any]:
        start = _utc_naive(request.horizon_start)
        end = _utc_naive(request.horizon_end)
        occurrences: list[dict[str, Any]] = []
        if request.include_persisted:
            for row in self.list(project_id, horizon_start=start, horizon_end=end):
                if not request.include_drafts and row.status != "active":
                    continue
                payload = _row_payload(row)
                if self._matches_scope(payload, request):
                    occurrences.append(payload)
        for rule in request.rules:
            for occurrence in self._generate(rule, start, end):
                if self._matches_scope(occurrence, request):
                    occurrences.append(occurrence)
        occurrences.sort(key=lambda item: (item["starts_at"], -int(item["priority"]), item["external_key"]))
        overlaps = self._overlaps(occurrences)
        return {
            "horizon_start": start.isoformat(),
            "horizon_end": end.isoformat(),
            "occurrences": occurrences,
            "occurrence_count": len(occurrences),
            "overlaps": overlaps,
            "overlap_count": len(overlaps),
            "external_sends": 0,
        }

    @staticmethod
    def _matches_scope(item: dict[str, Any], request: OpportunityPreviewInput) -> bool:
        if request.country_code and item.get("country_code") not in {None, request.country_code}:
            return False
        if request.region_code and item.get("region_code") not in {None, request.region_code}:
            return False
        if request.purpose_key:
            purpose_keys = item.get("purpose_keys") or []
            if purpose_keys and request.purpose_key not in purpose_keys:
                return False
        return True

    def _generate(
        self,
        rule: OpportunityRuleInput,
        horizon_start: datetime,
        horizon_end: datetime,
    ) -> list[dict[str, Any]]:
        _zone(rule.timezone)
        if rule.rule_type == "weekly":
            return self._weekly(rule, horizon_start, horizon_end)
        if rule.rule_type == "month_boundary":
            return self._month_boundaries(rule, horizon_start, horizon_end)
        raise CommercialCalendarError(f"Unsupported opportunity rule: {rule.rule_type}")

    def _weekly(self, rule: OpportunityRuleInput, start: datetime, end: datetime) -> list[dict[str, Any]]:
        config = rule.config or {}
        weekdays = sorted({int(value) for value in config.get("weekdays", [4])})
        if not weekdays or any(value < 1 or value > 7 for value in weekdays):
            raise CommercialCalendarError("weekly weekdays must use ISO values 1..7")
        duration = int(config.get("duration_hours") or 72)
        if duration < 1 or duration > 24 * 14:
            raise CommercialCalendarError("weekly duration_hours must be between 1 and 336")
        start_time = str(config.get("start_time") or "10:00")
        tz = _zone(rule.timezone)
        local_day = start.replace(tzinfo=timezone.utc).astimezone(tz).date() - timedelta(days=7)
        last_day = end.replace(tzinfo=timezone.utc).astimezone(tz).date() + timedelta(days=1)
        result = []
        while local_day <= last_day:
            if local_day.isoweekday() in weekdays:
                starts_at = _local_utc(local_day, start_time, rule.timezone)
                expires_at = starts_at + timedelta(hours=duration)
                if expires_at >= start and starts_at <= end:
                    result.append(self._generated_payload(rule, local_day.isoformat(), starts_at, expires_at))
            local_day += timedelta(days=1)
        return result

    def _month_boundaries(self, rule: OpportunityRuleInput, start: datetime, end: datetime) -> list[dict[str, Any]]:
        config = rule.config or {}
        before = int(config.get("days_before") or 2)
        after = int(config.get("days_after") or 5)
        if before < 0 or before > 31 or after < 0 or after > 31:
            raise CommercialCalendarError("month-boundary days must be between 0 and 31")
        start_time = str(config.get("start_time") or "09:00")
        end_time = str(config.get("end_time") or "23:00")
        tz = _zone(rule.timezone)
        local_start = start.replace(tzinfo=timezone.utc).astimezone(tz)
        local_end = end.replace(tzinfo=timezone.utc).astimezone(tz)
        absolute = local_start.year * 12 + local_start.month - 2
        last_absolute = local_end.year * 12 + local_end.month + 1
        result = []
        while absolute <= last_absolute:
            year, month_zero = divmod(absolute, 12)
            month = month_zero + 1
            first = date(year, month, 1)
            previous_last = first - timedelta(days=1)
            window_start_day = previous_last - timedelta(days=max(0, before - 1))
            window_end_day = first + timedelta(days=after)
            starts_at = _local_utc(window_start_day, start_time, rule.timezone)
            expires_at = _local_utc(window_end_day, end_time, rule.timezone)
            if expires_at >= start and starts_at <= end:
                key = f"{year:04d}-{month:02d}"
                result.append(self._generated_payload(rule, key, starts_at, expires_at, peak_at=_local_utc(first, start_time, rule.timezone)))
            absolute += 1
        return result

    @staticmethod
    def _generated_payload(
        rule: OpportunityRuleInput,
        suffix: str,
        starts_at: datetime,
        expires_at: datetime,
        *,
        peak_at: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "id": None,
            "external_key": f"{rule.rule_key}:{suffix}",
            "name": rule.name,
            "opportunity_type": rule.rule_type,
            "status": "preview",
            "country_code": rule.country_code.upper() if rule.country_code else None,
            "region_code": rule.region_code,
            "timezone": rule.timezone,
            "starts_at": starts_at.isoformat(),
            "peak_at": peak_at.isoformat() if peak_at else None,
            "expires_at": expires_at.isoformat(),
            "priority": rule.priority,
            "priority_source": rule.priority_source,
            "priority_reason": rule.priority_reason,
            "purpose_keys": list(rule.purpose_keys),
            "context": {"rule_key": rule.rule_key, "rule_config": rule.config},
            "source": "generated_preview",
        }

    @staticmethod
    def _overlaps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        overlaps: list[dict[str, Any]] = []
        for index, left in enumerate(items):
            left_start = datetime.fromisoformat(left["starts_at"])
            left_end = datetime.fromisoformat(left["expires_at"])
            for right in items[index + 1:]:
                right_start = datetime.fromisoformat(right["starts_at"])
                if right_start >= left_end:
                    break
                right_end = datetime.fromisoformat(right["expires_at"])
                if left.get("country_code") and right.get("country_code") and left["country_code"] != right["country_code"]:
                    continue
                if left.get("region_code") and right.get("region_code") and left["region_code"] != right["region_code"]:
                    continue
                overlap_start = max(left_start, right_start)
                overlap_end = min(left_end, right_end)
                if overlap_start >= overlap_end:
                    continue
                ordered = sorted([left, right], key=lambda item: (-int(item["priority"]), item["external_key"]))
                priority_tie = int(left["priority"]) == int(right["priority"])
                sources = {left.get("priority_source"), right.get("priority_source")}
                delegated_learning = priority_tie and sources == {"bounded_learning"}
                unresolved = "tie_requires_decision" in sources or (priority_tie and not delegated_learning)
                winner = None if priority_tie or unresolved else ordered[0]
                overlaps.append({
                    "opportunity_keys": [left["external_key"], right["external_key"]],
                    "starts_at": overlap_start.isoformat(),
                    "expires_at": overlap_end.isoformat(),
                    "declared_winner": winner["external_key"] if winner else None,
                    "declared_priority": int(winner["priority"]) if winner else None,
                    "knowledge_source": winner.get("priority_source") if winner else (
                        "bounded_learning" if delegated_learning else "tie_requires_decision"
                    ),
                    "priority_reason": winner.get("priority_reason") if winner else None,
                    "priority_tie": priority_tie,
                    "resolution_required": unresolved,
                    "combined_candidate_recommended": left["opportunity_type"] != right["opportunity_type"],
                    "reason": "overlapping_commercial_opportunities",
                })
        return overlaps
