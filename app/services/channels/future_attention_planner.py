"""Bounded, explainable planning over known future attention intents.

Planning is advisory when an intent is authored and binding only at the
locked dispatch gate.  It never calls a provider and never replaces Guardian.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import ContactLedger, SendLog
from app.services.channels.selection import (
    ATTENTION_CONTENDER_STATUSES,
    eligible_candidates,
    future_plan_config,
    future_plan_mode,
    group_by_contact,
    has_future_reservation,
    rank_candidates,
)


class FutureAttentionPlanner:
    """Build and persist one tenant's explainable agenda of known intents."""

    def __init__(self, db: Session):
        self.db = db

    def preview_contact(
        self,
        project_id: int,
        *,
        user_id: int | None = None,
        recipient: str | None = None,
        attention_scope: str | None = None,
        as_of: datetime | None = None,
        horizon_minutes: int | None = None,
        collision_window_minutes: int | None = None,
    ) -> dict[str, Any]:
        if user_id is None and not recipient:
            raise ValueError("user_id or recipient is required")
        now = as_of or datetime.utcnow()
        configured = future_plan_config(self.db, project_id)
        horizon = int(horizon_minutes or configured["horizon_minutes"])
        collision = int(
            collision_window_minutes or configured["collision_window_minutes"]
        )
        if horizon < 60 or horizon > 43200:
            raise ValueError("horizon_minutes must be between 60 and 43200")
        if collision < 1 or collision > min(horizon, 10080):
            raise ValueError(
                "collision_window_minutes must be between 1 and the bounded horizon"
            )
        horizon_end = now + timedelta(minutes=horizon)
        query = self.db.query(SendLog).filter(
            SendLog.project_id == project_id,
            SendLog.status.in_(ATTENTION_CONTENDER_STATUSES),
            SendLog.scheduled_at.isnot(None),
            SendLog.scheduled_at <= horizon_end,
            SendLog.is_historical == False,  # noqa: E712
        )
        if user_id is not None:
            query = query.filter(SendLog.user_id == user_id)
        else:
            query = query.filter(SendLog.recipient == recipient)
        if attention_scope:
            if attention_scope == "contact.promotional":
                query = query.filter(or_(
                    SendLog.attention_scope == attention_scope,
                    SendLog.attention_scope.is_(None),
                ))
            else:
                query = query.filter(SendLog.attention_scope == attention_scope)
        max_rows = configured["max_candidates_per_contact"]
        rows = query.order_by(
            SendLog.attention_scope.asc(),
            SendLog.scheduled_at.asc(),
            SendLog.id.asc(),
        ).limit(max_rows + 1).all()
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        rows = [
            row for row in eligible_candidates(rows, now)
            if not getattr(row, "expires_at", None)
            or row.scheduled_at <= now
            or row.expires_at > row.scheduled_at
        ]

        mode = future_plan_mode(self.db, project_id)
        windows: list[dict[str, Any]] = []
        for group_key, contenders in group_by_contact(rows).items():
            for index, window_rows in enumerate(
                self._windows(contenders, now, timedelta(minutes=collision)), start=1
            ):
                window = self._window(
                    window_rows,
                    now=now,
                    mode=mode,
                    collision_minutes=collision,
                    window_id=f"{group_key[-1]}:{index}",
                )
                windows.append(window)

        blocked_now = sum(
            1 for window in windows
            if window["consequence_at_gate"] == "hold_due_for_future_reservation"
        )
        past_context = self._past_context(project_id, user_id, now)
        future_pressure = self._future_pressure(rows)
        return {
            "project_id": project_id,
            "contact": {"user_id": user_id, "recipient": recipient},
            "mode": mode,
            "tenant_policy": {
                **configured,
                "horizon_minutes": horizon,
                "collision_window_minutes": collision,
            },
            "evaluated_at": now.isoformat(),
            "horizon_end": horizon_end.isoformat(),
            "candidate_count": len(rows),
            "truncated": truncated,
            "windows": windows,
            "past_context": past_context,
            "known_future_pressure": future_pressure,
            "consequence_at_gate": {
                "known_collision_windows": len(windows),
                "due_slots_held_now": blocked_now,
                "external_sends": 0,
                "rule": (
                    "Tenant-authored future reservations may hold a lower-ranked due intent only "
                    "inside the configured collision window and only in enforce mode."
                ),
                "decision_deadline": (
                    "The binding decision is recomputed under the contact/scope lock immediately "
                    "before dispatch; Guardian then rechecks permission and pressure."
                ),
                "historical_evidence": (
                    "ContactLedger supplies already-consumed attention, including external touches that "
                    "were explicitly counted as messages. Guardian is still authoritative at dispatch."
                ),
            },
            "external_sends": 0,
        }

    def refresh_for(self, send_log: SendLog, *, as_of: datetime | None = None) -> dict[str, Any]:
        """Refresh persisted plan evidence for every row in the same contact."""
        report = self.preview_contact(
            int(send_log.project_id),
            user_id=send_log.user_id,
            recipient=None if send_log.user_id is not None else send_log.recipient,
            attention_scope=send_log.attention_scope or "contact.promotional",
            as_of=as_of,
        )
        consequence_by_id = {
            int(item["send_log_id"]): item
            for window in report["windows"]
            for item in window["candidates"]
            if item["send_log_id"] is not None
        }
        evaluated_at = datetime.fromisoformat(report["evaluated_at"])
        horizon_end = datetime.fromisoformat(report["horizon_end"])
        for row_id, consequence in consequence_by_id.items():
            row = self.db.query(SendLog).filter(SendLog.id == row_id).first()
            if not row:
                continue
            row.planning_status = consequence["consequence"]
            row.planning_evaluated_at = evaluated_at
            row.planning_horizon_end = horizon_end
            row.planning_snapshot = {
                "mode": report["mode"],
                "tenant_policy": report["tenant_policy"],
                "evaluated_at": report["evaluated_at"],
                "horizon_end": report["horizon_end"],
                "candidate": consequence,
                "external_sends": 0,
            }
        self.db.flush()
        report["consequences_by_id"] = consequence_by_id
        return report

    def configuration_consequence(
        self,
        project_id: int,
        *,
        mode: str,
        config: dict[str, int],
        effective_mode: str | None = None,
    ) -> dict[str, Any]:
        pending = self.db.query(SendLog).filter(
            SendLog.project_id == project_id,
            SendLog.status.in_(ATTENTION_CONTENDER_STATUSES),
            SendLog.is_historical == False,  # noqa: E712
        ).count()
        reserving_count = self.db.query(SendLog.id).filter(
            SendLog.project_id == project_id,
            SendLog.status.in_(ATTENTION_CONTENDER_STATUSES),
            SendLog.attention_policy_snapshot["future_reservation"].as_boolean() == True,  # noqa: E712
            SendLog.is_historical == False,  # noqa: E712
        ).count()
        resolved_mode = effective_mode or ("off" if mode == "inherit" else mode)
        return {
            "feature_key": "future_plan",
            "requested_mode": mode,
            "effective_mode": resolved_mode,
            "tenant_policy": config,
            "pending_intents_to_replan": pending,
            "future_reservation_intents": reserving_count,
            "consequence_at_gate": {
                "off": "Future intents remain visible only to ordinary due-time selection.",
                "shadow": "Compute and explain holds without changing dispatch.",
                "enforce": "A higher-ranked opted-in future intent can hold a due intent inside the configured window.",
            }[resolved_mode],
            "safety_invariants": [
                "planning is not provider authorization",
                "the contact/scope contest is recomputed at dispatch",
                "Guardian still checks consent, quiet hours, cooldowns and caps",
                "unknown future facts cannot retroactively reclaim consumed attention",
            ],
            "external_sends": 0,
        }

    def _past_context(
        self,
        project_id: int,
        user_id: int | None,
        now: datetime,
    ) -> dict[str, Any]:
        if user_id is None:
            return {
                "available": False,
                "reason": "recipient_only_identity_has_no_contact_ledger",
            }
        from app.services.clock import FrozenClock
        from app.services.guardian.guardian_service import GuardianService

        recent = self.db.query(ContactLedger).filter(
            ContactLedger.project_id == project_id,
            ContactLedger.user_id == user_id,
            ContactLedger.sent_at <= now,
        ).order_by(ContactLedger.sent_at.desc(), ContactLedger.id.desc()).limit(20).all()
        return {
            "available": True,
            "attention_budget": GuardianService(
                self.db, clock=FrozenClock(now),
            ).budget_snapshot(project_id, user_id),
            "recent_counted_touches": [
                {
                    "ledger_id": row.id,
                    "channel": row.channel,
                    "source": row.source,
                    "source_id": row.source_id,
                    "occurred_at": row.sent_at.isoformat(),
                }
                for row in recent
            ],
            "interpretation": (
                "These are factual, already-consumed touches. A source=external_touch row means an "
                "operator explicitly chose count_as_message=true. Support/commercial meaning is not "
                "inferred from free text."
            ),
        }

    @staticmethod
    def _future_pressure(rows: list[SendLog]) -> dict[str, Any]:
        by_channel: dict[str, int] = {}
        by_purpose: dict[str, int] = {}
        for row in rows:
            channel = row.channel or row.preferred_channel or "unresolved"
            purpose = row.purpose_key or "unmapped"
            by_channel[channel] = by_channel.get(channel, 0) + 1
            by_purpose[purpose] = by_purpose.get(purpose, 0) + 1
        return {
            "candidate_count": len(rows),
            "by_channel": by_channel,
            "by_purpose": by_purpose,
            "interpretation": (
                "Candidates are intentions, not forecast sends. Winners, new facts and Guardian may reduce them."
            ),
        }

    @staticmethod
    def _windows(
        contenders: list[SendLog],
        now: datetime,
        collision: timedelta,
    ) -> list[list[SendLog]]:
        ordered = sorted(
            contenders,
            key=lambda row: (max(row.scheduled_at, now), row.id or 0),
        )
        result: list[list[SendLog]] = []
        anchor: datetime | None = None
        for row in ordered:
            effective = max(row.scheduled_at, now)
            if anchor is None or effective > anchor + collision:
                result.append([])
                anchor = effective
            result[-1].append(row)
        return result

    @staticmethod
    def _window(
        contenders: list[SendLog],
        *,
        now: datetime,
        mode: str,
        collision_minutes: int,
        window_id: str,
    ) -> dict[str, Any]:
        due = [row for row in contenders if row.scheduled_at <= now]
        future = [row for row in contenders if row.scheduled_at > now]
        reserving = [row for row in future if has_future_reservation(row)]
        advisory = rank_candidates(contenders)
        enforceable = rank_candidates([*due, *reserving])
        due_ranked = rank_candidates(due)
        owner = enforceable["winner"] or advisory["winner"]
        owner_is_future_reservation = owner in reserving
        binding_hold = bool(due and owner_is_future_reservation and mode == "enforce")
        shadow_hold = bool(due and owner_is_future_reservation and mode == "shadow")
        dispatch = None if binding_hold else due_ranked["winner"]
        candidates = []
        for row in rank_candidates(contenders)["ordered"]:
            is_due = row in due
            is_reserving = row in reserving
            if row is owner and is_reserving:
                consequence = "reserves_attention" if binding_hold else "would_reserve_attention"
            elif is_due and binding_hold:
                consequence = "held_by_future_reservation"
            elif is_due and row is dispatch:
                consequence = "dispatch_candidate"
            elif is_due:
                consequence = "occluded_due_candidate"
            elif is_reserving:
                consequence = "future_reservation_out_ranked"
            else:
                consequence = "future_visible_no_hold"
            policy = getattr(row, "attention_policy_snapshot", None) or {}
            candidates.append({
                "send_log_id": row.id,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "purpose_key": row.purpose_key,
                "attention_scope": row.attention_scope or "contact.promotional",
                "scheduled_at": row.scheduled_at.isoformat(),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "intent_tier": row.intent_tier,
                "future_reservation": is_reserving,
                "ordinal_reason": policy.get("ordinal_reason"),
                "consequence": consequence,
            })
        return {
            "window_id": window_id,
            "window_start": min(max(row.scheduled_at, now) for row in contenders).isoformat(),
            "window_end": (
                min(max(row.scheduled_at, now) for row in contenders)
                + timedelta(minutes=collision_minutes)
            ).isoformat(),
            "advisory_winner_id": getattr(advisory["winner"], "id", None),
            "attention_owner_id": getattr(owner, "id", None),
            "dispatch_candidate_id": getattr(dispatch, "id", None),
            "decision_deadline": (
                owner.scheduled_at.isoformat() if owner_is_future_reservation else now.isoformat()
            ),
            "consequence_at_gate": (
                "hold_due_for_future_reservation"
                if binding_hold
                else "would_hold_due_for_future_reservation"
                if shadow_hold
                else "dispatch_due_winner"
                if dispatch is not None
                else "future_plan_only"
            ),
            "candidates": candidates,
        }
