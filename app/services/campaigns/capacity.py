"""Provider-profile capacity planning and durable reservations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.campaigns import (
    CampaignRun,
    CampaignWave,
    ChannelCapacityReservation,
    ChannelDeliveryProfile,
)
from app.services.campaigns.recurrence import as_utc_naive, parse_datetime, timezone_or_error


class CapacityPlanError(ValueError):
    pass


def lock_delivery_profile(db: Session, project_id: int, profile_id: int) -> None:
    """Serialize delivery-profile mutation with durable capacity reservation."""
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {
        "key": f"campaign-delivery-profile:{project_id}:{profile_id}",
    })


def _parse_clock(value: str | None, default: time) -> time:
    if not value:
        return default
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError) as exc:
        raise CapacityPlanError(f"Invalid send-window time: {value!r}") from exc


def _profile_window(profile: ChannelDeliveryProfile, local_day: date) -> tuple[datetime, datetime]:
    profile_tz = timezone_or_error(profile.timezone or "UTC")
    config = profile.config or {}
    window = config.get("send_window") or {}
    start_clock = _parse_clock(window.get("start"), time(9, 0))
    end_clock = _parse_clock(window.get("end"), time(17, 0))
    start = datetime.combine(local_day, start_clock, profile_tz)
    end = datetime.combine(local_day, end_clock, profile_tz)
    if end <= start:
        end += timedelta(days=1)
    return as_utc_naive(start), as_utc_naive(end)


def _bucket(profile: ChannelDeliveryProfile, moment_utc: datetime) -> tuple[datetime, datetime, date]:
    tz = timezone_or_error(profile.timezone or "UTC")
    aware = moment_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    local_day = aware.date()
    start_local = datetime.combine(local_day, time.min, tz)
    end_local = start_local + timedelta(days=1)
    return as_utc_naive(start_local), as_utc_naive(end_local), local_day


def _daily_capacity(
    profile: ChannelDeliveryProfile,
    policy_config: dict[str, Any],
    local_day: date | None = None,
) -> int:
    profile_tz = timezone_or_error(profile.timezone or "UTC")
    local_day = local_day or datetime.now(profile_tz).date()
    window_start, window_end = _profile_window(profile, local_day)
    minutes = max(1, int((window_end - window_start).total_seconds() // 60))
    hours = max(1, math.ceil(minutes / 60))
    candidates: list[int] = []
    if profile.max_per_day:
        candidates.append(int(profile.max_per_day))
    if profile.max_per_hour:
        candidates.append(int(profile.max_per_hour) * hours)
    if profile.max_per_minute:
        candidates.append(int(profile.max_per_minute) * minutes)
    if not candidates:
        return 0
    nominal = min(candidates)
    margin_percent = int(policy_config.get("capacity_margin_percent", 10))
    if margin_percent < 0 or margin_percent >= 100:
        raise CapacityPlanError("capacity_margin_percent must be between 0 and 99")
    warmup = profile.warmup_config or {}
    warmup_limit = warmup.get("current_daily_limit")
    if warmup_limit is not None:
        nominal = min(nominal, max(0, int(warmup_limit)))
    return max(0, math.floor(nominal * (100 - margin_percent) / 100))


class CampaignCapacityPlanner:
    def __init__(self, db: Session, clock=None):
        self.db = db
        from app.services.clock import SystemClock
        self.clock = clock or SystemClock()

    def profiles(self, project_id: int, channel: str) -> list[ChannelDeliveryProfile]:
        return self.db.query(ChannelDeliveryProfile).filter(
            ChannelDeliveryProfile.project_id == project_id,
            ChannelDeliveryProfile.channel == channel,
            ChannelDeliveryProfile.status == "active",
            ChannelDeliveryProfile.health_status.in_(["healthy", "degraded"]),
        ).order_by(
            ChannelDeliveryProfile.priority.desc(),
            ChannelDeliveryProfile.id,
        ).all()

    def _reserved(self, profile_id: int, bucket_start: datetime, bucket_end: datetime) -> int:
        return int(self.db.query(func.coalesce(func.sum(ChannelCapacityReservation.units_reserved), 0)).filter(
            ChannelCapacityReservation.delivery_profile_id == profile_id,
            ChannelCapacityReservation.status.in_(["reserved", "active", "consumed"]),
            ChannelCapacityReservation.bucket_start < bucket_end,
            ChannelCapacityReservation.bucket_end > bucket_start,
        ).scalar() or 0)

    def _allocation_for_moment(
        self,
        profiles: list[ChannelDeliveryProfile],
        moment: datetime,
        units: int,
        policy_config: dict[str, Any],
        virtual_reserved: dict[tuple[int, datetime], int],
    ) -> list[dict[str, Any]]:
        available: dict[int, int] = {}
        buckets: dict[int, tuple[datetime, datetime, date]] = {}
        for profile in profiles:
            bucket_start, bucket_end, local_day = _bucket(profile, moment)
            buckets[profile.id] = (bucket_start, bucket_end, local_day)
            nominal = _daily_capacity(profile, policy_config, local_day)
            used = self._reserved(profile.id, bucket_start, bucket_end)
            used += virtual_reserved.get((profile.id, bucket_start), 0)
            available[profile.id] = max(0, nominal - used)
        if sum(available.values()) < units:
            return []

        remaining = units
        assigned = {profile.id: 0 for profile in profiles}
        while remaining:
            active = [profile for profile in profiles if available[profile.id] > assigned[profile.id]]
            if not active:
                break
            score_total = sum(max(1, profile.weight or 1) for profile in active)
            progressed = False
            for profile in active:
                room = available[profile.id] - assigned[profile.id]
                share = max(1, math.floor(remaining * max(1, profile.weight or 1) / score_total))
                take = min(room, share, remaining)
                if take:
                    assigned[profile.id] += take
                    remaining -= take
                    progressed = True
                if remaining == 0:
                    break
            if not progressed:
                break
        if remaining:
            return []

        result = []
        for profile in profiles:
            count = assigned[profile.id]
            if not count:
                continue
            bucket_start, bucket_end, local_day = buckets[profile.id]
            window_start, window_end = _profile_window(profile, local_day)
            # An exact/start time later than the normal window begins there;
            # never schedule before the requested moment.
            send_start = max(moment, window_start)
            if send_start >= window_end:
                # The bucket is still capacity-valid, but this profile has no
                # remaining send window for that moment.
                return []
            result.append({
                "delivery_profile_id": profile.id,
                "sender_identity_id": profile.sender_identity_id,
                "provider": profile.provider,
                "count": count,
                "bucket_start": bucket_start,
                "bucket_end": bucket_end,
                "window_start": send_start,
                "window_end": window_end,
            })
        for allocation in result:
            key = (allocation["delivery_profile_id"], allocation["bucket_start"])
            virtual_reserved[key] = virtual_reserved.get(key, 0) + allocation["count"]
        return result

    def preview(
        self,
        *,
        project_id: int,
        channel: str,
        units: int,
        schedule: dict[str, Any],
        policy_config: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        policy_config = dict(policy_config or {})
        now = now or self.clock.utcnow()
        mode = str(schedule.get("mode") or "start_forward")
        if mode not in {"start_forward", "send_by_deadline", "exact_waves"}:
            raise CapacityPlanError("mode must be start_forward, send_by_deadline or exact_waves")
        if units < 0:
            raise CapacityPlanError("units cannot be negative")
        profiles = self.profiles(project_id, channel)
        if units and not profiles:
            return {
                "feasible": False,
                "reason": "no_healthy_delivery_profiles",
                "mode": mode,
                "requested_count": units,
                "planned_count": 0,
                "waves": [],
            }
        if units == 0:
            return {"feasible": True, "mode": mode, "requested_count": 0, "planned_count": 0, "waves": []}

        virtual_reserved: dict[tuple[int, datetime], int] = {}
        waves: list[dict[str, Any]] = []
        remaining = units

        if mode == "exact_waves":
            exact = schedule.get("waves") or []
            if not isinstance(exact, list) or not exact:
                raise CapacityPlanError("exact_waves requires a non-empty waves array")
            if sum(int(item.get("count") or 0) for item in exact) != units:
                raise CapacityPlanError("Exact-wave counts must equal the eligible recipient count")
            for position, item in enumerate(exact):
                count = int(item.get("count") or 0)
                if count <= 0:
                    raise CapacityPlanError("Every exact wave must have a positive count")
                scheduled_at = as_utc_naive(parse_datetime(item.get("scheduled_at"), field="scheduled_at"))
                if scheduled_at < now:
                    raise CapacityPlanError("Exact waves cannot be scheduled in the past")
                allocations = self._allocation_for_moment(
                    profiles, scheduled_at, count, policy_config, virtual_reserved,
                )
                if count and not allocations:
                    return self._infeasible(mode, units, waves, "insufficient_capacity_for_exact_wave")
                waves.append({
                    "position": position,
                    "scheduled_at": scheduled_at,
                    "planned_count": count,
                    "allocations": allocations,
                    "requires_approval": bool(
                        item.get("requires_approval", False)
                        or item.get("approval_mode") == "manual"
                    ),
                    "approval_mode": item.get("approval_mode") or (
                        "manual" if item.get("requires_approval", False) else "none"
                    ),
                    "is_canary": bool(item.get("is_canary", False)),
                })
            remaining = 0
        else:
            start_value = schedule.get("start_at") or now
            start_at = max(now, as_utc_naive(parse_datetime(start_value, field="start_at")) if not isinstance(start_value, datetime) else as_utc_naive(start_value))
            campaign_tz = timezone_or_error(schedule.get("timezone") or "UTC")
            start_local = start_at.replace(tzinfo=timezone.utc).astimezone(campaign_tz)
            # A start timestamp is a lower bound, not the default clock for
            # every future day.  Without an explicit clock, begin at 09:00 in
            # the campaign timezone; if today's window passed, planning moves
            # to the next eligible day instead of repeating an after-hours
            # timestamp forever.
            send_clock = _parse_clock(schedule.get("time"), time(9, 0))
            if mode == "start_forward":
                max_days = max(1, min(int(schedule.get("max_planning_days") or 366), 3660))
                days = [start_local.date() + timedelta(days=offset) for offset in range(max_days)]
            else:
                if not schedule.get("deadline_at"):
                    raise CapacityPlanError("send_by_deadline requires deadline_at")
                deadline = as_utc_naive(parse_datetime(schedule["deadline_at"], field="deadline_at"))
                if deadline < start_at:
                    raise CapacityPlanError("deadline_at must be after start_at")
                deadline_local = deadline.replace(tzinfo=timezone.utc).astimezone(campaign_tz)
                span = (deadline_local.date() - start_local.date()).days
                days = [deadline_local.date() - timedelta(days=offset) for offset in range(span + 1)]

            provisional: list[dict[str, Any]] = []
            for local_day in days:
                if remaining <= 0:
                    break
                moment = as_utc_naive(datetime.combine(local_day, send_clock, campaign_tz))
                if moment < start_at:
                    moment = start_at
                day_available = sum(
                    max(0, _daily_capacity(profile, policy_config, _bucket(profile, moment)[2])
                        - self._reserved(profile.id, *_bucket(profile, moment)[:2])
                        - virtual_reserved.get((profile.id, _bucket(profile, moment)[0]), 0))
                    for profile in profiles
                )
                count = min(remaining, day_available)
                if count <= 0:
                    continue
                allocations = self._allocation_for_moment(
                    profiles, moment, count, policy_config, virtual_reserved,
                )
                if not allocations:
                    continue
                provisional.append({
                    "position": 0,
                    "scheduled_at": moment,
                    "planned_count": count,
                    "allocations": allocations,
                    "requires_approval": False,
                    "approval_mode": "none",
                    "is_canary": False,
                })
                remaining -= count
            if mode == "send_by_deadline":
                provisional.sort(key=lambda item: item["scheduled_at"])
            for position, wave in enumerate(provisional):
                wave["position"] = position
                waves.append(wave)

        if remaining:
            return self._infeasible(mode, units, waves, "insufficient_capacity_before_deadline" if mode == "send_by_deadline" else "insufficient_capacity")
        return {
            "feasible": True,
            "mode": mode,
            "requested_count": units,
            "planned_count": units,
            "estimated_start_at": waves[0]["scheduled_at"] if waves else None,
            "estimated_finish_at": max(
                allocation["window_end"]
                for wave in waves for allocation in wave["allocations"]
            ) if waves else None,
            "waves": waves,
        }

    @staticmethod
    def _infeasible(mode: str, units: int, waves: list[dict], reason: str) -> dict[str, Any]:
        planned = sum(int(wave["planned_count"]) for wave in waves)
        return {
            "feasible": False,
            "reason": reason,
            "mode": mode,
            "requested_count": units,
            "planned_count": planned,
            "shortfall": units - planned,
            "waves": waves,
        }

    def persist_plan(self, run: CampaignRun, plan: dict[str, Any]) -> list[CampaignWave]:
        if not plan.get("feasible"):
            raise CapacityPlanError(str(plan.get("reason") or "Capacity plan is not feasible"))
        # Lock in deterministic order so profile updates, identity updates and
        # multi-profile plans cannot cross after preview and before reserve.
        profile_ids = sorted({
            int(allocation["delivery_profile_id"])
            for item in plan.get("waves", [])
            for allocation in item.get("allocations", [])
        })
        for profile_id in profile_ids:
            lock_delivery_profile(self.db, run.project_id, profile_id)
        waves: list[CampaignWave] = []
        for item in plan.get("waves", []):
            wave = CampaignWave(
                project_id=run.project_id,
                run_id=run.id,
                position=item["position"],
                status="held" if item.get("requires_approval") else "planned",
                approval_mode=item.get("approval_mode") or "none",
                is_canary=bool(item.get("is_canary", False)),
                scheduled_at=item["scheduled_at"],
                window_start=min(a["window_start"] for a in item["allocations"]),
                window_end=max(a["window_end"] for a in item["allocations"]),
                planned_count=item["planned_count"],
            )
            self.db.add(wave)
            self.db.flush()
            waves.append(wave)
            for allocation in item["allocations"]:
                self._lock_bucket(run.project_id, allocation["delivery_profile_id"], allocation["bucket_start"])
                # Re-check under the bucket lock; another run may have consumed
                # the capacity between preview and confirmation.
                profile = self.db.query(ChannelDeliveryProfile).filter(
                    ChannelDeliveryProfile.id == allocation["delivery_profile_id"],
                    ChannelDeliveryProfile.project_id == run.project_id,
                ).first()
                if not profile:
                    raise CapacityPlanError("Delivery profile disappeared while reserving capacity")
                reserved = self._reserved(profile.id, allocation["bucket_start"], allocation["bucket_end"])
                allocation_day = _bucket(profile, allocation["bucket_start"])[2]
                if reserved + allocation["count"] > _daily_capacity(
                    profile,
                    run.campaign.policy_config or {},
                    allocation_day,
                ):
                    raise CapacityPlanError("Capacity changed; preview the campaign again")
                self.db.add(ChannelCapacityReservation(
                    project_id=run.project_id,
                    delivery_profile_id=profile.id,
                    run_id=run.id,
                    wave_id=wave.id,
                    bucket_start=allocation["bucket_start"],
                    bucket_end=allocation["bucket_end"],
                    units_reserved=allocation["count"],
                    units_consumed=0,
                    status="reserved",
                    reservation_key=f"campaign:{run.id}:wave:{wave.id}:profile:{profile.id}",
                ))
        self.db.flush()
        return waves

    def release_run(self, run: CampaignRun) -> int:
        now = datetime.utcnow()
        count = self.db.query(ChannelCapacityReservation).filter(
            ChannelCapacityReservation.project_id == run.project_id,
            ChannelCapacityReservation.run_id == run.id,
            ChannelCapacityReservation.status == "reserved",
        ).update({
            ChannelCapacityReservation.status: "released",
            ChannelCapacityReservation.released_at: now,
        }, synchronize_session=False)
        return count

    def _lock_bucket(self, project_id: int, profile_id: int, bucket_start: datetime) -> None:
        if self.db.get_bind().dialect.name != "postgresql":
            return
        self.db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {
            "key": f"campaign-capacity:{project_id}:{profile_id}:{bucket_start.isoformat()}"
        })
