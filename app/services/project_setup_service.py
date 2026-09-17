"""Durable planning layer for agent-first project configuration."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.services.project_foundation import public_foundation


EVIDENCE_KINDS = {"observed", "inferred", "recommended", "tenant_decided"}
DECISION_STATUSES = {"proposed", "accepted", "rejected", "superseded"}
PHASE_STATUSES = {"ready", "needs_tenant_decision", "blocked", "defer", "completed"}
PLAN_STATUSES = {"draft", "in_progress", "completed", "superseded", "archived"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,159}$")
_PHASE_FIELDS = {
    "key", "title", "outcome", "status", "dependencies", "required_scopes",
    "proposed_tools", "required_decisions", "blockers", "approval_question", "exit_evidence",
}


DEFAULT_PHASES = [
    {
        "key": "discover",
        "title": "Understand the project",
        "outcome": "A scope-aware inventory and tenant interview with evidence labels.",
        "status": "ready",
        "dependencies": [],
        "required_scopes": ["versya.projects:read"],
        "proposed_tools": ["assess_project_setup", "get_project_onboarding_contract"],
        "required_decisions": ["business.first_value_outcome"],
        "blockers": [],
        "approval_question": "Does this describe the first outcome the project should deliver?",
        "exit_evidence": "Observed inventory, explicit open questions and one chosen first outcome.",
    },
    {
        "key": "foundation",
        "title": "Configure the project foundation",
        "outcome": "The project has explicit market, locale, timezone and success-event semantics.",
        "status": "needs_tenant_decision",
        "dependencies": ["discover"],
        "required_scopes": ["versya.projects:write"],
        "proposed_tools": ["configure_project_foundation"],
        "required_decisions": ["market.default", "project.goal_event"],
        "blockers": [],
        "approval_question": "Are these markets and the observable success event correct?",
        "exit_evidence": "A confirmed foundation diff with external_sends=0.",
    },
    {
        "key": "data_foundation",
        "title": "Establish identity and factual ingestion",
        "outcome": "Stable identities, consent and lifecycle-changing facts have source owners and a live path.",
        "status": "needs_tenant_decision",
        "dependencies": ["foundation"],
        "required_scopes": ["versya.ingestion:read"],
        "proposed_tools": ["get_event_ingestion_contract", "get_event_ingestion_status"],
        "required_decisions": ["data.identity_source", "data.consent_policy"],
        "blockers": [],
        "approval_question": "Are identity ownership, consent and source truth explicit?",
        "exit_evidence": "Canary facts converge without SSH, SQL or manual repair.",
    },
    {
        "key": "lifecycle_shadow",
        "title": "Model lifecycle in shadow",
        "outcome": "Type, Stage and Age classifications are explainable and produce no external sends.",
        "status": "needs_tenant_decision",
        "dependencies": ["data_foundation"],
        "required_scopes": ["versya.lifecycle:read", "versya.lifecycle:write"],
        "proposed_tools": ["get_lifecycle_model_contract", "create_lifecycle_model"],
        "required_decisions": ["lifecycle.relationships", "lifecycle.precedence"],
        "blockers": [],
        "approval_question": "Do representative contacts fall into useful, explainable positions?",
        "exit_evidence": "Reviewed distribution and explanations in shadow; external_sends=0.",
    },
    {
        "key": "attention_foundation",
        "title": "Define attention and pressure",
        "outcome": "Business purposes compete deterministically before any active journey.",
        "status": "needs_tenant_decision",
        "dependencies": ["lifecycle_shadow"],
        "required_scopes": ["versya.automations:read", "versya.automations:write"],
        "proposed_tools": ["get_orchestration_contract", "configure_attention_planning"],
        "required_decisions": ["attention.purpose_order", "attention.manual_touch_policy"],
        "blockers": [],
        "approval_question": "When communications compete, is the winning outcome and lower-layer effect correct?",
        "exit_evidence": "Purpose order, entry effects, cooldowns and caps with consequence_at_gate.",
    },
    {
        "key": "first_value_slice",
        "title": "Build one end-to-end value slice",
        "outcome": "One purpose is reviewable in draft or shadow before expansion.",
        "status": "defer",
        "dependencies": ["attention_foundation"],
        "required_scopes": ["versya.campaigns:read", "versya.campaigns:write"],
        "proposed_tools": ["get_campaign_recipe_contract", "create_draft_campaign_recipe"],
        "required_decisions": ["operating.copy_autonomy", "operating.activation_owner"],
        "blockers": [],
        "approval_question": "Should this purpose become the first production value slice?",
        "exit_evidence": "One draft/shadow path with monitoring, rollback and external_sends=0.",
    },
]


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _key(value: Any, field: str, maximum: int = 160) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) > maximum or not _KEY_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a stable lowercase key")
    return normalized


def _short_text(value: Any, field: str, maximum: int, minimum: int = 1) -> str:
    normalized = str(value or "").strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        raise ValueError(f"{field} must contain {minimum} to {maximum} characters")
    return normalized


def normalize_phases(phases: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    raw_phases = deepcopy(phases if phases is not None else DEFAULT_PHASES)
    if not isinstance(raw_phases, list) or not raw_phases or len(raw_phases) > 20:
        raise ValueError("phases must contain 1 to 20 phase objects")
    normalized = []
    keys = set()
    for index, phase in enumerate(raw_phases):
        if not isinstance(phase, dict):
            raise ValueError(f"phases[{index}] must be an object")
        unknown = set(phase) - _PHASE_FIELDS
        if unknown:
            raise ValueError(f"phases[{index}] contains unsupported fields: {sorted(unknown)}")
        phase_key = _key(phase.get("key"), f"phases[{index}].key", 120)
        if phase_key in keys:
            raise ValueError(f"Duplicate phase key: {phase_key}")
        keys.add(phase_key)
        status = str(phase.get("status") or "needs_tenant_decision").strip().lower()
        if status not in PHASE_STATUSES:
            raise ValueError(f"Invalid phase status: {status}")
        dependencies = list(dict.fromkeys(
            _key(item, "dependency", 120) for item in phase.get("dependencies") or []
        ))
        normalized.append({
            "key": phase_key,
            "title": _short_text(phase.get("title"), f"phases[{index}].title", 255),
            "outcome": _short_text(phase.get("outcome"), f"phases[{index}].outcome", 2000),
            "status": status,
            "dependencies": dependencies,
            "required_scopes": sorted({_short_text(item, "required scope", 120) for item in phase.get("required_scopes") or []}),
            "proposed_tools": [_short_text(item, "proposed tool", 120) for item in phase.get("proposed_tools") or []],
            "required_decisions": [_key(item, "required decision") for item in phase.get("required_decisions") or []],
            "blockers": [_short_text(item, "blocker", 1000) for item in phase.get("blockers") or []],
            "approval_question": _short_text(phase.get("approval_question"), f"phases[{index}].approval_question", 1000),
            "exit_evidence": _short_text(phase.get("exit_evidence"), f"phases[{index}].exit_evidence", 2000),
        })
    for phase in normalized:
        missing = [key for key in phase["dependencies"] if key not in keys]
        if missing or phase["key"] in phase["dependencies"]:
            raise ValueError(f"Phase {phase['key']} has invalid dependencies: {missing}")

    dependencies_by_key = {phase["key"]: phase["dependencies"] for phase in normalized}
    visiting = set()
    visited = set()

    def visit(phase_key: str) -> None:
        if phase_key in visiting:
            raise ValueError(f"Phase dependency graph contains a cycle at {phase_key}")
        if phase_key in visited:
            return
        visiting.add(phase_key)
        for dependency in dependencies_by_key[phase_key]:
            visit(dependency)
        visiting.remove(phase_key)
        visited.add(phase_key)

    for phase_key in dependencies_by_key:
        visit(phase_key)
    return normalized


def normalize_plan_data(
    phases: Optional[List[Dict[str, Any]]],
    assumptions: Optional[List[str]],
    product_gaps: Optional[List[str]],
) -> Dict[str, Any]:
    return {
        "contract_version": "1.0",
        "phases": normalize_phases(phases),
        "assumptions": [_short_text(item, "assumption", 1000) for item in assumptions or []],
        "product_gaps": [_short_text(item, "product gap", 1000) for item in product_gaps or []],
        "execution_rule": (
            "Phase approval records business intent only. Every module write still requires its own "
            "dry-run, consequence gate and confirmation. No setup-plan operation authorizes a send."
        ),
    }


def normalize_plan_identity(title: Any, objective: Any) -> Dict[str, str]:
    return {
        "title": _short_text(title, "title", 255),
        "objective": _short_text(objective, "objective", 4000),
    }


def normalize_decision_input(
    decision_key: Any,
    evidence_kind: Any,
    status: Any,
    value: Any,
    rationale: Any,
    sources: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    key = _key(decision_key, "decision_key")
    evidence = str(evidence_kind or "").strip().lower()
    decision_status = str(status or "").strip().lower()
    if evidence not in EVIDENCE_KINDS:
        raise ValueError(f"evidence_kind must be one of {sorted(EVIDENCE_KINDS)}")
    if decision_status not in DECISION_STATUSES:
        raise ValueError(f"status must be one of {sorted(DECISION_STATUSES)}")
    if decision_status == "accepted" and evidence != "tenant_decided":
        raise ValueError("Only an explicit tenant_decided value may be accepted")
    if value is None:
        raise ValueError("value is required; use an explicit object or scalar")
    if sources is not None and (not isinstance(sources, list) or len(sources) > 20):
        raise ValueError("sources must be an array with at most 20 evidence references")
    return {
        "decision_key": key,
        "evidence_kind": evidence,
        "status": decision_status,
        "value": deepcopy(value),
        "rationale": _short_text(rationale, "rationale", 4000),
        "sources": deepcopy(sources),
    }


class ProjectSetupService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def assessment(project: models.Project, counts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        foundation = public_foundation(project)
        missing = []
        if not foundation["goal_event"]:
            missing.append("project.goal_event")
        if not foundation["market_config"]:
            missing.append("market.default")
        return {
            "contract_version": "1.0",
            "project_id": project.id,
            "foundation": foundation,
            "foundation_ready": not missing,
            "missing_tenant_decisions": missing,
            "observed_counts": counts or {},
            "recommendation": (
                "Record the first business outcome, then configure explicit market and success-event semantics."
                if missing else
                "Continue with identity/consent and live factual ingestion before lifecycle activation."
            ),
            "evidence_labels": {
                "foundation": "observed",
                "observed_counts": "observed",
                "missing_tenant_decisions": "inferred",
                "recommendation": "recommended",
            },
            "external_sends": 0,
        }

    def latest_decisions(self, plan_id: int) -> Dict[str, models.ProjectSetupDecision]:
        rows = (
            self.db.query(models.ProjectSetupDecision)
            .filter(models.ProjectSetupDecision.plan_id == plan_id)
            .order_by(models.ProjectSetupDecision.decision_key, models.ProjectSetupDecision.revision.desc())
            .all()
        )
        latest = {}
        for row in rows:
            latest.setdefault(row.decision_key, row)
        return latest

    def current_fingerprint(self, plan: models.ProjectSetupPlan) -> str:
        decisions = [
            {
                "key": key,
                "revision": row.revision,
                "evidence_kind": row.evidence_kind,
                "status": row.status,
                "value": row.value,
                "rationale": row.rationale,
            }
            for key, row in sorted(self.latest_decisions(plan.id).items())
        ]
        return _hash({
            "project_id": plan.project_id,
            "version": plan.version,
            "title": plan.title,
            "objective": plan.objective,
            "plan_data": plan.plan_data,
            "decisions": decisions,
        })

    def refresh_fingerprint(self, plan: models.ProjectSetupPlan) -> str:
        plan.fingerprint = self.current_fingerprint(plan)
        return plan.fingerprint

    def create_plan(
        self,
        project: models.Project,
        title: str,
        objective: str,
        plan_data: Dict[str, Any],
        assessment_snapshot: Dict[str, Any],
        user_id: Optional[int],
    ) -> models.ProjectSetupPlan:
        # Serialize version allocation for agents that start onboarding concurrently.
        self.db.query(models.Project).filter(models.Project.id == project.id).with_for_update().one()
        version = int(
            self.db.query(func.max(models.ProjectSetupPlan.version))
            .filter(models.ProjectSetupPlan.project_id == project.id)
            .scalar() or 0
        ) + 1
        identity = normalize_plan_identity(title, objective)
        plan = models.ProjectSetupPlan(
            project_id=project.id,
            version=version,
            title=identity["title"],
            objective=identity["objective"],
            status="draft",
            plan_data=deepcopy(plan_data),
            assessment_snapshot=deepcopy(assessment_snapshot),
            fingerprint="0" * 64,
            created_by_user_id=user_id,
        )
        self.db.add(plan)
        self.db.flush()
        self.refresh_fingerprint(plan)
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def get_plan(self, project_id: int, plan_id: int) -> models.ProjectSetupPlan:
        plan = self.db.query(models.ProjectSetupPlan).filter(
            models.ProjectSetupPlan.id == plan_id,
            models.ProjectSetupPlan.project_id == project_id,
        ).first()
        if not plan:
            raise ValueError("Project setup plan not found")
        return plan

    def record_decision(
        self,
        plan: models.ProjectSetupPlan,
        decision_key: str,
        evidence_kind: str,
        status: str,
        value: Any,
        rationale: str,
        sources: Optional[List[Dict[str, Any]]],
        user_id: Optional[int],
        expected_fingerprint: Optional[str] = None,
    ) -> models.ProjectSetupDecision:
        plan = (
            self.db.query(models.ProjectSetupPlan)
            .filter(models.ProjectSetupPlan.id == plan.id)
            .with_for_update()
            .one()
        )
        if expected_fingerprint and self.current_fingerprint(plan) != expected_fingerprint:
            raise ValueError("Plan fingerprint changed while recording the decision; review it again")
        normalized = normalize_decision_input(
            decision_key, evidence_kind, status, value, rationale, sources
        )
        key = normalized["decision_key"]
        revision = int(
            self.db.query(func.max(models.ProjectSetupDecision.revision))
            .filter(
                models.ProjectSetupDecision.plan_id == plan.id,
                models.ProjectSetupDecision.decision_key == key,
            ).scalar() or 0
        ) + 1
        row = models.ProjectSetupDecision(
            project_id=plan.project_id,
            plan_id=plan.id,
            decision_key=key,
            revision=revision,
            evidence_kind=normalized["evidence_kind"],
            status=normalized["status"],
            value=normalized["value"],
            rationale=normalized["rationale"],
            sources=normalized["sources"],
            created_by_user_id=user_id,
        )
        self.db.add(row)
        self.db.flush()
        self.refresh_fingerprint(plan)
        self.db.commit()
        self.db.refresh(row)
        return row

    def phase_gate(self, plan: models.ProjectSetupPlan, phase_key: str) -> Dict[str, Any]:
        key = _key(phase_key, "phase_key", 120)
        phase = next((item for item in plan.plan_data["phases"] if item["key"] == key), None)
        if not phase:
            raise ValueError("Phase not found in this plan")
        latest = self.latest_decisions(plan.id)
        missing_decisions = []
        for required in phase.get("required_decisions") or []:
            decision = latest.get(required)
            if not decision or decision.evidence_kind != "tenant_decided" or decision.status != "accepted":
                missing_decisions.append(required)
        blockers = list(phase.get("blockers") or [])
        if phase.get("status") == "blocked":
            blockers.append("The plan marks this phase as blocked")
        fingerprint = self.current_fingerprint(plan)
        stale_approvals = [
            approval.id for approval in plan.phase_approvals
            if approval.phase_key == key and approval.plan_fingerprint != fingerprint
        ]
        current_approval = next((
            approval for approval in reversed(plan.phase_approvals)
            if approval.phase_key == key and approval.plan_fingerprint == fingerprint
        ), None)
        return {
            "phase": phase,
            "can_approve": not blockers and not missing_decisions,
            "blockers": blockers,
            "missing_tenant_decisions": missing_decisions,
            "current_plan_fingerprint": fingerprint,
            "current_approval_id": current_approval.id if current_approval else None,
            "stale_approval_ids": stale_approvals,
            "execution_authorized": False,
            "external_sends": 0,
        }

    def approve_phase(
        self,
        plan: models.ProjectSetupPlan,
        phase_key: str,
        note: str,
        user_id: Optional[int],
        expected_fingerprint: Optional[str] = None,
    ) -> models.ProjectSetupPhaseApproval:
        plan = (
            self.db.query(models.ProjectSetupPlan)
            .filter(models.ProjectSetupPlan.id == plan.id)
            .with_for_update()
            .one()
        )
        gate = self.phase_gate(plan, phase_key)
        if expected_fingerprint and gate["current_plan_fingerprint"] != expected_fingerprint:
            raise ValueError("Plan fingerprint changed while approving the phase; review it again")
        if not gate["can_approve"]:
            raise ValueError("Phase has unresolved blockers or tenant decisions")
        existing = self.db.query(models.ProjectSetupPhaseApproval).filter(
            models.ProjectSetupPhaseApproval.plan_id == plan.id,
            models.ProjectSetupPhaseApproval.phase_key == gate["phase"]["key"],
            models.ProjectSetupPhaseApproval.plan_fingerprint == gate["current_plan_fingerprint"],
        ).first()
        if existing:
            return existing
        approval = models.ProjectSetupPhaseApproval(
            project_id=plan.project_id,
            plan_id=plan.id,
            phase_key=gate["phase"]["key"],
            plan_fingerprint=gate["current_plan_fingerprint"],
            approval_note=_short_text(note, "approval_note", 2000),
            approved_by_user_id=user_id,
        )
        self.db.add(approval)
        if plan.status == "draft":
            plan.status = "in_progress"
        self.db.commit()
        self.db.refresh(approval)
        return approval

    def payload(self, plan: models.ProjectSetupPlan) -> Dict[str, Any]:
        latest = self.latest_decisions(plan.id)
        fingerprint = self.current_fingerprint(plan)
        phase_gates = [self.phase_gate(plan, phase["key"]) for phase in plan.plan_data["phases"]]
        return {
            "id": plan.id,
            "project_id": plan.project_id,
            "version": plan.version,
            "title": plan.title,
            "objective": plan.objective,
            "status": plan.status,
            "plan_data": plan.plan_data,
            "assessment_snapshot": plan.assessment_snapshot,
            "fingerprint": fingerprint,
            "decisions": [
                {
                    "id": row.id,
                    "decision_key": key,
                    "revision": row.revision,
                    "evidence_kind": row.evidence_kind,
                    "status": row.status,
                    "value": row.value,
                    "rationale": row.rationale,
                    "sources": row.sources,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for key, row in sorted(latest.items())
            ],
            "phase_gates": phase_gates,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
            "execution_authorized": False,
            "external_sends": 0,
        }

    def compare(self, left: models.ProjectSetupPlan, right: models.ProjectSetupPlan) -> Dict[str, Any]:
        left_payload = self.payload(left)
        right_payload = self.payload(right)
        left_phases = {item["key"]: item for item in left.plan_data["phases"]}
        right_phases = {item["key"]: item for item in right.plan_data["phases"]}
        left_decisions = {item["decision_key"]: item for item in left_payload["decisions"]}
        right_decisions = {item["decision_key"]: item for item in right_payload["decisions"]}
        return {
            "project_id": left.project_id,
            "from_plan": {"id": left.id, "version": left.version, "fingerprint": left_payload["fingerprint"]},
            "to_plan": {"id": right.id, "version": right.version, "fingerprint": right_payload["fingerprint"]},
            "phase_changes": [
                {"key": key, "from": left_phases.get(key), "to": right_phases.get(key)}
                for key in sorted(set(left_phases) | set(right_phases))
                if left_phases.get(key) != right_phases.get(key)
            ],
            "decision_changes": [
                {"key": key, "from": left_decisions.get(key), "to": right_decisions.get(key)}
                for key in sorted(set(left_decisions) | set(right_decisions))
                if left_decisions.get(key) != right_decisions.get(key)
            ],
            "external_sends": 0,
        }
