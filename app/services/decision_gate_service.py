"""Persisted, project-scoped consequence evaluations and choices."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ContactLedger, LocalePolicyOverride, ProjectPolicy
from app.models.messaging import MessagingUser
from app.models.campaigns import Campaign
from app.models.engine_control import DecisionGateChoice, DecisionGateEvaluation, DecisionGateOutcome
from app.services.campaigns.service import CampaignService
from app.services.clock import SystemClock


class DecisionGateError(ValueError):
    pass


def _json_safe(value):
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, dict): return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_json_safe(v) for v in value]
    return value


class DecisionGateService:
    def __init__(self, db: Session, clock=None):
        self.db = db
        self.clock = clock or SystemClock()

    def _attention(self, project_id: int, user_ids: list[int], channel: str) -> dict[str, Any]:
        if not user_ids:
            return {"delayed_count": 0, "rules": {}, "next_allowed_at": None}
        policy = self.db.query(ProjectPolicy).filter(ProjectPolicy.project_id == project_id).first()
        if not policy or not policy.is_active:
            return {"delayed_count": 0, "rules": {}, "next_allowed_at": None}
        now = self.clock.utcnow(); blocked: dict[int, set[str]] = defaultdict(set); next_times = []
        locale_overrides = {
            row.locale: row for row in self.db.query(LocalePolicyOverride).filter(
                LocalePolicyOverride.project_id == project_id,
            ).all()
        }
        contacts = self.db.query(
            MessagingUser.id, MessagingUser.locale, MessagingUser.timezone,
        ).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.id.in_(user_ids),
        ).all()
        for user_id, locale, contact_timezone in contacts:
            override = locale_overrides.get(locale)
            quiet = (override.quiet_hours if override and override.quiet_hours else policy.quiet_hours) or {}
            channels = quiet.get("channels") or []
            if not quiet.get("enabled") or (channels and channel not in channels):
                continue
            try:
                tz = ZoneInfo(
                    contact_timezone
                    or (override.timezone if override else None)
                    or quiet.get("timezone")
                    or "UTC"
                )
                local_now = now.replace(tzinfo=timezone.utc).astimezone(tz)
                start_h, start_m = map(int, str(quiet.get("start", "22:00")).split(":"))
                end_h, end_m = map(int, str(quiet.get("end", "08:00")).split(":"))
                current = local_now.hour * 60 + local_now.minute
                start = start_h * 60 + start_m
                end = end_h * 60 + end_m
                active = current >= start or current < end if start > end else start <= current < end
                if active:
                    end_local = local_now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
                    if end_local <= local_now:
                        end_local += timedelta(days=1)
                    blocked[user_id].add("quiet_hours")
                    next_times.append(end_local.astimezone(timezone.utc).replace(tzinfo=None))
            except (ValueError, KeyError):
                # Invalid policy data is not silently extrapolated by the gate.
                blocked[user_id].add("quiet_hours_invalid")
        cooldown = int((policy.channel_cooldowns or {}).get(channel) or 0)
        if cooldown:
            rows = self.db.query(ContactLedger.user_id, func.max(ContactLedger.sent_at)).filter(
                ContactLedger.project_id == project_id, ContactLedger.user_id.in_(user_ids),
                ContactLedger.channel == channel,
                ContactLedger.sent_at >= now - timedelta(seconds=cooldown),
            ).group_by(ContactLedger.user_id).all()
            for user_id, last_sent in rows:
                blocked[user_id].add("channel_cooldown"); next_times.append(last_sent + timedelta(seconds=cooldown))
        periods = {
            "daily": (now.replace(hour=0, minute=0, second=0, microsecond=0), now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)),
            "weekly": ((now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0), (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7)),
        }
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = month_start.replace(year=month_start.year + 1, month=1) if month_start.month == 12 else month_start.replace(month=month_start.month + 1)
        periods["monthly"] = (month_start, next_month)
        caps = policy.contact_caps or {}
        for period, (start, reset) in periods.items():
            configured = caps.get(period) or {}
            if configured.get("total") is not None:
                rows = self.db.query(ContactLedger.user_id, func.count(ContactLedger.id)).filter(
                    ContactLedger.project_id == project_id, ContactLedger.user_id.in_(user_ids), ContactLedger.sent_at >= start,
                ).group_by(ContactLedger.user_id).having(func.count(ContactLedger.id) >= int(configured["total"])).all()
                for user_id, _ in rows: blocked[user_id].add("contact_caps"); next_times.append(reset)
            if configured.get(channel) is not None:
                rows = self.db.query(ContactLedger.user_id, func.count(ContactLedger.id)).filter(
                    ContactLedger.project_id == project_id, ContactLedger.user_id.in_(user_ids), ContactLedger.channel == channel,
                    ContactLedger.sent_at >= start,
                ).group_by(ContactLedger.user_id).having(func.count(ContactLedger.id) >= int(configured[channel])).all()
                for user_id, _ in rows: blocked[user_id].add("contact_caps"); next_times.append(reset)
        rule_counts: dict[str, int] = defaultdict(int)
        for rules in blocked.values():
            for rule in rules: rule_counts[rule] += 1
        return {
            "delayed_count": len(blocked), "rules": dict(rule_counts),
            "next_allowed_at": max(next_times).isoformat() if next_times else None,
        }

    def _campaign_input_snapshot(
        self,
        campaign: Campaign,
        schedule: dict[str, Any],
        *,
        plan: dict[str, Any] | None = None,
        attention: dict[str, Any] | None = None,
        group=None,
        audience_hash: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        service = CampaignService(self.db)
        plan = plan or service.preview_plan(campaign, schedule)
        if attention is None or group is None:
            eligibility, actions, resolved_group = service._snapshot(campaign)
            group = group or resolved_group
            eligible_ids = [item.user.id for item in eligibility if item.eligible]
            audience_hash = hashlib.sha256(
                ",".join(str(user_id) for user_id in sorted(eligible_ids)).encode()
            ).hexdigest()
            channel = actions[0].channel or campaign.default_channel
            attention = attention or self._attention(campaign.project_id, eligible_ids, channel)
        from app.services.engine_rollout_service import EngineRolloutService
        policy = self.db.query(ProjectPolicy).filter(
            ProjectPolicy.project_id == campaign.project_id,
        ).first()
        snapshot = _json_safe({
            "campaign_id": campaign.id,
            "campaign_version": campaign.version,
            "schedule": schedule,
            "candidate_count": plan["candidate_count"],
            "eligible_count": plan["eligible_count"],
            "planned_count": plan["planned_count"],
            "audience_hash": audience_hash,
            "capacity_plan": plan.get("capacity_plan") or {},
            "attention": attention,
            "group_id": group.id if group else None,
            "group_updated_at": group.updated_at if group else None,
            "group_last_evaluated_at": group.last_evaluated_at if group else None,
            "policy_updated_at": policy.updated_at if policy else None,
            "engine_modes": EngineRolloutService(self.db).effective_modes(campaign.project_id),
        })
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return snapshot, digest

    def evaluate_campaign(self, campaign: Campaign, schedule: dict[str, Any], actor_user_id: int | None, forced_override: dict | None = None) -> DecisionGateEvaluation:
        service = CampaignService(self.db)
        plan = service.preview_plan(campaign, schedule)
        eligibility, actions, group = service._snapshot(campaign)
        eligible_ids = [item.user.id for item in eligibility if item.eligible]
        audience_hash = hashlib.sha256(
            ",".join(str(user_id) for user_id in sorted(eligible_ids)).encode()
        ).hexdigest()
        channel = actions[0].channel or campaign.default_channel
        attention = self._attention(campaign.project_id, eligible_ids, channel)
        capacity = _json_safe(plan.get("capacity_plan") or {})
        conservative = {
            "key": "protect_reputation", "recommended": True, "risk": "low",
            "candidate_count": plan["candidate_count"], "eligible_count": plan["eligible_count"],
            "planned_count": plan["planned_count"], "suppressed_count": plan["suppressed_count"],
            "exclusions": plan.get("suppression_reasons") or {},
            "attention_delayed_count": attention["delayed_count"], "attention": attention,
            "capacity_plan": capacity,
            "cost": {"available": None, "currency": None, "reason": "provider_cost_not_configured"},
            "override": None,
        }
        options = [{"key": "do_nothing", "recommended": False, "risk": "low", "planned_count": 0}, conservative]
        threshold = max(25, int(max(1, plan["eligible_count"]) * 0.01))
        material_time = False
        if attention.get("next_allowed_at"):
            material_time = datetime.fromisoformat(attention["next_allowed_at"]) >= self.clock.utcnow() + timedelta(hours=6)
        if forced_override or attention["delayed_count"] >= threshold or material_time:
            override = forced_override or {
                "rules": sorted(attention["rules"]), "reason": "Accelerated option selected from consequence gate",
                "risk_acknowledged": True, "expires_in_hours": 24,
            }
            if override.get("rules"):
                options.append({**conservative, "key": "meet_deadline", "recommended": False, "risk": "high", "attention_delayed_count": 0, "override": override})
        input_snapshot, inputs_hash = self._campaign_input_snapshot(
            campaign, schedule, plan=plan, attention=attention, group=group,
            audience_hash=audience_hash,
        )
        row = DecisionGateEvaluation(
            project_id=campaign.project_id, gate_type="campaign_run", subject_type="campaign", subject_id=str(campaign.id),
            subject_version=campaign.version, method="exact", inputs_hash=inputs_hash, input_snapshot=input_snapshot,
            baseline=options[0], options=options,
            provenance={"audience": "exact", "eligibility": "exact", "attention": "exact", "capacity": "estimate", "delivery": "estimate", "cost": "estimate"},
            evaluated_at=self.clock.utcnow(), expires_at=self.clock.utcnow() + timedelta(minutes=30), created_by_user_id=actor_user_id,
        )
        self.db.add(row); self.db.commit(); self.db.refresh(row); return row

    def evaluate_rollout(self, project_id: int, updates: list[dict[str, Any]], actor_user_id: int | None) -> DecisionGateEvaluation:
        from app.services.engine_rollout_service import EngineRolloutService
        service = EngineRolloutService(self.db)
        dependencies = service.validate(project_id, updates)
        current = service.list_features(project_id)
        snapshot = _json_safe({"updates": updates, "current": current, "dependencies": dependencies})
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        options = [
            {"key": "do_nothing", "recommended": False, "risk": "low", "changes": []},
            {"key": "apply_rollout", "recommended": not dependencies, "risk": "moderate" if any(u.get("mode") == "enforce" for u in updates) else "low", "updates": updates, "dependencies": dependencies},
        ]
        row = DecisionGateEvaluation(
            project_id=project_id, gate_type="engine_rollout", subject_type="project", subject_id=str(project_id),
            status="valid", method="exact", inputs_hash=digest, input_snapshot=snapshot,
            baseline=options[0], options=options, provenance={"configuration": "exact", "dependencies": "exact"},
            evaluated_at=self.clock.utcnow(), expires_at=self.clock.utcnow() + timedelta(minutes=30), created_by_user_id=actor_user_id,
        )
        self.db.add(row); self.db.commit(); self.db.refresh(row); return row

    def get(self, project_id: int, evaluation_id: int) -> DecisionGateEvaluation | None:
        return self.db.query(DecisionGateEvaluation).filter(DecisionGateEvaluation.id == evaluation_id, DecisionGateEvaluation.project_id == project_id).first()

    def choose(self, project_id: int, evaluation_id: int, option_key: str, actor_user_id: int | None, reason: str | None = None) -> DecisionGateChoice:
        evaluation = self.db.query(DecisionGateEvaluation).filter(
            DecisionGateEvaluation.id == evaluation_id, DecisionGateEvaluation.project_id == project_id,
        ).with_for_update().first()
        if not evaluation: raise DecisionGateError("Decision evaluation not found")
        if evaluation.status != "valid" or evaluation.expires_at <= self.clock.utcnow():
            evaluation.status = "expired"; self.db.commit(); raise DecisionGateError("Decision evaluation is stale")
        if evaluation.subject_type == "campaign":
            campaign = self.db.query(Campaign).filter(Campaign.id == int(evaluation.subject_id), Campaign.project_id == project_id).first()
            if not campaign or campaign.version != evaluation.subject_version:
                evaluation.status = "stale"; self.db.commit(); raise DecisionGateError("Campaign changed after evaluation")
        option = next((item for item in evaluation.options if item.get("key") == option_key), None)
        if not option: raise DecisionGateError("Decision option not found")
        existing = self.db.query(DecisionGateChoice).filter(DecisionGateChoice.project_id == project_id, DecisionGateChoice.evaluation_id == evaluation.id).first()
        if existing:
            if existing.option_key == option_key: return existing
            raise DecisionGateError("Decision evaluation was already chosen")
        choice = DecisionGateChoice(project_id=project_id, evaluation_id=evaluation.id, option_key=option_key, consequence_snapshot=option, reason=reason, chosen_by_user_id=actor_user_id)
        self.db.add(choice); evaluation.status = "chosen"; self.db.commit(); self.db.refresh(choice); return choice

    def choice_for_run(self, project_id: int, campaign: Campaign, choice_id: int) -> DecisionGateChoice:
        choice = self.db.query(DecisionGateChoice).join(DecisionGateEvaluation, DecisionGateChoice.evaluation_id == DecisionGateEvaluation.id).filter(
            DecisionGateChoice.id == choice_id, DecisionGateChoice.project_id == project_id,
            DecisionGateEvaluation.subject_type == "campaign", DecisionGateEvaluation.subject_id == str(campaign.id),
        ).first()
        if not choice: raise DecisionGateError("Decision choice not found for campaign")
        evaluation = self.get(project_id, choice.evaluation_id)
        if not evaluation or evaluation.expires_at <= self.clock.utcnow() or evaluation.subject_version != campaign.version:
            raise DecisionGateError("Decision choice is stale")
        _, current_hash = self._campaign_input_snapshot(
            campaign, (evaluation.input_snapshot or {}).get("schedule") or {},
        )
        if current_hash != evaluation.inputs_hash:
            evaluation.status = "stale"
            self.db.commit()
            raise DecisionGateError("Campaign inputs changed after evaluation")
        if choice.option_key == "do_nothing": raise DecisionGateError("The do-nothing option cannot create a run")
        return choice

    def choice_for_rollout(
        self,
        project_id: int,
        choice_id: int,
        updates: list[dict[str, Any]],
    ) -> DecisionGateChoice:
        choice = self.db.query(DecisionGateChoice).join(
            DecisionGateEvaluation,
            DecisionGateChoice.evaluation_id == DecisionGateEvaluation.id,
        ).filter(
            DecisionGateChoice.id == choice_id,
            DecisionGateChoice.project_id == project_id,
            DecisionGateChoice.option_key == "apply_rollout",
            DecisionGateEvaluation.gate_type == "engine_rollout",
            DecisionGateEvaluation.subject_id == str(project_id),
        ).first()
        if not choice:
            raise DecisionGateError("Approved rollout decision not found")
        if choice.execution_id:
            raise DecisionGateError("Rollout decision was already executed")
        evaluation = self.get(project_id, choice.evaluation_id)
        if not evaluation or evaluation.expires_at <= self.clock.utcnow():
            raise DecisionGateError("Rollout decision is stale")
        approved = (choice.consequence_snapshot or {}).get("updates") or []
        if approved != updates:
            raise DecisionGateError("Execution differs from the approved rollout consequence")
        from app.services.engine_rollout_service import EngineRolloutService
        service = EngineRolloutService(self.db)
        current = service.list_features(project_id)
        dependencies = service.validate(project_id, updates)
        snapshot = _json_safe({
            "updates": updates,
            "current": current,
            "dependencies": dependencies,
        })
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if digest != evaluation.inputs_hash:
            evaluation.status = "stale"
            self.db.commit()
            raise DecisionGateError("Engine configuration changed after approval")
        return choice

    def reconcile_run(self, run) -> None:
        if not run.decision_choice_id: return
        existing = self.db.query(DecisionGateOutcome).filter(DecisionGateOutcome.project_id == run.project_id, DecisionGateOutcome.choice_id == run.decision_choice_id).first()
        outcome = {"run_id": run.id, "status": run.status, "planned": run.planned_count, "sent": run.sent_count, "delivered": run.delivered_count, "failed": run.failed_count, "skipped": run.skipped_count}
        if existing: existing.outcome = outcome; existing.reconciled_at = self.clock.utcnow()
        else: self.db.add(DecisionGateOutcome(project_id=run.project_id, choice_id=run.decision_choice_id, outcome=outcome, reconciled_at=self.clock.utcnow()))
        self.db.flush()
