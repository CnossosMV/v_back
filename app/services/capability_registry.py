"""Typed capability boundary shared by agents, MCP and operator APIs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models import ProjectPolicy
from app.models.campaigns import Campaign, CampaignRun
from app.models.engine_control import CapabilityExecution, DecisionGateChoice, DecisionGateEvaluation, DecisionGateOutcome
from app.models.project_import import LifecycleModel, ProjectImport, ProjectLifecycleCutover
from app.services.decision_gate_service import DecisionGateService
from app.services.engine_rollout_service import EngineRolloutService
from app.services.provider_state_service import ProviderStateService


@dataclass(frozen=True)
class Capability:
    key: str
    phases: tuple[str, ...]
    effect: str
    approval_required: bool
    rbac: dict[str, str]
    input_schema: dict
    output_schema: dict


CAPABILITIES = {
    item.key: item for item in (
        Capability("engine.inspect", ("inspect",), "read", False, {"inspect": "viewer"}, {"type": "object"}, {"type": "object"}),
        Capability("policies.inspect", ("inspect",), "read", False, {"inspect": "viewer"}, {"type": "object"}, {"type": "object"}),
        Capability("providers.inspect", ("inspect",), "read", False, {"inspect": "viewer"}, {"type": "object"}, {"type": "object", "required": ["providers"]}),
        Capability("campaign.outcome", ("inspect",), "read", False, {"inspect": "viewer"}, {"type": "object", "required": ["run_id"]}, {"type": "object"}),
        Capability("campaign.run", ("simulate", "propose", "execute"), "external_send", True, {"simulate": "editor", "propose": "admin", "execute": "admin"}, {"type": "object", "required": ["campaign_id", "schedule"]}, {"type": "object"}),
        Capability("engine.rollout", ("simulate", "propose", "execute"), "configuration", True, {"simulate": "admin", "propose": "admin", "execute": "admin"}, {"type": "object", "required": ["updates"]}, {"type": "object"}),
        Capability("project_import.inspect", ("inspect",), "read", False, {"inspect": "viewer"}, {"type": "object", "required": ["import_id"]}, {"type": "object"}),
        Capability("project_import.apply", ("simulate", "propose", "execute"), "project_data", True, {"simulate": "editor", "propose": "admin", "execute": "admin"}, {"type": "object", "required": ["import_id"]}, {"type": "object"}),
        Capability("lifecycle_model.inspect", ("inspect",), "read", False, {"inspect": "viewer"}, {"type": "object"}, {"type": "object"}),
        Capability("lifecycle_model.activate", ("simulate", "propose", "execute"), "configuration", True, {"simulate": "editor", "propose": "admin", "execute": "admin"}, {"type": "object", "required": ["model_id", "target_status"]}, {"type": "object"}),
        Capability("orchestration.cutover", ("simulate", "propose", "execute"), "orchestration", True, {"simulate": "editor", "propose": "admin", "execute": "admin"}, {"type": "object", "required": ["purpose_key", "mode", "expected_epoch"]}, {"type": "object"}),
        Capability("orchestration.rollback", ("simulate", "propose", "execute"), "orchestration", True, {"simulate": "editor", "propose": "admin", "execute": "admin"}, {"type": "object", "required": ["purpose_key", "expected_epoch"]}, {"type": "object"}),
    )
}


class CapabilityError(ValueError): pass


class CapabilityRegistry:
    def __init__(self, db: Session): self.db = db

    def definitions(self) -> list[dict]:
        return [{"key": c.key, "phases": c.phases, "effect": c.effect, "approval_required": c.approval_required, "rbac": c.rbac, "input_schema": c.input_schema, "output_schema": c.output_schema} for c in CAPABILITIES.values()]

    def invoke(self, *, project_id: int, key: str, phase: str, payload: dict[str, Any], actor_user_id: int | None, idempotency_key: str, decision_choice_id: int | None = None) -> dict:
        capability = CAPABILITIES.get(key)
        if not capability or phase not in capability.phases: raise CapabilityError("Capability or phase is not registered")
        missing = [name for name in capability.input_schema.get("required", []) if name not in payload]
        if missing:
            raise CapabilityError("Missing capability input: " + ", ".join(missing))
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str); input_hash = hashlib.sha256(raw.encode()).hexdigest()
        existing = self.db.query(CapabilityExecution).filter(CapabilityExecution.project_id == project_id, CapabilityExecution.capability_key == key, CapabilityExecution.idempotency_key == idempotency_key).first()
        if existing:
            if existing.input_hash != input_hash or existing.phase != phase: raise CapabilityError("Idempotency key was used with different input")
            return existing.output_payload or {"status": existing.status}
        if phase == "execute" and capability.approval_required and not decision_choice_id:
            raise CapabilityError("Approved decision_choice_id is required for execution")
        self._validate_flow(project_id, key, phase, payload, decision_choice_id)
        execution = CapabilityExecution(project_id=project_id, capability_key=key, phase=phase, idempotency_key=idempotency_key, actor_user_id=actor_user_id, decision_choice_id=decision_choice_id, input_hash=input_hash, input_payload=payload, status="running")
        self.db.add(execution); self.db.flush(); execution_id = execution.id
        try:
            output = self._dispatch(project_id, key, phase, payload, actor_user_id, decision_choice_id)
            output["capability_execution_id"] = execution.id
            safe_output = json.loads(json.dumps(output, default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value)))
            execution.output_payload = safe_output; execution.status = "completed"; execution.finished_at = datetime.utcnow(); self.db.commit()
            return safe_output
        except Exception as exc:
            self.db.rollback()
            failed = self.db.query(CapabilityExecution).filter(
                CapabilityExecution.id == execution_id,
            ).first()
            if failed:
                failed.status = "failed"; failed.error = str(exc); failed.finished_at = datetime.utcnow(); self.db.commit()
            raise

    @staticmethod
    def _core_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in payload.items()
            if key not in {"simulation_execution_id", "proposal_execution_id"}
        }

    def _validate_flow(
        self,
        project_id: int,
        capability_key: str,
        phase: str,
        payload: dict[str, Any],
        decision_choice_id: int | None,
    ) -> None:
        if phase not in {"propose", "execute"}:
            return
        parent_field = "simulation_execution_id" if phase == "propose" else "proposal_execution_id"
        parent_phase = "simulate" if phase == "propose" else "propose"
        parent_id = payload.get(parent_field)
        if not parent_id:
            raise CapabilityError(f"{parent_field} is required by the capability flow")
        parent = self.db.query(CapabilityExecution).filter(
            CapabilityExecution.id == int(parent_id),
            CapabilityExecution.project_id == project_id,
            CapabilityExecution.capability_key == capability_key,
            CapabilityExecution.phase == parent_phase,
            CapabilityExecution.status == "completed",
        ).first()
        if not parent:
            raise CapabilityError(f"Completed {parent_phase} execution not found")
        if self._core_payload(parent.input_payload or {}) != self._core_payload(payload):
            raise CapabilityError("Capability payload changed after simulation/proposal")
        if phase == "execute":
            evaluation_id = (parent.output_payload or {}).get("evaluation_id")
            choice = self.db.query(DecisionGateChoice).filter(
                DecisionGateChoice.id == int(decision_choice_id),
                DecisionGateChoice.project_id == project_id,
                DecisionGateChoice.evaluation_id == evaluation_id,
            ).first()
            if not choice:
                raise CapabilityError("Approval does not belong to the proposed consequence")

    def _dispatch(self, project_id, key, phase, payload, actor_user_id, choice_id):
        if key == "engine.inspect": return {"features": EngineRolloutService(self.db).list_features(project_id)}
        if key == "policies.inspect":
            policy = self.db.query(ProjectPolicy).filter(
                ProjectPolicy.project_id == project_id,
            ).first()
            return {"policy": None if not policy else {
                "contact_caps": policy.contact_caps,
                "channel_cooldowns": policy.channel_cooldowns,
                "quiet_hours": policy.quiet_hours,
                "suppression_config": policy.suppression_config,
                "priority_rules": policy.priority_rules,
                "is_active": policy.is_active,
                "updated_at": policy.updated_at,
            }}
        if key == "providers.inspect": return {"providers": [ProviderStateService.serialize(row) for row in ProviderStateService(self.db).refresh(project_id)]}
        if key == "campaign.outcome":
            run = self.db.query(CampaignRun).filter(
                CampaignRun.id == int(payload["run_id"]),
                CampaignRun.project_id == project_id,
            ).first()
            if not run:
                raise CapabilityError("Campaign run not found")
            outcome = self.db.query(DecisionGateOutcome).filter(
                DecisionGateOutcome.project_id == project_id,
                DecisionGateOutcome.choice_id == run.decision_choice_id,
            ).first() if run.decision_choice_id else None
            return {
                "run_id": run.id,
                "status": run.status,
                "projected": run.capacity_plan,
                "actual": outcome.outcome if outcome else None,
                "reconciled_at": outcome.reconciled_at if outcome else None,
            }
        if key == "campaign.run":
            campaign = self.db.query(Campaign).filter(Campaign.id == int(payload["campaign_id"]), Campaign.project_id == project_id).first()
            if not campaign: raise CapabilityError("Campaign not found")
            gate = DecisionGateService(self.db)
            if phase in {"simulate", "propose"}:
                evaluation = gate.evaluate_campaign(campaign, payload.get("schedule") or {}, actor_user_id)
                result = {"evaluation_id": evaluation.id, "options": evaluation.options, "expires_at": evaluation.expires_at}
                if phase == "propose": result["approval_required"] = True
                return result
            choice = gate.choice_for_run(project_id, campaign, int(choice_id))
            expected = payload.get("expected") or {}
            run = __import__("app.services.campaigns.service", fromlist=["CampaignService"]).CampaignService(self.db).create_run(
                campaign, schedule=payload.get("schedule") or {}, requested_by_user_id=actor_user_id,
                expected=expected, run_key=payload.get("run_key"), decision_choice_id=choice.id,
            )
            return {"run_id": run.id, "status": run.status}
        if key == "engine.rollout":
            updates = payload.get("updates") or []; service = EngineRolloutService(self.db)
            dependencies = service.validate(project_id, updates)
            if phase in {"simulate", "propose"}:
                evaluation = DecisionGateService(self.db).evaluate_rollout(project_id, updates, actor_user_id)
                return {"valid": not dependencies, "dependencies": dependencies, "approval_required": phase == "propose", "evaluation_id": evaluation.id, "options": evaluation.options, "expires_at": evaluation.expires_at}
            choice = DecisionGateService(self.db).choice_for_rollout(
                project_id, int(choice_id), updates,
            )
            features = service.update(project_id, updates, actor_user_id)
            choice.execution_type = "engine_rollout"
            choice.execution_id = str(project_id)
            self.db.commit()
            return {"features": features}
        if key == "project_import.inspect":
            row = self.db.query(ProjectImport).filter(
                ProjectImport.id == str(payload["import_id"]),
                ProjectImport.project_id == project_id,
            ).first()
            if not row:
                raise CapabilityError("Project import not found")
            return {
                "import_id": row.id,
                "status": row.status,
                "validation_report": row.validation_report,
                "reconciliation_report": row.reconciliation_report,
                "record_counts": row.record_counts,
            }
        if key == "project_import.apply":
            from app.services.project_import_service import ProjectImportError, ProjectImportService
            service = ProjectImportService(self.db)
            try:
                row = service.get(project_id, str(payload["import_id"]))
                simulation = {
                    "import_id": row.id,
                    "status": row.status,
                    "ready": row.status == "ready" and bool((row.validation_report or {}).get("valid")),
                    "counts": row.record_counts or {},
                    "validation_report": row.validation_report,
                    "consequences": {
                        "external_sends": 0,
                        "live_event_dispatch": 0,
                        "contacts_mutated": (row.record_counts or {}).get("contacts", 0),
                        "automations_enabled": 0,
                    },
                }
                if phase == "simulate":
                    return simulation
                if phase == "propose":
                    evaluation = self._consequence_evaluation(
                        project_id, key, "project_import", row.id, payload, simulation,
                        "apply_import", actor_user_id, recommended=simulation["ready"],
                    )
                    return {**simulation, "evaluation_id": evaluation.id, "options": evaluation.options, "expires_at": evaluation.expires_at, "approval_required": True}
                self._require_choice(project_id, int(choice_id), "apply_import")
                return service.apply(row, actor_user_id)
            except ProjectImportError as exc:
                raise CapabilityError(str(exc)) from exc
        if key == "lifecycle_model.inspect":
            query = self.db.query(LifecycleModel).filter(LifecycleModel.project_id == project_id)
            if payload.get("model_id"):
                query = query.filter(LifecycleModel.id == int(payload["model_id"]))
            return {"models": [{
                "id": row.id, "version": row.version, "name": row.name,
                "status": row.status, "checksum": row.checksum,
                "validation_report": row.validation_report,
            } for row in query.order_by(LifecycleModel.version.desc()).all()]}
        if key == "lifecycle_model.activate":
            from app.services.lifecycle_model_service import (
                LifecycleModelError,
                LifecycleModelService,
                validate_lifecycle_definition,
            )
            service = LifecycleModelService(self.db)
            try:
                model = service.get(project_id, int(payload["model_id"]))
                # Simulation/proposal is read-only. Persist validation state
                # only in the explicit activation execution path.
                report = validate_lifecycle_definition(model.definition or {})
                current = self.db.query(LifecycleModel).filter(
                    LifecycleModel.project_id == project_id,
                    LifecycleModel.status == payload["target_status"],
                    LifecycleModel.id != model.id,
                ).first()
                comparison = service.compare(current, model, sample_limit=int(payload.get("sample_limit", 250))) if current else None
                simulation = {
                    "model_id": model.id,
                    "target_status": payload["target_status"],
                    "valid": report["valid"],
                    "validation_report": report,
                    "comparison": comparison,
                    "transition_events_on_activation": 0,
                }
                if phase == "simulate":
                    return simulation
                if phase == "propose":
                    evaluation = self._consequence_evaluation(
                        project_id, key, "lifecycle_model", str(model.id), payload,
                        simulation, "activate_model", actor_user_id, recommended=report["valid"],
                    )
                    return {**simulation, "evaluation_id": evaluation.id, "options": evaluation.options, "expires_at": evaluation.expires_at, "approval_required": True}
                self._require_choice(project_id, int(choice_id), "activate_model")
                return service.activate(model, target_status=str(payload["target_status"]), actor_user_id=actor_user_id)
            except LifecycleModelError as exc:
                raise CapabilityError(str(exc)) from exc
        if key in {"orchestration.cutover", "orchestration.rollback"}:
            from app.services.orchestration_cutover_service import OrchestrationCutoverError, OrchestrationCutoverService
            service = OrchestrationCutoverService(self.db)
            try:
                purpose_key = str(payload["purpose_key"])
                current = service.get(project_id, purpose_key)
                target_mode = (
                    str(payload["mode"])
                    if key == "orchestration.cutover"
                    else (current.previous_mode if current and current.previous_mode else "legacy")
                )
                model_id = payload.get("lifecycle_model_id")
                if key == "orchestration.rollback" and current and target_mode in {"shadow", "versya"}:
                    model_id = current.lifecycle_model_id
                simulation = service.preview(
                    project_id, purpose_key, target_mode,
                    int(payload["expected_epoch"]), int(model_id) if model_id is not None else None,
                )
                if phase == "simulate":
                    return simulation
                option_key = "apply_cutover" if key == "orchestration.cutover" else "apply_rollback"
                if phase == "propose":
                    evaluation = self._consequence_evaluation(
                        project_id, key, "orchestration_purpose", purpose_key,
                        payload, simulation, option_key, actor_user_id,
                        recommended=simulation["valid"],
                    )
                    return {**simulation, "evaluation_id": evaluation.id, "options": evaluation.options, "expires_at": evaluation.expires_at, "approval_required": True}
                self._require_choice(project_id, int(choice_id), option_key)
                if key == "orchestration.rollback":
                    changed = service.rollback(
                        project_id,
                        purpose_key=purpose_key,
                        expected_epoch=int(payload["expected_epoch"]),
                        reason=str(payload.get("reason") or "Approved orchestration rollback"),
                        actor_user_id=actor_user_id,
                    )
                else:
                    changed = service.update(
                        project_id,
                        purpose_key=purpose_key,
                        mode=target_mode,
                        expected_epoch=int(payload["expected_epoch"]),
                        lifecycle_model_id=int(model_id) if model_id is not None else None,
                        reason=str(payload.get("reason") or "Approved orchestration cutover"),
                        actor_user_id=actor_user_id,
                    )
                return {
                    "purpose_key": changed.purpose_key,
                    "mode": changed.mode,
                    "previous_mode": changed.previous_mode,
                    "orchestration_epoch": changed.orchestration_epoch,
                    "lifecycle_model_id": changed.lifecycle_model_id,
                }
            except OrchestrationCutoverError as exc:
                raise CapabilityError(str(exc)) from exc
        raise CapabilityError("Capability handler is not implemented")

    def _consequence_evaluation(
        self,
        project_id: int,
        gate_type: str,
        subject_type: str,
        subject_id: str,
        payload: dict[str, Any],
        consequence: dict[str, Any],
        apply_option_key: str,
        actor_user_id: int | None,
        *,
        recommended: bool,
    ) -> DecisionGateEvaluation:
        snapshot = {"payload": self._core_payload(payload), "consequence": consequence}
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        options = [
            {"key": "do_nothing", "recommended": not recommended, "risk": "low"},
            {"key": apply_option_key, "recommended": recommended, "risk": "moderate", "consequence": consequence},
        ]
        row = DecisionGateEvaluation(
            project_id=project_id,
            gate_type=gate_type,
            subject_type=subject_type,
            subject_id=str(subject_id),
            status="valid",
            method="exact",
            inputs_hash=digest,
            input_snapshot=snapshot,
            baseline=options[0],
            options=options,
            provenance={"simulation": "exact", "external_sends": "exact"},
            evaluated_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=30),
            created_by_user_id=actor_user_id,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _require_choice(self, project_id: int, choice_id: int, option_key: str) -> DecisionGateChoice:
        choice = self.db.query(DecisionGateChoice).filter(
            DecisionGateChoice.id == choice_id,
            DecisionGateChoice.project_id == project_id,
            DecisionGateChoice.option_key == option_key,
        ).first()
        if not choice:
            raise CapabilityError(f"Approved {option_key} choice not found")
        return choice
