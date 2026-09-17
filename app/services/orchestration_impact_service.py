"""Deterministic activation and episode-entry consequence analysis."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.models.campaigns import Campaign, CampaignRecipient, CampaignRun
from app.models.messaging import MessagingTemplate
from app.schemas.orchestration_attention import AttentionPolicyInput


class OrchestrationImpactError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__("Orchestration impact has unresolved or unsupported effects")


@dataclass(frozen=True)
class _Asset:
    kind: str
    id: int | None
    project_id: int
    name: str
    active: bool
    status: str
    purpose_key: str | None
    trigger_signature: str | None
    definition_version: str
    policy: dict[str, Any] | None
    policy_error: str | None
    event_name: str | None = None
    event_conditions: tuple[dict[str, Any], ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.id if self.id is not None else 'candidate'}"


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        values = [_canonical(item) for item in value]
        return sorted(
            values,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
        )
    return value


def _signature(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), default=str)


def _parse_policy(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not raw:
        return None, None
    try:
        return AttentionPolicyInput(**raw).model_dump(mode="json"), None
    except Exception as exc:
        return None, str(exc)


def _trigger_signature(kind: str, row: Any) -> str | None:
    if kind == "event_action":
        return f"event:{row.trigger_event}:{_signature(row.conditions or [])}"
    if kind == "funnel":
        config = row.trigger_config or {}
        if row.trigger_type == "event":
            event = config.get("event_name") or config.get("event")
            return f"event:{event}:{_signature(config.get('conditions') or [])}" if event else None
        return f"segment:{_signature(config)}"
    if kind == "campaign":
        return f"audience:{_signature(row.selection_config or {})}"
    if kind == "template":
        events = sorted(set(row.trigger_events or []))
        return f"events:{_signature(events)}" if events else None
    return None


def _definition_version(row: Any) -> str:
    version = getattr(row, "version", None)
    if version is not None:
        return str(version)
    updated = getattr(row, "updated_at", None)
    return updated.isoformat() if updated else "0"


def _asset(kind: str, row: Any) -> _Asset:
    policy, policy_error = _parse_policy(getattr(row, "attention_policy", None))
    if kind == "event_action":
        active = bool(row.is_active)
        status = "active" if active else "disabled"
    elif kind == "template":
        active = bool(row.automation_enabled)
        status = "active" if active else "draft"
    else:
        status = str(getattr(row, "status", "draft"))
        active = status == "active"
    return _Asset(
        kind=kind,
        id=getattr(row, "id", None),
        project_id=int(row.project_id),
        name=str(getattr(row, "name", None) or f"{kind} candidate"),
        active=active,
        status=status,
        purpose_key=getattr(row, "purpose_key", None),
        trigger_signature=_trigger_signature(kind, row),
        definition_version=_definition_version(row),
        policy=policy,
        policy_error=policy_error,
        event_name=(str(row.trigger_event) if kind == "event_action" else None),
        event_conditions=(
            tuple(dict(item) for item in (row.conditions or []) if isinstance(item, dict))
            if kind == "event_action"
            else ()
        ),
    )


def _scope(asset: _Asset) -> str | None:
    return (asset.policy or {}).get("attention_scope")


def _ordinal(asset: _Asset) -> int | None:
    value = (asset.policy or {}).get("ordinal")
    return int(value) if value is not None else None


def _targeted(candidate: _Asset, existing: _Asset) -> bool:
    policy = candidate.policy or {}
    existing_policy = existing.policy or {}
    candidate_group = policy.get("exclusive_group")
    existing_group = existing_policy.get("exclusive_group")
    if candidate_group and candidate_group == existing_group:
        return True
    if existing.purpose_key and existing.purpose_key in set(policy.get("target_purpose_keys") or []):
        return True
    if existing_group and existing_group in set(policy.get("target_exclusive_groups") or []):
        return True
    return False


def _condition_value(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), default=str)


def _event_condition_constraints(
    conditions: tuple[dict[str, Any], ...],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return only constraints whose disjointness can be proven under AND semantics."""
    allowed: dict[str, set[str]] = {}
    excluded: dict[str, set[str]] = {}
    for condition in conditions:
        field = str(condition.get("field") or "").strip()
        operator = str(condition.get("operator") or "").strip().lower()
        if not field:
            continue
        raw_value = condition.get("value")
        if operator in {"==", "=", "eq", "equals"}:
            values = {_condition_value(raw_value)}
            allowed[field] = allowed[field] & values if field in allowed else values
        elif operator == "in" and isinstance(raw_value, (list, tuple, set)):
            values = {_condition_value(value) for value in raw_value}
            allowed[field] = allowed[field] & values if field in allowed else values
        elif operator in {"!=", "<>", "neq", "not_equals"}:
            excluded.setdefault(field, set()).add(_condition_value(raw_value))
        elif operator == "not_in" and isinstance(raw_value, (list, tuple, set)):
            excluded.setdefault(field, set()).update(
                _condition_value(value) for value in raw_value
            )
    return allowed, excluded


def _event_actions_proven_disjoint(candidate: _Asset, existing: _Asset) -> bool:
    """Prove two variants cannot match the same event; uncertainty stays blocking."""
    if (
        candidate.kind != "event_action"
        or existing.kind != "event_action"
        or not candidate.event_name
        or candidate.event_name != existing.event_name
    ):
        return False
    candidate_allowed, candidate_excluded = _event_condition_constraints(
        candidate.event_conditions
    )
    existing_allowed, existing_excluded = _event_condition_constraints(
        existing.event_conditions
    )
    for field in set(candidate_allowed) | set(existing_allowed):
        candidate_values = candidate_allowed.get(field)
        existing_values = existing_allowed.get(field)
        if (
            candidate_values is not None
            and existing_values is not None
            and candidate_values.isdisjoint(existing_values)
        ):
            return True
        if (
            candidate_values
            and candidate_values.issubset(existing_excluded.get(field, set()))
        ):
            return True
        if (
            existing_values
            and existing_values.issubset(candidate_excluded.get(field, set()))
        ):
            return True
    return False


def _overlap_certainty(candidate: _Asset, existing: _Asset) -> str:
    if _event_actions_proven_disjoint(candidate, existing):
        return "proven_disjoint_event_conditions"
    cp, ep = candidate.policy or {}, existing.policy or {}
    if cp.get("exclusive_group") and cp.get("exclusive_group") == ep.get("exclusive_group"):
        return "definite_exclusive_group"
    if candidate.purpose_key and candidate.purpose_key == existing.purpose_key:
        return "definite_same_purpose"
    if candidate.trigger_signature and candidate.trigger_signature == existing.trigger_signature:
        return "likely_same_trigger_or_audience"
    return "possible_same_attention_scope"


def _relationship(candidate: _Asset, existing: _Asset) -> dict[str, str]:
    """Pure attention/lifecycle decision for one pair of active episodes.

    Attention order and episode lifecycle are intentionally separate.  A
    higher ordinal may own the next attention slot while the lower episode
    remains alive; only an explicit, compatible entry effect may destroy or
    suspend it.
    """
    policy = candidate.policy or {}
    existing_policy = existing.policy or {}
    targeted = _targeted(candidate, existing)
    targeted_by_existing = _targeted(existing, candidate)
    declared_effect = str(policy.get("entry_effect") or "")
    if targeted and declared_effect == "reject_entry":
        return {
            "attention_decision": "entry_rejected",
            "entry_action": "reject_entry",
            "existing_episode_effect": "preserve",
            "reason": "declared_incompatible_episode_active",
        }
    if targeted_by_existing and existing_policy.get("entry_effect") == "reject_entry":
        return {
            "attention_decision": "entry_rejected",
            "entry_action": "reject_entry",
            "existing_episode_effect": "preserve",
            "reason": "existing_episode_rejects_candidate",
        }

    allowed_incoming = set(existing_policy.get("allowed_incoming_effects") or [])
    if _scope(candidate) != _scope(existing):
        requested_effect = declared_effect if targeted and declared_effect in {"exit", "suspend"} else "coexist"
        if requested_effect not in allowed_incoming:
            return {
                "attention_decision": "entry_rejected",
                "entry_action": "reject_entry",
                "existing_episode_effect": "preserve",
                "reason": f"existing_episode_disallows_{requested_effect}",
            }
        return {
            "attention_decision": "independent_scope",
            "entry_action": requested_effect,
            "existing_episode_effect": requested_effect if requested_effect in {"exit", "suspend"} else "preserve",
            "reason": (
                "declared_cross_scope_incompatibility"
                if requested_effect in {"exit", "suspend"}
                else "different_attention_scope"
            ),
        }

    candidate_ordinal, existing_ordinal = _ordinal(candidate), _ordinal(existing)
    if candidate_ordinal is None or existing_ordinal is None:
        return {
            "attention_decision": "unresolved",
            "entry_action": "unresolved",
            "existing_episode_effect": "preserve",
            "reason": "attention_ordinal_missing",
        }
    if candidate_ordinal == existing_ordinal:
        tie_policy = str(policy.get("tie_policy") or "require_order")
        existing_tie_policy = str(existing_policy.get("tie_policy") or "require_order")
        if tie_policy == "require_order" or existing_tie_policy == "require_order":
            return {
                "attention_decision": "unresolved_tie",
                "entry_action": "unresolved",
                "existing_episode_effect": "preserve",
                "reason": "equal_ordinal_requires_order",
            }
        if tie_policy != existing_tie_policy:
            return {
                "attention_decision": "unresolved_tie",
                "entry_action": "unresolved",
                "existing_episode_effect": "preserve",
                "reason": "equal_ordinal_tie_policies_disagree",
            }
        return {
            "attention_decision": "delegated_tie",
            "entry_action": "tie",
            "existing_episode_effect": "preserve",
            "reason": f"delegated_to_{tie_policy}",
        }
    if candidate_ordinal < existing_ordinal:
        if targeted and declared_effect in {"exit", "suspend"}:
            return {
                "attention_decision": "candidate_occluded",
                "entry_action": "unresolved",
                "existing_episode_effect": "preserve",
                "reason": "destructive_entry_effect_cannot_displace_higher_ordinal",
            }
        if not bool(policy.get("allow_start_occluded", True)):
            return {
                "attention_decision": "entry_rejected",
                "entry_action": "reject_entry",
                "existing_episode_effect": "preserve",
                "reason": "candidate_disallows_occluded_start",
            }
        return {
            "attention_decision": "candidate_occluded",
            "entry_action": "candidate_occluded",
            "existing_episode_effect": "preserve",
            "reason": "existing_ordinal_is_higher",
        }
    if targeted and declared_effect in {"exit", "suspend"}:
        if declared_effect not in allowed_incoming:
            return {
                "attention_decision": "entry_rejected",
                "entry_action": "reject_entry",
                "existing_episode_effect": "preserve",
                "reason": f"existing_episode_disallows_{declared_effect}",
            }
        return {
            "attention_decision": "candidate_selected",
            "entry_action": declared_effect,
            "existing_episode_effect": declared_effect,
            "reason": "declared_incompatible_episode",
        }
    if "occlude" not in allowed_incoming:
        return {
            "attention_decision": "entry_rejected",
            "entry_action": "reject_entry",
            "existing_episode_effect": "preserve",
            "reason": "existing_episode_disallows_occlude",
        }
    return {
        "attention_decision": "candidate_selected",
        "entry_action": "occlude",
        "existing_episode_effect": "preserve",
        "reason": (
            "explicit_coexistence_with_attention_arbitration"
            if declared_effect == "coexist"
            else "candidate_ordinal_is_higher"
        ),
    }


class OrchestrationImpactService:
    """One source of truth for preview, activation gates and runtime entry."""

    def __init__(self, db: Session):
        self.db = db

    def _assets(self, project_id: int) -> list[_Asset]:
        event_actions = [
            _asset("event_action", row)
            for row in self.db.query(models.EventAction).filter(
                models.EventAction.project_id == project_id,
            ).all()
        ]
        funnels = [
            _asset("funnel", row)
            for row in self.db.query(models.Funnel).filter(
                models.Funnel.project_id == project_id,
                models.Funnel.is_system == False,  # noqa: E712
            ).all()
        ]
        campaigns = [
            _asset("campaign", row)
            for row in self.db.query(Campaign).filter(Campaign.project_id == project_id).all()
        ]
        templates = [
            _asset("template", row)
            for row in self.db.query(MessagingTemplate).filter(
                MessagingTemplate.project_id == project_id,
            ).all()
        ]
        return [*event_actions, *funnels, *campaigns, *templates]

    def preview(self, kind: str, row: Any) -> dict[str, Any]:
        if kind not in {"event_action", "funnel", "campaign", "template"}:
            raise ValueError(f"Unsupported orchestration asset kind: {kind}")
        candidate = _asset(kind, row)
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        impacts: list[dict[str, Any]] = []
        modes: dict[str, str] = {}

        if candidate.policy_error:
            blockers.append({"code": "invalid_attention_policy", "detail": candidate.policy_error})
        if not candidate.policy:
            blockers.append({
                "code": "attention_policy_required",
                "detail": "Activation requires an explicit attention policy.",
            })
        if not candidate.purpose_key:
            blockers.append({
                "code": "purpose_key_required",
                "detail": "Activation requires a stable business purpose_key.",
            })
        promotional_candidate = (
            kind != "event_action"
            or str(getattr(row, "lane", "promotional")) == "promotional"
        )
        if promotional_candidate and self.db is not None:
            from app.services.engine_rollout_service import EngineRolloutService

            modes = EngineRolloutService(self.db).effective_modes(candidate.project_id)
            if modes.get("candidate") != "enforce" or modes.get("selection") != "enforce":
                blockers.append({
                    "code": "shared_attention_runtime_required",
                    "detail": (
                        "Promotional activation requires Candidate and Selection in enforce "
                        f"(candidate={modes.get('candidate')}, selection={modes.get('selection')})."
                    ),
                })
        if kind == "template":
            if not bool(getattr(row, "is_active", False)):
                blockers.append({
                    "code": "template_content_inactive",
                    "detail": "Template content must be active before its event automation can be enabled.",
                })
            if not getattr(row, "trigger_events", None):
                blockers.append({
                    "code": "trigger_events_required",
                    "detail": "Event template automation requires at least one trigger event.",
                })

        for existing in self._assets(int(row.project_id)):
            if existing.kind == candidate.kind and existing.id == candidate.id:
                continue
            if not existing.active:
                continue
            if not candidate.policy:
                impacts.append(self._impact(candidate, existing, "unresolved", "candidate_policy_missing"))
                continue
            if existing.policy_error or not existing.policy:
                blockers.append({
                    "code": "existing_attention_policy_required",
                    "asset_ref": existing.ref,
                    "asset_name": existing.name,
                    "detail": existing.policy_error or "Existing active asset has no explicit attention policy.",
                })
                impacts.append(self._impact(candidate, existing, "unresolved", "existing_policy_missing"))
                continue

            certainty = _overlap_certainty(candidate, existing)
            proven_disjoint = certainty == "proven_disjoint_event_conditions"
            relationship = (
                {
                    "attention_decision": "mutually_exclusive_variant",
                    "entry_action": "coexist",
                    "existing_episode_effect": "preserve",
                    "reason": "event_conditions_proven_disjoint",
                }
                if proven_disjoint
                else _relationship(candidate, existing)
            )
            action = relationship["entry_action"]
            if relationship["reason"] in {
                "equal_ordinal_requires_order",
                "equal_ordinal_tie_policies_disagree",
            }:
                blockers.append({
                    "code": "attention_order_required",
                    "asset_ref": existing.ref,
                    "asset_name": existing.name,
                    "detail": "Overlapping active assets have equal ordinals and require an explicit order.",
                })
            if relationship["reason"] == "destructive_entry_effect_cannot_displace_higher_ordinal":
                blockers.append({
                    "code": "contradictory_entry_effect",
                    "asset_ref": existing.ref,
                    "asset_name": existing.name,
                    "detail": "A lower-ordinal episode cannot exit or suspend the higher-ordinal episode it targets.",
                })
            if (
                candidate.purpose_key
                and candidate.purpose_key == existing.purpose_key
                and not proven_disjoint
                and action not in {"exit", "reject_entry"}
            ):
                blockers.append({
                    "code": "purpose_owner_overlap",
                    "asset_ref": existing.ref,
                    "asset_name": existing.name,
                    "purpose_key": candidate.purpose_key,
                    "detail": "One purpose_key cannot have two live decision owners; explicitly exit the old episode or reject entry.",
                })
            if action == "reject_entry":
                warnings.append({
                    "code": "entries_will_be_rejected",
                    "asset_ref": existing.ref,
                    "asset_name": existing.name,
                    "reason": relationship["reason"],
                    "detail": "Contacts already in this episode will not enter the candidate episode.",
                })

            impact = self._impact(
                candidate, existing, action, relationship["reason"],
                certainty=certainty,
                attention_decision=relationship["attention_decision"],
                existing_episode_effect=relationship["existing_episode_effect"],
            )
            impacts.append(impact)
            support = self._runtime_support(action, candidate, existing)
            impact["runtime_support"] = support
            if not support["supported"]:
                blockers.append({
                    "code": "attention_effect_not_supported",
                    "asset_ref": existing.ref,
                    "asset_name": existing.name,
                    "effect": action,
                    "detail": support["reason"],
                })

        if candidate.policy:
            if candidate.policy.get("occluded_clock") == "active_attention":
                blockers.append({
                    "code": "active_attention_clock_not_supported",
                    "detail": "Pausing funnel clocks while occluded is not implemented.",
                })
            if candidate.policy.get("future_reservation"):
                if not modes and self.db is not None:
                    from app.services.engine_rollout_service import EngineRolloutService

                    modes = EngineRolloutService(self.db).effective_modes(candidate.project_id)
                if modes.get("future_plan") != "enforce":
                    blockers.append({
                        "code": "future_attention_runtime_required",
                        "detail": (
                            "future_reservation=true requires Future Plan in enforce "
                            f"(future_plan={modes.get('future_plan', 'unavailable')})."
                        ),
                    })
            if candidate.policy.get("missed_window") != "expire":
                blockers.append({
                    "code": "missed_window_policy_not_supported",
                    "detail": "Only missed_window=expire is currently enforced across every orchestration source.",
                })

        blockers = self._dedupe(blockers)
        warnings = self._dedupe(warnings)
        materialized = self._materialized_impact(impacts)
        for item in materialized["per_asset"]:
            if item.get("entry_action") == "exit" and item.get("processing_recipients", 0):
                blockers.append({
                    "code": "campaign_submission_in_flight",
                    "asset_ref": item["asset_ref"],
                    "count": item["processing_recipients"],
                    "detail": "Wait for in-flight provider submissions to finish before approving an exit effect.",
                })
        blockers = self._dedupe(blockers)
        consequence_at_gate = self._consequence_at_gate(
            candidate,
            impacts,
            materialized,
            blockers,
            modes,
        )
        fingerprint_payload = {
            "candidate": self._public_asset(candidate),
            "impacts": impacts,
            "materialized": materialized,
            "blockers": blockers,
            "consequence_at_gate": consequence_at_gate,
        }
        fingerprint = hashlib.sha256(
            _signature(fingerprint_payload).encode("utf-8")
        ).hexdigest()
        return {
            "project_id": int(row.project_id),
            "candidate": self._public_asset(candidate),
            "safe_to_activate": not blockers,
            "requires_confirmation": True,
            "impact_fingerprint": fingerprint,
            "impacts": impacts,
            "materialized_impact": materialized,
            "blockers": blockers,
            "warnings": warnings,
            "consequence_at_gate": consequence_at_gate,
            "external_sends": 0,
        }

    def ensure_can_activate(self, kind: str, row: Any) -> dict[str, Any]:
        report = self.preview(kind, row)
        if not report["safe_to_activate"]:
            raise OrchestrationImpactError(report)
        return report

    def ensure_approved(
        self,
        kind: str,
        row: Any,
        impact_fingerprint: str | None,
    ) -> dict[str, Any]:
        """Fail if approval is absent or refers to any older impact graph."""
        report = self.ensure_can_activate(kind, row)
        if not impact_fingerprint or impact_fingerprint != report["impact_fingerprint"]:
            stale = dict(report)
            stale["safe_to_activate"] = False
            stale["blockers"] = [
                *report["blockers"],
                {
                    "code": "attention_impact_approval_required",
                    "detail": "Preview the current impact and approve its exact impact_fingerprint.",
                    "expected_impact_fingerprint": report["impact_fingerprint"],
                },
            ]
            raise OrchestrationImpactError(stale)
        return report

    def apply_entry(self, kind: str, row: Any, user_id: int) -> dict[str, Any]:
        """Apply lifecycle effects before creating a new per-contact episode.

        This method performs no provider I/O.  It locks materialized episodes,
        rejects unsupported/in-flight transitions, and lets the caller create
        the new enrollment/recipient in the same transaction.
        """
        candidate = _asset(kind, row)
        if candidate.policy_error:
            return {
                "allowed": False,
                "reason": "attention_policy_invalid",
                "exited": [],
                "canceled": [],
                "external_sends": 0,
            }
        if not candidate.policy:
            # Compatibility for episodes that were already active before the
            # contract existed. New activation is blocked by preview(), but a
            # deploy must not silently stop their existing event path.
            return {
                "allowed": True,
                "reason": "legacy_active_asset_without_attention_policy",
                "decisions": [],
                "exited": [],
                "canceled": [],
                "external_sends": 0,
            }

        active_funnels = self.db.query(models.FunnelEnrollment, models.Funnel).join(
            models.Funnel, models.Funnel.id == models.FunnelEnrollment.funnel_id,
        ).filter(
            models.Funnel.project_id == candidate.project_id,
            models.FunnelEnrollment.user_id == user_id,
            models.FunnelEnrollment.status == "active",
        ).with_for_update(of=models.FunnelEnrollment).all()

        active_campaigns = self.db.query(CampaignRecipient, Campaign).join(
            CampaignRun, CampaignRun.id == CampaignRecipient.run_id,
        ).join(
            Campaign, Campaign.id == CampaignRun.campaign_id,
        ).filter(
            Campaign.project_id == candidate.project_id,
            CampaignRecipient.user_id == user_id,
            CampaignRecipient.status.in_(["pending", "processing", "deferred", "held"]),
            CampaignRun.status.in_(["scheduled", "running", "held", "cancel_requested"]),
        ).with_for_update(of=CampaignRecipient).all()

        pending_event_actions = self.db.query(
            models.ScheduledEventAction, models.EventAction,
        ).join(
            models.EventAction,
            models.EventAction.id == models.ScheduledEventAction.event_action_id,
        ).filter(
            models.ScheduledEventAction.project_id == candidate.project_id,
            models.ScheduledEventAction.user_id == user_id,
            models.ScheduledEventAction.status == "pending",
        ).with_for_update(of=models.ScheduledEventAction).all()

        pending_templates = self.db.query(
            models.SendLog, MessagingTemplate,
        ).join(
            MessagingTemplate,
            MessagingTemplate.id == models.SendLog.source_id,
        ).filter(
            models.SendLog.project_id == candidate.project_id,
            models.SendLog.user_id == user_id,
            models.SendLog.source_type == "template",
            models.SendLog.status.in_(["candidate", "delayed", "deferred"]),
        ).with_for_update(of=models.SendLog).all()

        episodes: list[tuple[_Asset, str, Any]] = []
        for enrollment, funnel in active_funnels:
            if kind == "funnel" and funnel.id == getattr(row, "id", None):
                continue
            source_kind, source_row = "funnel", funnel
            if funnel.is_system and funnel.event_action:
                source_kind, source_row = "event_action", funnel.event_action
            episodes.append((_asset(source_kind, source_row), "funnel", enrollment))
        for recipient, campaign in active_campaigns:
            if kind == "campaign" and campaign.id == getattr(row, "id", None):
                continue
            episodes.append((_asset("campaign", campaign), "campaign", recipient))
        for scheduled, event_action in pending_event_actions:
            if kind == "event_action" and event_action.id == getattr(row, "id", None):
                continue
            episodes.append((_asset("event_action", event_action), "event_action", scheduled))
        for send_log, template in pending_templates:
            if kind == "template" and template.id == getattr(row, "id", None):
                continue
            episodes.append((_asset("template", template), "template_send", send_log))

        decisions: list[tuple[_Asset, str, Any, str]] = []
        for existing, materialized_kind, materialized in episodes:
            if not existing.policy or existing.policy_error:
                return {
                    "allowed": False,
                    "reason": "existing_attention_policy_missing_or_invalid",
                    "blocking_asset": existing.ref,
                    "exited": [],
                    "canceled": [],
                    "external_sends": 0,
                }
            relationship = _relationship(candidate, existing)
            action = relationship["entry_action"]
            decisions.append((existing, materialized_kind, materialized, action))
            support = self._runtime_support(action, candidate, existing)
            if not support["supported"]:
                return {
                    "allowed": False,
                    "reason": "entry_effect_unresolved_or_unsupported",
                    "blocking_asset": existing.ref,
                    "entry_action": action,
                    "runtime_support": support,
                    "exited": [],
                    "canceled": [],
                    "external_sends": 0,
                }
            if action == "reject_entry":
                return {
                    "allowed": False,
                    "reason": "incompatible_episode_active",
                    "blocking_asset": existing.ref,
                    "entry_action": action,
                    "exited": [],
                    "canceled": [],
                    "external_sends": 0,
                }
            if action == "exit" and materialized_kind == "campaign" and materialized.status == "processing":
                return {
                    "allowed": False,
                    "reason": "inflight_campaign_submission_cannot_be_exited_safely",
                    "blocking_asset": existing.ref,
                    "entry_action": action,
                    "exited": [],
                    "canceled": [],
                    "external_sends": 0,
                }

        exited: list[str] = []
        canceled: list[str] = []
        now = datetime.utcnow()
        for existing, materialized_kind, materialized, action in decisions:
            if action != "exit":
                continue
            if materialized_kind == "funnel":
                from app.services.funnel_engine import FunnelEngine

                FunnelEngine(self.db)._exit_enrollment(
                    materialized,
                    f"attention_policy_exit:{candidate.ref}",
                )
                exited.append(f"funnel_enrollment:{materialized.id}")
            elif materialized_kind == "campaign":
                recipient = materialized
                recipient.status = "canceled"
                recipient.suppression_reason = f"attention_policy_exit:{candidate.ref}"
                recipient.completed_at = now
                self.db.query(models.SendLog).filter(
                    models.SendLog.project_id == candidate.project_id,
                    models.SendLog.source_type == "campaign",
                    models.SendLog.source_id == recipient.id,
                    models.SendLog.status.in_(["queued", "candidate", "delayed", "deferred"]),
                ).update({
                    models.SendLog.status: "canceled",
                    models.SendLog.error_message: f"Superseded by {candidate.ref}",
                    models.SendLog.failed_at: now,
                }, synchronize_session=False)
                canceled.append(f"campaign_recipient:{recipient.id}")
            elif materialized_kind == "event_action":
                scheduled = materialized
                scheduled.status = "cancelled"
                scheduled.cancelled_at = now
                scheduled.cancel_reason = f"attention_policy_exit:{candidate.ref}"
                canceled.append(f"scheduled_event_action:{scheduled.id}")
            elif materialized_kind == "template_send":
                send_log = materialized
                send_log.status = "canceled"
                send_log.error_message = f"Superseded by {candidate.ref}"
                send_log.failed_at = now
                canceled.append(f"template_send:{send_log.id}")
        self.db.flush()
        return {
            "allowed": True,
            "reason": "attention_entry_policy_applied",
            "decisions": [
                {"asset_ref": existing.ref, "entry_action": action}
                for existing, _, _, action in decisions
            ],
            "exited": exited,
            "canceled": canceled,
            "external_sends": 0,
        }

    @staticmethod
    def _public_asset(asset: _Asset) -> dict[str, Any]:
        return {
            "kind": asset.kind,
            "id": asset.id,
            "project_id": asset.project_id,
            "ref": asset.ref,
            "name": asset.name,
            "status": asset.status,
            "purpose_key": asset.purpose_key,
            "trigger_signature": asset.trigger_signature,
            "definition_version": asset.definition_version,
            "attention_policy": asset.policy,
            "attention_policy_error": asset.policy_error,
        }

    @staticmethod
    def _impact(
        candidate: _Asset,
        existing: _Asset,
        action: str,
        reason: str,
        *,
        certainty: str = "possible_same_attention_scope",
        attention_decision: str | None = None,
        existing_episode_effect: str | None = None,
    ) -> dict[str, Any]:
        return {
            "existing": OrchestrationImpactService._public_asset(existing),
            "overlap_certainty": certainty,
            "entry_action": action,
            "attention_decision": attention_decision or (
                "independent_scope" if action == "coexist" else "unresolved"
            ),
            "existing_episode_effect": existing_episode_effect or "preserve",
            "reason": reason,
            "candidate_ordinal": _ordinal(candidate),
            "existing_ordinal": _ordinal(existing),
        }

    def _runtime_support(self, action: str, candidate: _Asset, existing: _Asset) -> dict[str, Any]:
        if action in {"coexist", "reject_entry", "exit"}:
            return {"supported": True, "reason": "deterministic_entry_policy"}
        if action == "suspend":
            return {"supported": False, "reason": "Enrollment/campaign clock suspension is not implemented."}
        if action == "tie":
            tie_policy = (candidate.policy or {}).get("tie_policy")
            if tie_policy != "perishability":
                return {"supported": False, "reason": "Bounded-learning tie execution is not enabled for all sources."}
        if action in {"candidate_occluded", "occlude", "tie"}:
            try:
                from app.services.engine_rollout_service import EngineRolloutService

                modes = EngineRolloutService(self.db).effective_modes(
                    candidate.project_id
                )
                supported = modes.get("selection") == "enforce" and modes.get("candidate") == "enforce"
                return {
                    "supported": supported,
                    "reason": (
                        "deterministic_perishability_tie_break"
                        if supported and action == "tie"
                        else "shared_selection_enforced"
                        if supported
                        else "Candidate and Selection must be enforce."
                    ),
                }
            except Exception:
                return {"supported": False, "reason": "Could not verify shared Selection rollout."}
        if action == "unresolved":
            return {"supported": False, "reason": "The relationship has no deterministic resolution."}
        return {"supported": False, "reason": f"Unsupported entry action: {action}"}

    def _materialized_impact(self, impacts: list[dict[str, Any]]) -> dict[str, Any]:
        affected = {item["existing"]["ref"]: item for item in impacts if item["entry_action"] not in {"coexist"}}
        active_enrollments = 0
        active_campaign_recipients = 0
        pending_event_actions = 0
        pending_template_sends = 0
        per_asset: list[dict[str, Any]] = []
        for ref, impact in affected.items():
            existing = impact["existing"]
            kind, asset_id = existing["kind"], existing["id"]
            counts: dict[str, int] = {}
            if kind == "funnel":
                counts["active_enrollments"] = self.db.query(models.FunnelEnrollment).filter(
                    models.FunnelEnrollment.funnel_id == asset_id,
                    models.FunnelEnrollment.status == "active",
                ).count()
                active_enrollments += counts["active_enrollments"]
            elif kind == "campaign":
                counts["active_runs"] = self.db.query(CampaignRun).filter(
                    CampaignRun.campaign_id == asset_id,
                    CampaignRun.status.in_(["scheduled", "running", "held", "cancel_requested"]),
                ).count()
                counts["active_recipients"] = self.db.query(CampaignRecipient).join(
                    CampaignRun, CampaignRun.id == CampaignRecipient.run_id,
                ).filter(
                    CampaignRun.campaign_id == asset_id,
                    CampaignRecipient.status.in_(["pending", "processing", "deferred", "held"]),
                ).count()
                counts["processing_recipients"] = self.db.query(CampaignRecipient).join(
                    CampaignRun, CampaignRun.id == CampaignRecipient.run_id,
                ).filter(
                    CampaignRun.campaign_id == asset_id,
                    CampaignRecipient.status == "processing",
                ).count()
                active_campaign_recipients += counts["active_recipients"]
            elif kind == "event_action":
                counts["pending_actions"] = self.db.query(models.ScheduledEventAction).filter(
                    models.ScheduledEventAction.event_action_id == asset_id,
                    models.ScheduledEventAction.status == "pending",
                ).count()
                pending_event_actions += counts["pending_actions"]
            elif kind == "template":
                counts["pending_sends"] = self.db.query(models.SendLog).filter(
                    models.SendLog.project_id == existing["project_id"],
                    models.SendLog.source_type == "template",
                    models.SendLog.source_id == asset_id,
                    models.SendLog.status.in_(["candidate", "delayed", "deferred"]),
                ).count()
                pending_template_sends += counts["pending_sends"]
            per_asset.append({"asset_ref": ref, "entry_action": impact["entry_action"], **counts})
        return {
            "active_funnel_enrollments": active_enrollments,
            "active_campaign_recipients": active_campaign_recipients,
            "pending_event_actions": pending_event_actions,
            "pending_template_sends": pending_template_sends,
            "per_asset": per_asset,
        }

    @staticmethod
    def _consequence_at_gate(
        candidate: _Asset,
        impacts: list[dict[str, Any]],
        materialized: dict[str, Any],
        blockers: list[dict[str, Any]],
        modes: dict[str, str],
    ) -> dict[str, Any]:
        """Translate authored mechanics into tenant-reviewable outcomes."""
        counts = {
            item["asset_ref"]: {
                key: value
                for key, value in item.items()
                if key not in {"asset_ref", "entry_action"}
            }
            for item in materialized.get("per_asset", [])
        }
        labels = {
            "coexist": "remains_active_independent",
            "occlude": "remains_active_but_loses_current_attention",
            "candidate_occluded": "new_entry_starts_occluded",
            "tie": "remains_active_and_uses_declared_tie_policy",
            "exit": "is_resolved_and_pending_intents_are_canceled",
            "suspend": "would_pause_declared_clocks_and_actions",
            "reject_entry": "prevents_the_new_entry",
            "unresolved": "blocks_activation_until_tenant_decides",
        }
        per_asset = []
        for impact in impacts:
            existing = impact["existing"]
            action = impact["entry_action"]
            per_asset.append({
                "asset_ref": existing["ref"],
                "asset_name": existing["name"],
                "entry_action": action,
                "attention_decision": impact.get("attention_decision"),
                "consequence": labels.get(action, "blocks_until_resolved"),
                "currently_materialized": counts.get(existing["ref"], {}),
                "reason": impact.get("reason"),
            })
        policy = candidate.policy or {}
        future_enabled = bool(policy.get("future_reservation"))
        return {
            "decision": "blocked" if blockers else "ready_for_exact_impact_confirmation",
            "tenant_authored_rule": {
                "attention_scope": policy.get("attention_scope"),
                "ordinal": policy.get("ordinal"),
                "ordinal_reason": policy.get("ordinal_reason"),
                "entry_effect": policy.get("entry_effect"),
                "future_reservation": future_enabled,
            },
            "per_asset": per_asset,
            "future_attention": {
                "mode": modes.get("future_plan", "unavailable"),
                "consequence": (
                    "Known higher-ranked future intents may hold lower due intents inside the tenant window."
                    if future_enabled and modes.get("future_plan") == "enforce"
                    else "The candidate cannot hold attention before it is due."
                ),
                "binding_boundary": "recomputed_under_contact_scope_lock_before_dispatch",
            },
            "blocker_codes": [item.get("code") for item in blockers],
            "external_sends": 0,
        }

    @staticmethod
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            key = _signature(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result
