"""Campaign definitions, audience snapshots, plans and durable runs."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Project, SendLog
from app.models.campaigns import (
    Campaign,
    CampaignAction,
    CampaignRecipient,
    CampaignRun,
    CampaignVariant,
    CampaignWave,
    ChannelCapacityReservation,
    ChannelDeliveryProfile,
)
from app.models.messaging import MessagingUser
from app.services.campaigns.alerts import upsert_operational_alert
from app.services.campaigns.capacity import CampaignCapacityPlanner, CapacityPlanError
from app.services.campaigns.channel_registry import CampaignChannelRegistry
from app.services.campaigns.eligibility import (
    CampaignEligibilityService,
    CampaignPolicyError,
    ContactEligibility,
    validate_policy,
)
from app.services.campaigns.recurrence import RecurrenceError, next_occurrence, validate_recurrence
from app.services.campaigns.recurrence import as_utc_naive, parse_datetime
from app.services.campaigns.policy_overrides import (
    create_override,
    normalize_override_request,
)
from app.services.contact_groups.compiler import compile_group_query
from app.services.contact_groups.service import ContactGroupService


class CampaignError(ValueError):
    pass


class AudienceChangedError(CampaignError):
    def __init__(self, actual: dict[str, int]):
        super().__init__("Audience changed after preview; confirm the new totals")
        self.actual = actual


class CampaignService:
    def __init__(self, db: Session, clock=None):
        self.db = db
        from app.services.clock import SystemClock
        self.clock = clock or SystemClock()
        self.groups = ContactGroupService(db)
        self.eligibility = CampaignEligibilityService(db)
        self.capacity = CampaignCapacityPlanner(db, clock=self.clock)

    def get(self, project_id: int, campaign_id: int) -> Campaign | None:
        return self.db.query(Campaign).filter(
            Campaign.id == campaign_id,
            Campaign.project_id == project_id,
        ).first()

    def list(self, project_id: int, include_archived: bool = False) -> list[Campaign]:
        query = self.db.query(Campaign).filter(Campaign.project_id == project_id)
        if not include_archived:
            query = query.filter(Campaign.status != "archived")
        return query.order_by(Campaign.updated_at.desc(), Campaign.id.desc()).all()

    def create(
        self,
        project_id: int,
        values: dict[str, Any],
        created_by_user_id: int | None,
        *,
        commit: bool = True,
    ) -> Campaign:
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise CampaignError("Project not found")
        if values.get("status") not in {None, "draft"}:
            raise CampaignError("Create campaigns as draft; use the impact-gated activation endpoint")
        policy = validate_policy(values.get("policy_config"))
        policy = self._record_risk_acknowledgement(policy, created_by_user_id)
        campaign_type = str(values.get("campaign_type") or "one_off")
        if campaign_type not in {"one_off", "recurring", "automation"}:
            raise CampaignError("campaign_type must be one_off, recurring or automation")
        recurrence = values.get("recurrence_config")
        if campaign_type == "recurring":
            recurrence = validate_recurrence(recurrence)
        campaign = Campaign(
            project_id=project_id,
            name=str(values["name"]).strip(),
            description=values.get("description"),
            campaign_type=campaign_type,
            status=values.get("status") or "draft",
            default_channel=values.get("default_channel") or "email",
            selection_config=values.get("selection_config") or {},
            policy_config=policy,
            recurrence_config=recurrence,
            opportunity_config=values.get("opportunity_config"),
            timezone=values.get("timezone") or project.default_timezone,
            starts_at=values.get("starts_at"),
            target_at=values.get("target_at"),
            external_key=values.get("external_key"),
            purpose_key=values.get("purpose_key"),
            attention_policy=values.get("attention_policy"),
            created_by_user_id=created_by_user_id,
        )
        if not campaign.name:
            raise CampaignError("name is required")
        self.db.add(campaign)
        self.db.flush()
        self._replace_actions(campaign, values.get("actions") or [])
        self._validate_campaign(campaign)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        self.db.refresh(campaign)
        return campaign

    def update(
        self,
        campaign: Campaign,
        values: dict[str, Any],
        updated_by_user_id: int | None = None,
        *,
        commit: bool = True,
    ) -> Campaign:
        if campaign.status not in {"draft", "paused"}:
            raise CampaignError("Only draft or paused campaigns can be edited")
        if values.get("status") not in {None, campaign.status}:
            raise CampaignError("Campaign state changes must use activate, pause or archive endpoints")
        active_run = self.db.query(CampaignRun.id).filter(
            CampaignRun.project_id == campaign.project_id,
            CampaignRun.campaign_id == campaign.id,
            CampaignRun.status.in_(["scheduled", "running", "held", "cancel_requested"]),
        ).first()
        if active_run:
            raise CampaignError("Cancel active runs before editing campaign content")
        if "policy_config" in values:
            values["policy_config"] = validate_policy(values["policy_config"])
            values["policy_config"] = self._record_risk_acknowledgement(
                values["policy_config"], updated_by_user_id,
            )
        campaign_type = values.get("campaign_type", campaign.campaign_type)
        recurrence = values.get("recurrence_config", campaign.recurrence_config)
        if campaign_type == "recurring":
            values["recurrence_config"] = validate_recurrence(recurrence)
        for field in (
            "name", "description", "campaign_type", "status", "default_channel",
            "selection_config", "policy_config", "recurrence_config", "opportunity_config", "timezone",
            "starts_at", "target_at", "external_key",
            "purpose_key", "attention_policy",
        ):
            if field in values:
                setattr(campaign, field, values[field])
        if "actions" in values:
            self._replace_actions(campaign, values["actions"] or [])
        campaign.version = int(campaign.version or 1) + 1
        campaign.updated_at = datetime.utcnow()
        self._validate_campaign(campaign)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        self.db.refresh(campaign)
        return campaign

    def archive(self, campaign: Campaign) -> Campaign:
        active_run = self.db.query(CampaignRun.id).filter(
            CampaignRun.campaign_id == campaign.id,
            CampaignRun.project_id == campaign.project_id,
            CampaignRun.status.in_(["scheduled", "running", "held", "cancel_requested"]),
        ).first()
        if active_run:
            raise CampaignError("Cancel active runs before archiving this campaign")
        campaign.status = "archived"
        self.db.commit()
        self.db.refresh(campaign)
        return campaign

    def activate(
        self,
        campaign: Campaign,
        *,
        impact_fingerprint: str | None,
        commit: bool = True,
    ) -> tuple[Campaign, dict[str, Any]]:
        """Activate only after the shared, deterministic attention preflight."""
        self._validate_campaign(campaign)
        from app.services.orchestration_impact_service import OrchestrationImpactService

        impact = OrchestrationImpactService(self.db).ensure_approved(
            "campaign", campaign, impact_fingerprint,
        )
        campaign.status = "active"
        campaign.updated_at = datetime.utcnow()
        if commit:
            self.db.commit()
            self.db.refresh(campaign)
        else:
            self.db.flush()
        return campaign, impact

    def _replace_actions(self, campaign: Campaign, actions: list[dict[str, Any]]) -> None:
        # Runs retain foreign keys to the exact action/variant revision they
        # executed.  Archive old revisions instead of deleting them (which
        # would SET NULL on recipients and destroy the audit trail).
        for action in list(campaign.actions):
            if action.status != "archived":
                action.status = "archived"
            for variant in action.variants:
                variant.status = "archived"
        self.db.flush()
        for position, values in enumerate(actions):
            action_type = str(values.get("action_type") or "send_message")
            channel = str(values.get("channel") or campaign.default_channel or "email")
            if action_type != "send_message":
                raise CampaignError(f"Unsupported campaign action_type: {action_type}")
            if CampaignChannelRegistry.get(channel) is None:
                raise CampaignError(f"Campaign channel is not registered: {channel}")
            action = CampaignAction(
                project_id=campaign.project_id,
                campaign_id=campaign.id,
                position=int(values["position"] if values.get("position") is not None else position),
                action_type=action_type,
                channel=channel,
                config=values.get("config") or {},
                status=values.get("status") or "active",
            )
            self.db.add(action)
            self.db.flush()
            for variant_values in values.get("variants") or []:
                self.db.add(CampaignVariant(
                    project_id=campaign.project_id,
                    campaign_id=campaign.id,
                    action_id=action.id,
                    variant_key=str(variant_values.get("variant_key") or uuid.uuid4().hex[:12]),
                    locale=variant_values.get("locale"),
                    template_id=variant_values.get("template_id"),
                    subject=variant_values.get("subject"),
                    body=variant_values.get("body"),
                    variant_config=variant_values.get("variant_config") or {},
                    weight=int(variant_values.get("weight") or 100),
                    status=variant_values.get("status") or "active",
                ))
        self.db.flush()
        self.db.expire(campaign, ["actions", "variants"])

    def _validate_campaign(self, campaign: Campaign) -> None:
        if not campaign.name:
            raise CampaignError("name is required")
        if not campaign.actions:
            raise CampaignError("At least one campaign action is required")
        channels = {action.channel for action in campaign.actions if action.status == "active"}
        if len(channels) != 1:
            raise CampaignError("A v1 campaign run must use one channel across all active actions")
        if campaign.campaign_type == "recurring" and not campaign.starts_at:
            raise CampaignError("Recurring campaigns require starts_at as the recurrence anchor")
        selection = campaign.selection_config or {}
        if not selection.get("group_id") and not selection.get("rule_config"):
            raise CampaignError("selection_config requires group_id or rule_config")

    def _candidate_query(self, campaign: Campaign):
        selection = campaign.selection_config or {}
        group_id = selection.get("group_id")
        if group_id:
            group = self.groups.get(campaign.project_id, int(group_id))
            if not group or group.status != "active":
                raise CampaignError("Selected contact group is missing or inactive")
            return self.groups.selection_query(group), group
        return compile_group_query(
            self.db,
            campaign.project_id,
            selection.get("rule_config"),
            lifecycle_model_id=(
                int(selection["lifecycle_model_id"])
                if selection.get("lifecycle_model_id") is not None
                else None
            ),
        ), None

    def _snapshot(self, campaign: Campaign) -> tuple[list[ContactEligibility], list[CampaignAction], Any]:
        candidate_query, group = self._candidate_query(campaign)
        actions = [action for action in campaign.actions if action.status == "active"]
        if not actions:
            raise CampaignError("Campaign has no active actions")
        channel = actions[0].channel or campaign.default_channel
        eligibility = self.eligibility.evaluate(
            project_id=campaign.project_id,
            candidate_query=candidate_query,
            selection_config=campaign.selection_config,
            policy_config=campaign.policy_config,
            channel=channel,
        )
        return eligibility, actions, group

    def preview_plan(self, campaign: Campaign, schedule: dict[str, Any]) -> dict[str, Any]:
        self._validate_campaign(campaign)
        eligibility, actions, group = self._snapshot(campaign)
        reasons = Counter(item.reason for item in eligibility if item.reason)
        eligible_count = sum(1 for item in eligibility if item.eligible)
        planned_count = eligible_count * len(actions)
        channel = actions[0].channel or campaign.default_channel
        schedule = self._normalize_schedule(campaign, schedule)
        capacity_plan = self.capacity.preview(
            project_id=campaign.project_id,
            channel=channel,
            units=planned_count,
            schedule=schedule,
            policy_config=campaign.policy_config,
            now=self.clock.utcnow(),
        )
        return {
            "campaign_id": campaign.id,
            "campaign_version": campaign.version,
            "group_id": group.id if group else None,
            "group_last_evaluated_at": group.last_evaluated_at if group else None,
            "candidate_count": len(eligibility),
            "eligible_count": eligible_count,
            "action_count": len(actions),
            "planned_count": planned_count,
            "suppressed_count": len(eligibility) - eligible_count,
            "suppression_reasons": dict(sorted(reasons.items())),
            "capacity_plan": capacity_plan,
        }

    def create_run(
        self,
        campaign: Campaign,
        *,
        schedule: dict[str, Any],
        requested_by_user_id: int | None,
        trigger_type: str = "manual",
        expected: dict[str, int] | None = None,
        run_key: str | None = None,
        policy_override: dict[str, Any] | None = None,
        decision_choice_id: int | None = None,
    ) -> CampaignRun:
        if campaign.status != "active":
            raise CampaignError("Only active campaigns can create runs")
        from app.services.orchestration_cutover_service import OrchestrationCutoverService
        gate = OrchestrationCutoverService(self.db).execution_gate(
            campaign.project_id, campaign.purpose_key,
        )
        if not gate["allowed"]:
            raise CampaignError(
                f"Campaign purpose '{campaign.purpose_key}' is owned by {gate['mode']} "
                f"at orchestration epoch {gate['orchestration_epoch']}"
            )
        if trigger_type == "manual" and not str(run_key or "").strip():
            raise CampaignError("run_key is required for manual campaign runs")

        normalized_run_key = str(run_key or uuid.uuid4().hex).strip()
        decision_choice = None
        if decision_choice_id:
            from app.services.decision_gate_service import DecisionGateService
            decision_choice = DecisionGateService(self.db).choice_for_run(
                campaign.project_id, campaign, decision_choice_id,
            )
            policy_override = (decision_choice.consequence_snapshot or {}).get("override")
        normalized_override = normalize_override_request(policy_override)
        if normalized_override and trigger_type != "manual":
            raise CampaignError("Only manual campaign runs may request pacing overrides")
        request_fingerprint = hashlib.sha256(json.dumps(
            self._json_safe({
                "campaign_id": campaign.id,
                "campaign_version": int(campaign.version or 0),
                "trigger_type": trigger_type,
                "schedule": schedule or {},
                "expected": expected,
                "requested_by_user_id": requested_by_user_id,
                "policy_override": normalized_override,
                "decision_choice_id": decision_choice_id,
            }),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"campaign-run:{campaign.project_id}:{campaign.id}:{normalized_run_key}"},
            )
        existing_run = self.db.query(CampaignRun).filter(
            CampaignRun.project_id == campaign.project_id,
            CampaignRun.campaign_id == campaign.id,
            CampaignRun.run_key == normalized_run_key,
        ).first()
        if existing_run:
            existing_fingerprint = str(
                (existing_run.audience_snapshot or {}).get("run_request_fingerprint") or ""
            )
            if existing_fingerprint == request_fingerprint:
                return existing_run
            raise CampaignError("run_key was already used for a different campaign request")
        if decision_choice and decision_choice.execution_id:
            raise CampaignError("Decision choice was already used by another execution")
        self._validate_campaign(campaign)
        eligibility, actions, group = self._snapshot(campaign)
        eligible = [item for item in eligibility if item.eligible]
        actual = {
            "campaign_version": int(campaign.version or 0),
            "candidate_count": len(eligibility),
            "eligible_count": len(eligible),
            "planned_count": len(eligible) * len(actions),
        }
        if expected and any(int(expected.get(key, -1)) != value for key, value in actual.items()):
            raise AudienceChangedError(actual)
        totals = {key: actual[key] for key in ("candidate_count", "eligible_count", "planned_count")}
        schedule = self._normalize_schedule(campaign, schedule)
        channel = actions[0].channel or campaign.default_channel
        plan = self.capacity.preview(
            project_id=campaign.project_id,
            channel=channel,
            units=totals["planned_count"],
            schedule=schedule,
            policy_config=campaign.policy_config,
            now=self.clock.utcnow(),
        )
        if not plan.get("feasible"):
            raise CapacityPlanError(str(plan.get("reason") or "Campaign capacity is insufficient"))

        user_ids = sorted(item.user.id for item in eligibility)
        audience_hash = hashlib.sha256(
            f"{campaign.id}:{campaign.version}:".encode()
            + ",".join(str(user_id) for user_id in user_ids).encode()
        ).hexdigest()
        reasons = Counter(item.reason for item in eligibility if item.reason)
        snapshot = {
            "run_request_fingerprint": request_fingerprint,
            "campaign_version": campaign.version,
            "selection_config": campaign.selection_config,
            "policy_config": campaign.policy_config,
            "opportunity_config": campaign.opportunity_config,
            "group_id": group.id if group else None,
            "group_last_evaluated_at": group.last_evaluated_at.isoformat() if group and group.last_evaluated_at else None,
            "suppression_reasons": dict(sorted(reasons.items())),
        }
        run = CampaignRun(
            project_id=campaign.project_id,
            campaign_id=campaign.id,
            run_key=normalized_run_key,
            trigger_type=trigger_type,
            status="scheduled",
            audience_snapshot=snapshot,
            audience_hash=audience_hash,
            timezone=schedule.get("timezone") or campaign.timezone,
            starts_at=min((wave["scheduled_at"] for wave in plan["waves"]), default=None),
            target_at=campaign.target_at,
            deadline_at=schedule.get("deadline_at"),
            capacity_plan=self._json_safe(plan),
            candidate_count=totals["candidate_count"],
            eligible_count=totals["eligible_count"],
            planned_count=totals["planned_count"],
            queued_count=0,
            skipped_count=(totals["candidate_count"] - totals["eligible_count"]) * len(actions),
            requested_by_user_id=requested_by_user_id,
            decision_choice_id=decision_choice_id,
        )
        self.db.add(run)
        try:
            self.db.flush()
            if decision_choice:
                decision_choice.execution_type = "campaign_run"
                decision_choice.execution_id = str(run.id)
            create_override(
                self.db,
                run,
                normalized_override,
                requested_by_user_id,
            )
            waves = self.capacity.persist_plan(run, plan)
            self._create_recipients(run, eligibility, actions, waves, plan)
            if totals["planned_count"] == 0:
                run.status = "completed"
                run.finished_at = datetime.utcnow()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(run)
        return run

    def _create_recipients(
        self,
        run: CampaignRun,
        eligibility: list[ContactEligibility],
        actions: list[CampaignAction],
        waves: list[CampaignWave],
        plan: dict[str, Any],
    ) -> None:
        variants = self._variants_by_action(run.project_id, run.campaign_id)
        project = self.db.query(Project).filter(Project.id == run.project_id).first()
        default_locale = project.default_locale if project else None

        specs: list[tuple[ContactEligibility, CampaignAction, CampaignVariant | None]] = []
        from app.services.orchestration_impact_service import OrchestrationImpactService
        attention = OrchestrationImpactService(self.db)
        for item in eligibility:
            if not item.eligible:
                for action in actions:
                    self.db.add(CampaignRecipient(
                        project_id=run.project_id,
                        run_id=run.id,
                        action_id=action.id,
                        user_id=item.user.id,
                        endpoint_id=item.endpoint.id if item.endpoint else None,
                        channel=action.channel or run.campaign.default_channel,
                        endpoint_hash=item.endpoint_hash,
                        idempotency_key=f"{item.user.id}:{action.id}:suppressed",
                        status="skipped",
                        suppression_reason=item.reason,
                        completed_at=datetime.utcnow(),
                    ))
                continue
            entry = attention.apply_entry("campaign", run.campaign, item.user.id)
            if not entry["allowed"]:
                for action in actions:
                    self.db.add(CampaignRecipient(
                        project_id=run.project_id,
                        run_id=run.id,
                        action_id=action.id,
                        user_id=item.user.id,
                        endpoint_id=item.endpoint.id if item.endpoint else None,
                        channel=action.channel or run.campaign.default_channel,
                        endpoint_hash=item.endpoint_hash,
                        idempotency_key=f"{item.user.id}:{action.id}:attention-rejected",
                        status="skipped",
                        suppression_reason=str(entry.get("reason") or "attention_entry_rejected")[:120],
                        completed_at=datetime.utcnow(),
                    ))
                continue
            for action in actions:
                variant = self._select_variant(
                    variants.get(action.id, []),
                    item.user.locale,
                    default_locale,
                    f"{run.run_key}:{item.user.id}:{action.id}",
                )
                specs.append((item, action, variant))

        specs.sort(key=lambda spec: hashlib.sha256(
            f"{run.run_key}:{spec[0].user.id}:{spec[1].id}".encode()
        ).digest())
        cursor = 0
        wave_by_position = {wave.position: wave for wave in waves}
        for wave_plan in plan["waves"]:
            wave = wave_by_position[wave_plan["position"]]
            wave_total = 0
            for allocation in wave_plan["allocations"]:
                count = int(allocation["count"])
                selected_specs = specs[cursor:cursor + count]
                cursor += count
                wave_total += len(selected_specs)
                start = allocation["window_start"]
                end = allocation["window_end"]
                span_seconds = max(0, int((end - start).total_seconds()) - 1)
                for index, (item, action, variant) in enumerate(selected_specs):
                    offset = math.floor(span_seconds * index / max(1, count))
                    status = "held" if wave.status == "held" else "pending"
                    self.db.add(CampaignRecipient(
                        project_id=run.project_id,
                        run_id=run.id,
                        wave_id=wave.id,
                        action_id=action.id,
                        variant_id=variant.id if variant else None,
                        user_id=item.user.id,
                        endpoint_id=item.endpoint.id if item.endpoint else None,
                        channel=action.channel or run.campaign.default_channel,
                        endpoint_hash=item.endpoint_hash,
                        idempotency_key=f"{item.user.id}:{action.id}",
                        status=status,
                        provider=allocation["provider"],
                        delivery_profile_id=allocation["delivery_profile_id"],
                        sender_identity_id=allocation.get("sender_identity_id"),
                        scheduled_at=start + timedelta(seconds=offset),
                        queued_at=datetime.utcnow(),
                    ))
            wave.queued_count = wave_total
        if cursor != len(specs):
            raise CampaignError("Capacity plan did not assign every eligible campaign recipient")
        run.queued_count = len(specs)
        self.db.flush()

    def _variants_by_action(
        self,
        project_id: int,
        campaign_id: int,
    ) -> dict[int, list[CampaignVariant]]:
        rows = self.db.query(CampaignVariant).filter(
            CampaignVariant.project_id == project_id,
            CampaignVariant.campaign_id == campaign_id,
            CampaignVariant.status == "active",
        ).order_by(CampaignVariant.id).all()
        result: dict[int, list[CampaignVariant]] = defaultdict(list)
        for row in rows:
            if row.action_id:
                result[row.action_id].append(row)
        return result

    @staticmethod
    def _select_variant(
        variants: list[CampaignVariant],
        locale: str | None,
        default_locale: str | None,
        seed: str,
    ) -> CampaignVariant | None:
        if not variants:
            return None
        candidates = [variant for variant in variants if variant.locale == locale]
        if not candidates and default_locale:
            candidates = [variant for variant in variants if variant.locale == default_locale]
        if not candidates:
            candidates = [variant for variant in variants if variant.locale is None]
        if not candidates:
            return None
        total = sum(max(1, variant.weight or 1) for variant in candidates)
        point = int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16) % total
        cursor = 0
        for variant in candidates:
            cursor += max(1, variant.weight or 1)
            if point < cursor:
                return variant
        return candidates[-1]

    def cancel_run(self, run: CampaignRun) -> CampaignRun:
        # Serialize cancellation with CampaignWorker's final pre-provider gate.
        run = self.db.query(CampaignRun).filter(
            CampaignRun.id == run.id,
            CampaignRun.project_id == run.project_id,
        ).with_for_update().one()
        if run.status in {"completed", "canceled", "failed"}:
            self.db.commit()
            self.db.refresh(run)
            return run
        now = datetime.utcnow()
        run.cancel_requested = True
        recipient_ids = select(CampaignRecipient.id).where(
            CampaignRecipient.project_id == run.project_id,
            CampaignRecipient.run_id == run.id,
        )
        # Cancel every durable provider-pending attempt in the run, including
        # recipients already claimed as processing.  Filtering by source_id
        # also catches a log committed immediately before recipient linkage.
        self.db.query(SendLog).filter(
            SendLog.project_id == run.project_id,
            SendLog.source_type == "campaign",
            SendLog.source_id.in_(recipient_ids),
            SendLog.status.in_(["queued", "candidate", "delayed", "deferred", "submitting"]),
        ).update({
            SendLog.status: "canceled",
            SendLog.error_message: "Campaign run canceled",
            SendLog.failed_at: now,
        }, synchronize_session=False)
        canceled = self.db.query(CampaignRecipient).filter(
            CampaignRecipient.project_id == run.project_id,
            CampaignRecipient.run_id == run.id,
            CampaignRecipient.status.in_(["pending", "held", "deferred"]),
        ).update({
            CampaignRecipient.status: "canceled",
            CampaignRecipient.suppression_reason: "run_canceled",
            CampaignRecipient.completed_at: now,
        }, synchronize_session=False)
        run.canceled_count = int(run.canceled_count or 0) + canceled
        in_flight = self.db.query(CampaignRecipient.id).filter(
            CampaignRecipient.project_id == run.project_id,
            CampaignRecipient.run_id == run.id,
            CampaignRecipient.status == "processing",
        ).first()
        run.status = "cancel_requested" if in_flight else "canceled"
        if not in_flight:
            run.finished_at = now
        # A processing recipient may already be crossing the provider
        # boundary. Keep reservations intact until every in-flight claim is
        # terminal; _refresh_run releases unused capacity at that point.
        if not in_flight:
            reservations = self.db.query(ChannelCapacityReservation).filter(
                ChannelCapacityReservation.project_id == run.project_id,
                ChannelCapacityReservation.run_id == run.id,
                ChannelCapacityReservation.status.in_(["reserved", "active", "consumed"]),
            ).all()
            for reservation in reservations:
                if reservation.units_consumed:
                    reservation.units_reserved = reservation.units_consumed
                    reservation.status = "consumed"
                else:
                    reservation.status = "released"
                    reservation.released_at = now
        self.db.commit()
        self.db.refresh(run)
        return run

    def approve_wave(self, wave: CampaignWave, approved_by_user_id: int | None) -> CampaignWave:
        if wave.status != "held":
            raise CampaignError("Only held waves can be approved")
        wave.status = "planned"
        wave.approved_at = datetime.utcnow()
        wave.approved_by_user_id = approved_by_user_id
        self.db.query(CampaignRecipient).filter(
            CampaignRecipient.wave_id == wave.id,
            CampaignRecipient.status == "held",
        ).update({CampaignRecipient.status: "pending"}, synchronize_session=False)
        self.db.commit()
        self.db.refresh(wave)
        return wave

    def schedule_due_recurrences(self, now: datetime | None = None) -> int:
        now = now or datetime.utcnow()
        campaigns = self.db.query(Campaign).filter(
            Campaign.campaign_type == "recurring",
            Campaign.status == "active",
        ).all()
        created = 0
        for campaign in campaigns:
            try:
                config = validate_recurrence(campaign.recurrence_config)
                lead_days = max(0, int(config.get("planning_lead_days") or 0))
                last_run = self.db.query(CampaignRun).filter(
                    CampaignRun.project_id == campaign.project_id,
                    CampaignRun.campaign_id == campaign.id,
                    CampaignRun.trigger_type == "recurrence",
                ).order_by(CampaignRun.target_at.desc().nullslast(), CampaignRun.id.desc()).first()
                after = (
                    last_run.target_at
                    if last_run and last_run.target_at
                    else now - timedelta(days=lead_days, seconds=1)
                )
                occurrence = next_occurrence(
                    anchor=campaign.starts_at,
                    after=after,
                    config=config,
                    timezone_name=campaign.timezone or "UTC",
                )
                if occurrence is None or occurrence - timedelta(days=lead_days) > now:
                    continue
                if occurrence < now and not config.get("catch_up", False):
                    occurrence = next_occurrence(
                        anchor=campaign.starts_at,
                        after=now,
                        config=config,
                        timezone_name=campaign.timezone or "UTC",
                    )
                    if occurrence is None or occurrence - timedelta(days=lead_days) > now:
                        continue
                run_key = f"recurrence:{occurrence.isoformat()}"
                already = self.db.query(CampaignRun.id).filter(
                    CampaignRun.project_id == campaign.project_id,
                    CampaignRun.campaign_id == campaign.id,
                    CampaignRun.run_key == run_key,
                ).first()
                if already:
                    continue
                schedule_config = dict(config.get("schedule") or {})
                mode = schedule_config.get("mode") or "start_forward"
                if mode == "send_by_deadline":
                    schedule_config.update({
                        "start_at": occurrence - timedelta(days=lead_days),
                        "deadline_at": occurrence,
                    })
                else:
                    schedule_config["start_at"] = occurrence
                run = self.create_run(
                    campaign,
                    schedule=schedule_config,
                    requested_by_user_id=None,
                    trigger_type="recurrence",
                    run_key=run_key,
                )
                run.target_at = occurrence
                self.db.commit()
                created += 1
            except Exception as exc:
                self.db.rollback()
                upsert_operational_alert(
                    self.db,
                    project_id=campaign.project_id,
                    dedupe_key=f"campaign-recurrence:{campaign.id}",
                    alert_type="campaign_recurrence_failed",
                    title=f"Recurring campaign {campaign.name} could not be planned",
                    message=str(exc),
                    severity="error",
                    context={"campaign_id": campaign.id},
                )
                self.db.commit()
        return created

    @staticmethod
    def _normalize_schedule(campaign: Campaign, schedule: dict[str, Any] | None) -> dict[str, Any]:
        schedule = dict(schedule or {})
        schedule.setdefault("mode", "send_by_deadline" if campaign.target_at else "start_forward")
        schedule.setdefault("timezone", campaign.timezone or "UTC")
        if campaign.starts_at:
            schedule.setdefault("start_at", campaign.starts_at)
        else:
            schedule.setdefault("start_at", datetime.utcnow())
        if campaign.target_at:
            schedule.setdefault("deadline_at", campaign.target_at)
        for field in ("start_at", "deadline_at"):
            if schedule.get(field) is not None:
                schedule[field] = as_utc_naive(parse_datetime(schedule[field], field=field))
        if schedule.get("mode") == "exact_waves":
            normalized_waves = []
            for wave in schedule.get("waves") or []:
                wave = dict(wave)
                if wave.get("scheduled_at") is not None:
                    wave["scheduled_at"] = as_utc_naive(
                        parse_datetime(wave["scheduled_at"], field="scheduled_at")
                    )
                normalized_waves.append(wave)
            schedule["waves"] = normalized_waves
        return schedule

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: CampaignService._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [CampaignService._json_safe(item) for item in value]
        return value

    @staticmethod
    def _record_risk_acknowledgement(
        policy: dict[str, Any],
        actor_user_id: int | None,
    ) -> dict[str, Any]:
        if policy.get("permission_mode") != "subscribed_no_optout":
            return policy
        policy = dict(policy)
        acknowledgement = dict(policy.get("risk_acknowledgement") or {})
        acknowledgement["acknowledged_by_user_id"] = actor_user_id
        acknowledgement["acknowledged_at"] = datetime.utcnow().isoformat()
        policy["risk_acknowledgement"] = acknowledgement
        return policy
