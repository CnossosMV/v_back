"""Business logic behind Versya MCP tools."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import re
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal, engine
from app.mcp.audit import consume_pending_action, create_pending_action, log_tool_call
from app.mcp.auth import (
    McpPrincipal,
    require_admin_principal,
    require_project_access,
    require_scopes,
)
from app.mcp.config import get_mcp_settings, mcp_resource_url
from app.models.messaging import MessagingEvent, MessagingTemplate, MessagingUser
from app.schemas.messaging import (
    MessagingTemplateCreate,
    MessagingTemplatePreviewRequest,
    MessagingTemplateUpdate,
)
from app.schemas.event_actions import EventActionCreate, EventActionUpdate
from app.schemas.project_variables import ProjectVariableCreate, ProjectVariableUpdate
from app.services.authorization_service import get_user_projects, is_workspace_admin
from app.services.channels.channel_health_service import ChannelHealthService
from app.services.event_actions.funnel_compiler import compile_event_action
from app.services.funnel_service import FunnelService
from app.services.funnel.lint import lint_funnel
from app.services.messaging import template_renderer
from app.services.project_variable_service import ProjectVariableService
from app.services.automation_conflicts import analyze_automation_conflicts


_PROJECT_PUBLIC_FIELDS = [
    "id",
    "name",
    "description",
    "workspace_id",
    "is_active",
    "inbox_ttl_waiting_agent",
    "inbox_ttl_waiting_customer",
    "goal_event",
    "default_locale",
    "supported_locales",
    "default_timezone",
    "market_config",
    "created_at",
    "updated_at",
]


def _row(model: Any, fields: Optional[List[str]] = None) -> Dict[str, Any]:
    if fields is None:
        fields = [column.name for column in model.__table__.columns]
    result = {}
    for field in fields:
        value = getattr(model, field, None)
        if hasattr(value, "value"):
            value = value.value
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        result[field] = value
    return result


def _project_public_row(project: models.Project) -> Dict[str, Any]:
    """Serialize tenant-visible project metadata without internal key material."""
    return _row(project, _PROJECT_PUBLIC_FIELDS)


def _project_onboarding_contract_document(
    project: models.Project,
    counts: Dict[str, Optional[int]],
    lifecycle_models: List[Dict[str, Any]],
    inventory_scope_status: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build the read-only zero-to-one brief a tenant agent must follow."""
    return {
        "contract_version": "1.2",
        "project": _project_public_row(project),
        "observed_inventory": {
            "counts": counts,
            "lifecycle_models": lifecycle_models,
            "scope_status": inventory_scope_status or {},
            "project_signals": {
                "locale_configured": bool(project.default_locale),
                "timezone_configured": bool(project.default_timezone),
                "goal_event_configured": bool(project.goal_event),
                "markets_configured": bool(counts.get("active_markets")),
            },
        },
        "agent_mandate": {
            "outcome": (
                "Help a non-technical tenant choose the smallest coherent project foundation, "
                "translate business answers into reviewable Versya configuration, and stop before "
                "any write or external-send authorization until the tenant approves the relevant gate."
            ),
            "language": "Interview in business language; never ask the tenant to hand-author tool JSON.",
            "evidence_labels": ["observed", "inferred", "recommended", "tenant_decided"],
            "non_goals": [
                "Do not configure every module merely because it exists.",
                "Do not invent lifecycle keys, purpose priorities, consent, source facts or copy claims.",
                "Do not use SSH, SQL, private routes or provider consoles to fill an MCP capability gap.",
            ],
        },
        "persistent_plan_contract": {
            "purpose": "Shared, versioned setup memory across agents and sessions.",
            "tools": [
                "assess_project_setup", "create_project_setup_plan",
                "list_project_setup_plans", "get_project_setup_plan",
                "record_project_setup_decision", "approve_project_setup_phase",
                "compare_project_setup_plans", "set_project_setup_plan_status",
            ],
            "fingerprint_rule": (
                "Every decision revision changes the plan fingerprint and makes prior phase approvals stale."
            ),
            "execution_rule": (
                "Phase approval records business intent only; module-specific dry-run and confirmation gates remain mandatory."
            ),
            "external_sends": 0,
        },
        "tenant_interview": [
            {
                "topic": "business_outcome",
                "ask": "What should this project help the business achieve first, and which observable fact proves success?",
                "produces": ["first_value_outcome", "candidate_success_event", "initial_purpose_key"],
            },
            {
                "topic": "people_and_identity",
                "ask": "Who are the people or accounts, which source owns their stable identity, and how is consent proven?",
                "produces": ["identity_namespace", "source_of_truth", "consent_policy", "dedupe_constraints"],
            },
            {
                "topic": "current_relationships",
                "ask": "Which durable commercial relationships and operational conditions matter today?",
                "produces": ["lifecycle_type_candidates", "stage_candidates", "type_entry_clock", "age_buckets"],
            },
            {
                "topic": "markets_and_channels",
                "ask": "Which countries, timezones, languages and approved channels can be used?",
                "produces": ["market_calendar", "locales", "channel_constraints"],
            },
            {
                "topic": "attention_and_pressure",
                "ask": "When several valid communications compete, which outcomes win, which journeys end, and what pressure is acceptable?",
                "produces": ["purpose_order", "entry_effects", "cooldowns", "caps", "manual_touch_policy"],
            },
            {
                "topic": "operating_model",
                "ask": "What may the agent prepare alone, what always needs review, and who can activate or authorize sends?",
                "produces": ["copy_autonomy", "approval_owners", "rollout_mode"],
            },
        ],
        "configuration_ladder": [
            {
                "phase": "01_discover",
                "goal": "Resolve the project and inventory current configuration without writes.",
                "tools": [
                    "get_mcp_status", "list_projects", "get_project_overview",
                    "get_agent_skill_catalog", "assess_project_setup",
                    "get_project_onboarding_contract", "list_channel_health", "list_templates",
                    "list_project_variables", "list_funnels", "list_event_actions", "list_campaigns",
                    "list_campaign_recipes",
                ],
                "exit_evidence": "A gap matrix labeled observed/inferred/recommended/tenant_decided.",
            },
            {
                "phase": "02_persist_proposal",
                "goal": "Persist the phased proposal and evidence-labeled tenant choices.",
                "tools": [
                    "create_project_setup_plan", "record_project_setup_decision",
                    "get_project_setup_plan", "approve_project_setup_phase",
                ],
                "exit_evidence": "A current plan fingerprint with explicit open decisions and no implicit execution.",
                "send_boundary": "Plan and phase approval always have external_sends=0.",
            },
            {
                "phase": "03_project_foundation",
                "goal": "Configure explicit market, locale, timezone and factual success-event semantics.",
                "tools": [
                    "configure_project_foundation", "get_project_market_contract",
                    "list_project_markets", "create_project_market", "update_project_market",
                    "set_default_project_market", "set_project_market_status",
                ],
                "exit_evidence": (
                    "A confirmed foundation diff and paginated first-class market inventory; "
                    "market remains distinct from locale."
                ),
            },
            {
                "phase": "04_data_foundation",
                "goal": "Define stable identity, consent, source truth and continuous factual ingestion.",
                "tools": [
                    "get_project_import_contract", "get_event_ingestion_contract",
                    "get_event_ingestion_status",
                ],
                "conditional": "Use Project Import only when historical source data exists; live ingestion is still required.",
                "exit_evidence": "Canary identity/event scenarios and an explicit owner for every lifecycle-changing fact.",
            },
            {
                "phase": "05_lifecycle_shadow",
                "goal": "Translate durable relationship, condition and recency into Type/Stage/Age and compare in shadow.",
                "tools": [
                    "get_lifecycle_model_contract", "validate_lifecycle_model_definition",
                    "create_lifecycle_model", "get_lifecycle_distribution",
                    "explain_contact_lifecycle", "compare_lifecycle_materialization",
                ],
                "exit_evidence": "Reviewed distribution, representative explanations and zero external sends.",
            },
            {
                "phase": "06_attention_foundation",
                "goal": "Name business intents and decide deterministic competition before activating journeys.",
                "tools": [
                    "get_orchestration_contract", "list_orchestration_purposes",
                    "get_attention_planning_contract", "configure_attention_planning",
                    "preview_contact_attention_plan", "inspect_automation_conflicts",
                    "lint_funnel_configuration",
                ],
                "exit_evidence": "Purpose briefs, order, entry effects, pressure policy and consequence_at_gate.",
            },
            {
                "phase": "07_first_value_slice",
                "goal": "Build one purpose end to end in draft/shadow before expanding the project.",
                "tools": [
                    "get_campaign_recipe_contract", "preview_campaign_recipe",
                    "create_draft_campaign_recipe", "get_campaign_planning_inbox",
                    "preview_orchestration_impact", "assess_orchestration_readiness",
                ],
                "exit_evidence": "One reviewable draft or shadow path, rollback instruction and external_sends=0.",
            },
            {
                "phase": "08_activate_gradually",
                "goal": "Activate and transfer ownership purpose by purpose using exact-impact confirmations.",
                "tools": [
                    "preview_orchestration_impact", "set_orchestration_cutover",
                    "evaluate_campaign_run_consequences", "schedule_campaign_run",
                    "get_campaign_run_status", "cancel_campaign_run",
                ],
                "exit_evidence": "Tenant-approved consequence, exact owner/epoch, monitoring and rollback.",
                "send_boundary": "Only confirmed run scheduling authorizes future external sends.",
            },
        ],
        "proposal_output_contract": {
            "required_sections": [
                "project_snapshot",
                "business_answers_and_open_questions",
                "recommended_foundation_with_reasons",
                "phased_configuration_plan",
                "exact_next_mcp_calls",
                "approvals_and_consequence_gates",
                "product_gaps",
            ],
            "phase_statuses": ["ready", "needs_tenant_decision", "blocked", "defer", "completed"],
            "rules": [
                "Recommend the smallest next phase that can produce observable value.",
                "Present defaults as recommendations with trade-offs, never as tenant decisions.",
                "Reuse existing compatible configuration instead of creating near-duplicates.",
                "Every write begins with dry-run and exact consequences; discovery itself has external_sends=0.",
                "If a required setting has no tenant-safe MCP/public write path, report a product gap.",
                "Persist the approved proposal and decisions; never rely on chat history as project state.",
                "A setup phase approval never replaces the specific module consequence gate.",
            ],
        },
        "external_sends": 0,
        "next": (
            "Complete the tenant interview, run the phase-01 inventory tools allowed by the current grant, "
            "and present a proposal before requesting any write scope or confirmation."
        ),
    }


def _safe_count(db: Session, model: Any, project_id: int) -> int:
    return db.query(func.count(model.id)).filter(model.project_id == project_id).scalar() or 0


def _accessible_projects(db: Session, principal: McpPrincipal) -> List[Dict[str, Any]]:
    user = db.query(models.User).filter(models.User.id == principal.user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if is_workspace_admin(db, user):
        projects = (
            db.query(models.Project)
            .filter(models.Project.workspace_id == user.workspace_id, models.Project.is_active == True)
            .order_by(models.Project.name)
            .all()
        )
        return [{"project": project, "role": "owner"} for project in projects]
    return get_user_projects(db, principal.user_id)


def _resolve_project_id(db: Session, principal: McpPrincipal, project_id: Optional[int]) -> int:
    if project_id is not None:
        return project_id
    if principal.default_project_id is not None:
        return principal.default_project_id

    projects = _accessible_projects(db, principal)
    if len(projects) == 1:
        return projects[0]["project"].id
    if not projects:
        raise HTTPException(status_code=403, detail="No accessible Versya projects were found")
    raise HTTPException(
        status_code=400,
        detail="No default MCP project is configured. Call list_projects, choose a project_id, or set a default project in Versya Settings > MCP.",
    )


def _handle_tool(
    tool_name: str,
    project_id: Optional[int],
    input_summary: Dict[str, Any],
    callback: Callable[[Session, McpPrincipal], Any],
):
    db = SessionLocal()
    principal = None
    try:
        principal = require_scopes([])
        audit_project_id = project_id or principal.default_project_id
        output = callback(db, principal)
        log_tool_call(db, principal, tool_name, "success", input_summary, _output_summary(output), project_id=audit_project_id)
        return output
    except Exception as exc:
        status = "denied" if exc.__class__.__name__.endswith(("AuthError", "AuthorizationError")) else "error"
        try:
            audit_project_id = project_id or (principal.default_project_id if principal else None)
            log_tool_call(db, principal, tool_name, status, input_summary, None, str(exc), audit_project_id)
        except Exception:
            pass
        raise
    finally:
        db.close()


def _output_summary(output: Any) -> Any:
    if isinstance(output, list):
        return {"count": len(output), "sample": output[:3]}
    if isinstance(output, dict):
        return {k: v for k, v in output.items() if k not in {"body", "rendered_body"}}
    return output


def _oauth_session_status(principal: McpPrincipal) -> Dict[str, Any]:
    """Describe token durability without exposing OAuth credentials."""
    scope_claim = principal.token_claims.get("scope")
    token_scopes = set(scope_claim.split()) if isinstance(scope_claim, str) else set()
    offline_access_granted = "offline_access" in token_scopes

    expires_at = None
    seconds_remaining = None
    try:
        exp = int(principal.token_claims.get("exp"))
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        seconds_remaining = max(0, int(exp - datetime.now(timezone.utc).timestamp()))
    except (TypeError, ValueError, OverflowError):
        pass

    return {
        "access_token_expires_at": expires_at,
        "access_token_seconds_remaining": seconds_remaining,
        "offline_access_granted": offline_access_granted,
        "refresh_mode": "offline" if offline_access_granted else "online_session",
        "long_running_agent_ready": offline_access_granted,
        "recommended_scope": "offline_access",
        "action_required": None if offline_access_granted else {
            "kind": "reauthorize",
            "scope": "offline_access",
            "reason": "The current OAuth grant can expire during a long-running agent workflow.",
        },
    }


# ---------------------------------------------------------------------------
# Product MCP tools
# ---------------------------------------------------------------------------


def get_mcp_status() -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes([])
        connector = None
        if principal.connector_installation_id:
            connector = (
                db.query(models.McpConnectorInstallation)
                .filter(models.McpConnectorInstallation.id == principal.connector_installation_id)
                .first()
            )
        project = None
        if principal.default_project_id:
            project = db.query(models.Project).filter(models.Project.id == principal.default_project_id).first()
        return {
            "user": {"id": principal.user_id, "email": principal.email},
            "connector": {
                "status": connector.status if connector else principal.connector_status,
                "client_name": connector.client_name if connector else None,
                "scopes": sorted(principal.scopes),
                "default_project": _row(project, ["id", "name", "workspace_id"]) if project else None,
                "last_used_at": connector.last_used_at.isoformat() if connector and connector.last_used_at else None,
            },
            "mcp": {
                "public_base_url": mcp_resource_url(""),
                "product_url": mcp_resource_url("/mcp/"),
                "audit_enabled": True,
            },
            "oauth_session": _oauth_session_status(principal),
        }

    return _handle_tool("get_mcp_status", None, {}, run)


def list_projects() -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        return {
            "projects": [
                {**_project_public_row(item["project"]), "role": item["role"]}
                for item in _accessible_projects(db, principal)
            ]
        }

    return _handle_tool("list_projects", None, {}, run)


def get_project_overview(project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        project = db.query(models.Project).filter(models.Project.id == resolved_project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        return {
            "project": _project_public_row(project),
            "counts": {
                "templates": _safe_count(db, MessagingTemplate, resolved_project_id),
                "contacts": _safe_count(db, MessagingUser, resolved_project_id),
                "funnels": _safe_count(db, models.Funnel, resolved_project_id),
                "chatbots": _safe_count(db, models.Chatbot, resolved_project_id),
                "agent_teams": _safe_count(db, models.AgentTeam, resolved_project_id),
                "markets": _safe_count(db, models.ProjectMarket, resolved_project_id),
                "active_markets": db.query(func.count(models.ProjectMarket.id)).filter(
                    models.ProjectMarket.project_id == resolved_project_id,
                    models.ProjectMarket.status == "active",
                ).scalar() or 0,
            },
        }

    return _handle_tool("get_project_overview", project_id, {"project_id": project_id}, run)


def get_project_onboarding_contract(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Return a tenant-specific, read-only zero-to-one configuration guide."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        project = db.query(models.Project).filter(models.Project.id == resolved_project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        has_lifecycle_read = "versya.lifecycle:read" in principal.scopes
        has_campaigns_read = "versya.campaigns:read" in principal.scopes
        has_imports_read = "versya.imports:read" in principal.scopes
        lifecycle_models = []
        if has_lifecycle_read:
            lifecycle_models = (
                db.query(models.LifecycleModel)
                .filter(models.LifecycleModel.project_id == resolved_project_id)
                .order_by(models.LifecycleModel.version)
                .all()
            )
        counts = {
            "contacts": _safe_count(db, MessagingUser, resolved_project_id),
            "templates": _safe_count(db, MessagingTemplate, resolved_project_id),
            "funnels": _safe_count(db, models.Funnel, resolved_project_id),
            "campaigns": _safe_count(db, models.Campaign, resolved_project_id) if has_campaigns_read else None,
            "campaign_recipes": _safe_count(db, models.CampaignRecipe, resolved_project_id) if has_campaigns_read else None,
            "project_imports": _safe_count(db, models.ProjectImport, resolved_project_id) if has_imports_read else None,
            "lifecycle_models": len(lifecycle_models) if has_lifecycle_read else None,
            "markets": _safe_count(db, models.ProjectMarket, resolved_project_id),
            "active_markets": db.query(func.count(models.ProjectMarket.id)).filter(
                models.ProjectMarket.project_id == resolved_project_id,
                models.ProjectMarket.status == "active",
            ).scalar() or 0,
        }
        model_rows = [
            _row(model, ["id", "version", "name", "status", "contract_version", "created_at"])
            for model in lifecycle_models
        ]
        scope_status = {
            "base_project_inventory": "available",
            "lifecycle": "available" if has_lifecycle_read else "requires versya.lifecycle:read",
            "campaigns": "available" if has_campaigns_read else "requires versya.campaigns:read",
            "imports": "available" if has_imports_read else "requires versya.imports:read",
        }
        return _project_onboarding_contract_document(project, counts, model_rows, scope_status)

    return _handle_tool(
        "get_project_onboarding_contract",
        project_id,
        {"project_id": project_id},
        run,
    )


def get_agent_skill_catalog(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Return the canonical versioned onboarding skill and verified download metadata."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.agent_skill_service import skill_manifest
        return {
            "project_id": resolved,
            "skills": [skill_manifest()],
            "contract_fallback": "get_project_onboarding_contract",
            "external_sends": 0,
        }

    return _handle_tool("get_agent_skill_catalog", project_id, {"project_id": project_id}, run)


def assess_project_setup(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Assess setup readiness without treating restricted inventory as empty."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        project = db.query(models.Project).filter(models.Project.id == resolved).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        counts: Dict[str, Any] = {
            "setup_plans": _safe_count(db, models.ProjectSetupPlan, resolved),
            "contacts": _safe_count(db, MessagingUser, resolved),
            "templates": _safe_count(db, MessagingTemplate, resolved),
            "funnels": _safe_count(db, models.Funnel, resolved),
            "campaigns": None,
            "campaign_recipes": None,
            "lifecycle_models": None,
            "project_imports": None,
            "markets": _safe_count(db, models.ProjectMarket, resolved),
            "active_markets": db.query(func.count(models.ProjectMarket.id)).filter(
                models.ProjectMarket.project_id == resolved,
                models.ProjectMarket.status == "active",
            ).scalar() or 0,
        }
        scope_status = {
            "campaigns": "requires versya.campaigns:read",
            "lifecycle_models": "requires versya.lifecycle:read",
            "project_imports": "requires versya.imports:read",
        }
        if "versya.campaigns:read" in principal.scopes:
            counts["campaigns"] = _safe_count(db, models.Campaign, resolved)
            counts["campaign_recipes"] = _safe_count(db, models.CampaignRecipe, resolved)
            scope_status["campaigns"] = "available"
        if "versya.lifecycle:read" in principal.scopes:
            counts["lifecycle_models"] = _safe_count(db, models.LifecycleModel, resolved)
            scope_status["lifecycle_models"] = "available"
        if "versya.imports:read" in principal.scopes:
            counts["project_imports"] = _safe_count(db, models.ProjectImport, resolved)
            scope_status["project_imports"] = "available"
        from app.services.project_setup_service import ProjectSetupService
        assessment = ProjectSetupService.assessment(project, counts)
        assessment["scope_status"] = scope_status
        assessment["next"] = (
            "Interview the tenant, record explicit decisions, and dry-run create_project_setup_plan."
        )
        return assessment

    return _handle_tool("assess_project_setup", project_id, {"project_id": project_id}, run)


def create_project_setup_plan(
    title: str,
    objective: str,
    project_id: Optional[int] = None,
    phases: Optional[List[Dict[str, Any]]] = None,
    assumptions: Optional[List[str]] = None,
    product_gaps: Optional[List[str]] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create one immutable-version setup proposal; no phase executes automatically."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        project = db.query(models.Project).filter(models.Project.id == resolved).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        from app.services.project_setup_service import (
            ProjectSetupService,
            normalize_plan_data,
            normalize_plan_identity,
        )
        service = ProjectSetupService(db)
        plan_data = normalize_plan_data(phases, assumptions, product_gaps)
        identity = normalize_plan_identity(title, objective)
        assessment = service.assessment(project)
        action_input = {
            "project_id": resolved,
            "title": identity["title"],
            "objective": identity["objective"],
            "plan_data": plan_data,
            "assessment_snapshot": assessment,
        }
        consequence = {
            "creates_plan_version": True,
            "phase_count": len(plan_data["phases"]),
            "executes_modules": False,
            "activates_automations": False,
            "authorizes_sends": False,
            "external_sends": 0,
        }
        if dry_run:
            return {"dry_run": True, "can_create": True, "proposal": action_input, "consequence_at_gate": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "create_project_setup_plan", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "create_project_setup_plan", confirm_token, expected_payload=action_input
        )
        plan = service.create_plan(
            project,
            action_input["title"],
            action_input["objective"],
            plan_data,
            assessment,
            principal.user_id,
        )
        return {"status": "created", "plan": service.payload(plan), "external_sends": 0}

    return _handle_tool(
        "create_project_setup_plan",
        project_id,
        {
            "project_id": project_id,
            "title": title,
            "objective": objective,
            "phases": phases,
            "assumptions": assumptions,
            "product_gaps": product_gaps,
            "dry_run": dry_run,
        },
        run,
    )


def list_project_setup_plans(
    project_id: Optional[int] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """List durable setup-plan versions."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        query = db.query(models.ProjectSetupPlan).filter(models.ProjectSetupPlan.project_id == resolved)
        if status:
            from app.services.project_setup_service import PLAN_STATUSES
            normalized_status = str(status).strip().lower()
            if normalized_status not in PLAN_STATUSES:
                raise ValueError(f"status must be one of {sorted(PLAN_STATUSES)}")
            query = query.filter(models.ProjectSetupPlan.status == normalized_status)
        rows = query.order_by(models.ProjectSetupPlan.version.desc()).all()
        return {
            "project_id": resolved,
            "plans": [
                _row(row, ["id", "project_id", "version", "title", "objective", "status", "fingerprint", "created_at", "updated_at"])
                for row in rows
            ],
            "external_sends": 0,
        }

    return _handle_tool(
        "list_project_setup_plans", project_id, {"project_id": project_id, "status": status}, run
    )


def get_project_setup_plan(plan_id: int, project_id: Optional[int] = None) -> Dict[str, Any]:
    """Read a plan with latest decisions, stale approvals and exact phase gates."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.project_setup_service import ProjectSetupService
        service = ProjectSetupService(db)
        return service.payload(service.get_plan(resolved, plan_id))

    return _handle_tool(
        "get_project_setup_plan", project_id, {"project_id": project_id, "plan_id": plan_id}, run
    )


def record_project_setup_decision(
    plan_id: int,
    decision_key: str,
    evidence_kind: str,
    status: str,
    value: Any,
    rationale: str,
    expected_plan_fingerprint: str,
    project_id: Optional[int] = None,
    sources: Optional[List[Dict[str, Any]]] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Append an evidence-labeled decision revision and invalidate stale approvals."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.services.project_setup_service import ProjectSetupService
        service = ProjectSetupService(db)
        plan = service.get_plan(resolved, plan_id)
        current = service.current_fingerprint(plan)
        if expected_plan_fingerprint != current:
            raise ValueError("Plan fingerprint changed; read the plan and review new decisions before writing")
        action_input = {
            "project_id": resolved,
            "plan_id": plan_id,
            "decision_key": str(decision_key or "").strip().lower(),
            "evidence_kind": str(evidence_kind or "").strip().lower(),
            "status": str(status or "").strip().lower(),
            "value": value,
            "rationale": str(rationale or "").strip(),
            "sources": sources,
            "expected_plan_fingerprint": current,
        }
        # Validate without persisting by using the same semantic constraints explicitly.
        from app.services.project_setup_service import normalize_decision_input
        normalized = normalize_decision_input(
            decision_key, evidence_kind, status, value, rationale, sources
        )
        action_input.update(normalized)
        consequence = {
            "appends_decision_revision": True,
            "changes_plan_fingerprint": True,
            "may_stale_prior_phase_approvals": True,
            "executes_modules": False,
            "external_sends": 0,
        }
        if dry_run:
            return {"dry_run": True, "can_record": True, "decision": action_input, "consequence_at_gate": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "record_project_setup_decision", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "record_project_setup_decision", confirm_token, expected_payload=action_input
        )
        row = service.record_decision(
            plan,
            action_input["decision_key"],
            action_input["evidence_kind"],
            action_input["status"],
            action_input["value"],
            action_input["rationale"],
            action_input["sources"],
            principal.user_id,
            expected_fingerprint=current,
        )
        db.refresh(plan)
        return {
            "status": "recorded",
            "decision": _row(row),
            "plan_fingerprint": service.current_fingerprint(plan),
            "external_sends": 0,
        }

    return _handle_tool(
        "record_project_setup_decision",
        project_id,
        {
            "project_id": project_id,
            "plan_id": plan_id,
            "decision_key": decision_key,
            "evidence_kind": evidence_kind,
            "status": status,
            "value": value,
            "rationale": rationale,
            "sources": sources,
            "expected_plan_fingerprint": expected_plan_fingerprint,
            "dry_run": dry_run,
        },
        run,
    )


def approve_project_setup_phase(
    plan_id: int,
    phase_key: str,
    approval_note: str,
    expected_plan_fingerprint: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Record business approval for one exact phase fingerprint; never execute it."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.project_setup_service import ProjectSetupService
        service = ProjectSetupService(db)
        plan = service.get_plan(resolved, plan_id)
        gate = service.phase_gate(plan, phase_key)
        if expected_plan_fingerprint != gate["current_plan_fingerprint"]:
            raise ValueError("Plan fingerprint changed; read and re-review the phase before approval")
        consequence = {
            **gate,
            "records_business_approval": True,
            "executes_modules": False,
            "authorizes_sends": False,
        }
        if dry_run or not gate["can_approve"]:
            return {"dry_run": True, "consequence_at_gate": consequence}
        action_input = {
            "project_id": resolved,
            "plan_id": plan_id,
            "phase_key": gate["phase"]["key"],
            "approval_note": str(approval_note or "").strip(),
            "expected_plan_fingerprint": gate["current_plan_fingerprint"],
        }
        if not confirm_token:
            pending = create_pending_action(db, principal, "approve_project_setup_phase", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "approve_project_setup_phase", confirm_token, expected_payload=action_input
        )
        approval = service.approve_phase(
            plan,
            phase_key,
            approval_note,
            principal.user_id,
            expected_fingerprint=gate["current_plan_fingerprint"],
        )
        return {
            "status": "approved",
            "approval": _row(approval),
            "execution_authorized": False,
            "next": "Execute the phase only with each specific module tool and its independent gate.",
            "external_sends": 0,
        }

    return _handle_tool(
        "approve_project_setup_phase",
        project_id,
        {
            "project_id": project_id,
            "plan_id": plan_id,
            "phase_key": phase_key,
            "approval_note": approval_note,
            "expected_plan_fingerprint": expected_plan_fingerprint,
            "dry_run": dry_run,
        },
        run,
    )


def compare_project_setup_plans(
    from_plan_id: int,
    to_plan_id: int,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Compare proposal phases and latest decisions between two durable versions."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.project_setup_service import ProjectSetupService
        service = ProjectSetupService(db)
        return service.compare(
            service.get_plan(resolved, from_plan_id),
            service.get_plan(resolved, to_plan_id),
        )

    return _handle_tool(
        "compare_project_setup_plans",
        project_id,
        {"project_id": project_id, "from_plan_id": from_plan_id, "to_plan_id": to_plan_id},
        run,
    )


def set_project_setup_plan_status(
    plan_id: int,
    target_status: str,
    reason: str,
    expected_plan_fingerprint: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Complete, supersede or archive a setup plan without deleting its history."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.project_setup_service import PLAN_STATUSES, ProjectSetupService
        service = ProjectSetupService(db)
        plan = service.get_plan(resolved, plan_id)
        current = service.current_fingerprint(plan)
        target = str(target_status or "").strip().lower()
        if target not in PLAN_STATUSES:
            raise ValueError(f"target_status must be one of {sorted(PLAN_STATUSES)}")
        allowed = {
            "draft": {"in_progress", "superseded", "archived"},
            "in_progress": {"completed", "superseded", "archived"},
            "completed": {"archived"},
            "superseded": {"archived"},
            "archived": set(),
        }
        if target not in allowed.get(plan.status, set()):
            raise ValueError(f"Cannot move a setup plan from {plan.status} to {target}")
        if expected_plan_fingerprint != current:
            raise ValueError("Plan fingerprint changed; read the plan before changing status")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("reason is required")
        action_input = {
            "project_id": resolved,
            "plan_id": plan_id,
            "from_status": plan.status,
            "target_status": target,
            "reason": normalized_reason,
            "expected_plan_fingerprint": current,
        }
        consequence = {
            "changes_plan_status": True,
            "deletes_history": False,
            "executes_modules": False,
            "external_sends": 0,
        }
        if dry_run:
            return {"dry_run": True, "can_update": True, "change": action_input, "consequence_at_gate": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "set_project_setup_plan_status", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "set_project_setup_plan_status", confirm_token, expected_payload=action_input
        )
        plan.status = target
        snapshot = dict(plan.assessment_snapshot or {})
        snapshot["last_status_reason"] = normalized_reason
        plan.assessment_snapshot = snapshot
        db.commit()
        db.refresh(plan)
        return {"status": "updated", "plan": service.payload(plan), "external_sends": 0}

    return _handle_tool(
        "set_project_setup_plan_status",
        project_id,
        {
            "project_id": project_id,
            "plan_id": plan_id,
            "target_status": target_status,
            "reason": reason,
            "expected_plan_fingerprint": expected_plan_fingerprint,
            "dry_run": dry_run,
        },
        run,
    )


def configure_project_foundation(
    settings: Dict[str, Any],
    reason: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Configure market, locale, timezone and factual goal semantics through one exact diff."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        project = db.query(models.Project).filter(models.Project.id == resolved).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        from app.services.project_foundation import apply_foundation, normalize_foundation_update
        from app.services.project_market_service import ProjectMarketService
        report = normalize_foundation_update(project, settings)
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("reason is required")
        action_input = {
            "project_id": resolved,
            "settings": report["after"],
            "changes": report["changes"],
            "reason": normalized_reason,
        }
        consequence = {
            "changes": report["changes"],
            "reclassifies_contacts": False,
            "activates_automations": False,
            "authorizes_sends": False,
            "external_sends": 0,
        }
        if not report["changes"]:
            return {"dry_run": dry_run, "status": "no_change", "foundation": report["after"], "consequence_at_gate": consequence}
        if dry_run:
            return {"dry_run": True, "can_apply": True, "foundation": report, "consequence_at_gate": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "configure_project_foundation", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "configure_project_foundation", confirm_token, expected_payload=action_input
        )
        apply_foundation(project, report["after"])
        ProjectMarketService(db).replace_from_config(project, report["after"]["market_config"])
        db.commit()
        db.refresh(project)
        return {
            "status": "configured",
            "project": _project_public_row(project),
            "consequence_at_gate": consequence,
            "external_sends": 0,
        }

    return _handle_tool(
        "configure_project_foundation",
        project_id,
        {"project_id": project_id, "settings": settings, "reason": reason, "dry_run": dry_run},
        run,
    )


def get_project_market_contract(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Explain first-class multi-market semantics and the safe agent workflow."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.project_market_service import ProjectMarketService
        return {"project_id": resolved, **ProjectMarketService.contract()}

    return _handle_tool(
        "get_project_market_contract", project_id, {"project_id": project_id}, run
    )


def list_project_markets(
    project_id: Optional[int] = None,
    include_archived: bool = False,
    after_key: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """List project markets by stable-key cursor; page size is not a project limit."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.project_market_service import ProjectMarketService
        return ProjectMarketService(db).list_page(
            resolved,
            include_archived=include_archived,
            after_key=after_key,
            limit=limit,
        )

    return _handle_tool(
        "list_project_markets",
        project_id,
        {
            "project_id": project_id,
            "include_archived": include_archived,
            "after_key": after_key,
            "limit": limit,
        },
        run,
    )


def create_project_market(
    market: Dict[str, Any],
    reason: str,
    project_id: Optional[int] = None,
    make_default: bool = False,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create one market without reclassifying contacts or enabling sends."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.project_market_service import ProjectMarketService, market_payload
        service = ProjectMarketService(db)
        proposal = service.preview_create(resolved, market, make_default)
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("reason is required")
        action_input = {
            "project_id": resolved,
            "market": proposal,
            "reason": normalized_reason,
        }
        consequence = {
            "creates_market": proposal["key"],
            "changes_default": proposal["is_default"],
            "reclassifies_contacts": False,
            "activates_automations": False,
            "external_sends": 0,
        }
        if dry_run:
            return {"dry_run": True, "can_create": True, "proposal": proposal, "consequence_at_gate": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "create_project_market", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(db, principal, "create_project_market", confirm_token, expected_payload=action_input)
        row = service.create(resolved, proposal, proposal["is_default"])
        db.commit()
        db.refresh(row)
        return {"status": "created", "market": market_payload(row), "consequence_at_gate": consequence}

    return _handle_tool(
        "create_project_market",
        project_id,
        {"project_id": project_id, "market": market, "make_default": make_default, "reason": reason, "dry_run": dry_run},
        run,
    )


def update_project_market(
    market_key: str,
    changes: Dict[str, Any],
    reason: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Update mutable market semantics while preserving its stable key."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.project_market_service import ProjectMarketService, market_payload
        service = ProjectMarketService(db)
        report = service.preview_update(resolved, market_key, changes)
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("reason is required")
        action_input = {
            "project_id": resolved,
            "market_key": str(market_key or "").strip().lower(),
            "changes": report["changes"],
            "reason": normalized_reason,
        }
        consequence = {
            "changes": report["changes"],
            "reclassifies_contacts": False,
            "activates_automations": False,
            "external_sends": 0,
        }
        if not report["changes"]:
            return {"dry_run": dry_run, "status": "no_change", "market": report["after"], "consequence_at_gate": consequence}
        if dry_run:
            return {"dry_run": True, "can_update": True, "market": report, "consequence_at_gate": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "update_project_market", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(db, principal, "update_project_market", confirm_token, expected_payload=action_input)
        row = service.update(resolved, market_key, changes)
        db.commit()
        db.refresh(row)
        return {"status": "updated", "market": market_payload(row), "consequence_at_gate": consequence}

    return _handle_tool(
        "update_project_market",
        project_id,
        {"project_id": project_id, "market_key": market_key, "changes": changes, "reason": reason, "dry_run": dry_run},
        run,
    )


def set_default_project_market(
    market_key: str,
    reason: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Move the project fallback to another active market."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.project_market_service import ProjectMarketService, market_payload
        service = ProjectMarketService(db)
        row = service.get(resolved, market_key)
        if not row:
            raise ValueError("Market not found")
        if row.status != "active":
            raise ValueError("Only an active market can be default")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("reason is required")
        action_input = {
            "project_id": resolved,
            "market_key": row.key,
            "from_default": next((item.key for item in row.project.markets if item.is_default), None),
            "reason": normalized_reason,
        }
        consequence = {"changes_default_market": not row.is_default, "external_sends": 0}
        if row.is_default:
            return {"dry_run": dry_run, "status": "no_change", "market": market_payload(row), "consequence_at_gate": consequence}
        if dry_run:
            return {"dry_run": True, "can_update": True, "change": action_input, "consequence_at_gate": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "set_default_project_market", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(db, principal, "set_default_project_market", confirm_token, expected_payload=action_input)
        row = service.set_default(resolved, row.key)
        db.commit()
        db.refresh(row)
        return {"status": "updated", "market": market_payload(row), "consequence_at_gate": consequence}

    return _handle_tool(
        "set_default_project_market",
        project_id,
        {"project_id": project_id, "market_key": market_key, "reason": reason, "dry_run": dry_run},
        run,
    )


def set_project_market_status(
    market_key: str,
    status: str,
    reason: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Archive or restore a market without deleting its identity or history."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.project_market_service import MARKET_STATUSES, ProjectMarketService, market_payload
        service = ProjectMarketService(db)
        row = service.get(resolved, market_key)
        if not row:
            raise ValueError("Market not found")
        target = str(status or "").strip().lower()
        if target not in MARKET_STATUSES:
            raise ValueError(f"status must be one of {sorted(MARKET_STATUSES)}")
        if target == "archived" and row.is_default:
            another_active = db.query(models.ProjectMarket).filter(
                models.ProjectMarket.project_id == resolved,
                models.ProjectMarket.status == "active",
                models.ProjectMarket.id != row.id,
            ).first()
            if another_active:
                raise ValueError("Set another default market before archiving this one")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("reason is required")
        action_input = {
            "project_id": resolved,
            "market_key": row.key,
            "from_status": row.status,
            "target_status": target,
            "reason": normalized_reason,
        }
        consequence = {
            "changes_market_status": row.status != target,
            "deletes_market": False,
            "reclassifies_contacts": False,
            "external_sends": 0,
        }
        if row.status == target:
            return {"dry_run": dry_run, "status": "no_change", "market": market_payload(row), "consequence_at_gate": consequence}
        if dry_run:
            return {"dry_run": True, "can_update": True, "change": action_input, "consequence_at_gate": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "set_project_market_status", action_input, project_id=resolved)
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(db, principal, "set_project_market_status", confirm_token, expected_payload=action_input)
        row = service.set_status(resolved, row.key, target)
        db.commit()
        db.refresh(row)
        return {"status": "updated", "market": market_payload(row), "consequence_at_gate": consequence}

    return _handle_tool(
        "set_project_market_status",
        project_id,
        {"project_id": project_id, "market_key": market_key, "status": status, "reason": reason, "dry_run": dry_run},
        run,
    )


def get_event_ingestion_contract(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Return the tenant-safe continuous profile/event ingestion contract."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.projects:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.event_ingestion_contract import event_ingestion_contract_document
        return event_ingestion_contract_document()

    return _handle_tool(
        "get_event_ingestion_contract",
        project_id,
        {"project_id": project_id},
        run,
    )


def get_event_ingestion_status(
    project_id: Optional[int] = None,
    hours: int = 24,
) -> Dict[str, Any]:
    """Inspect live ingress and lifecycle convergence without privileged access."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes([
            "versya.projects:read",
            "versya.ingestion:read",
            "versya.lifecycle:read",
        ])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from datetime import timedelta
        from app.models import ContactPosition
        from app.models.messaging import MessagingApiKey
        from app.models.project_import import LifecycleModel

        bounded_hours = max(1, min(int(hours), 24 * 30))
        cutoff = datetime.utcnow() - timedelta(hours=bounded_hours)
        recent_contact_ids = {
            int(row[0]) for row in db.query(MessagingUser.id).filter(
                MessagingUser.project_id == resolved,
                MessagingUser.status == "active",
                MessagingUser.is_sandbox == False,  # noqa: E712
                MessagingUser.created_at >= cutoff,
            ).all()
        }
        event_query = db.query(MessagingEvent).filter(
            MessagingEvent.project_id == resolved,
            MessagingEvent.source == "backend",
            MessagingEvent.created_at >= cutoff,
        )
        total_events = event_query.count()
        events_with_stable_id = event_query.filter(
            MessagingEvent.external_event_id.isnot(None),
        ).count()
        latest_event = event_query.order_by(MessagingEvent.created_at.desc()).first()
        keys = db.query(MessagingApiKey).filter(
            MessagingApiKey.project_id == resolved,
        ).order_by(MessagingApiKey.created_at.desc()).all()
        key_rows = [
            {
                "id": key.id,
                "name": key.name,
                "key_prefix": key.key_prefix,
                "permissions": key.permissions or [],
                "is_active": key.is_active,
                "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
                "rate_limit_per_minute": key.rate_limit_per_minute,
                "rate_limit_per_day": key.rate_limit_per_day,
                "has_ip_allowlist": bool(key.allowed_ips),
            }
            for key in keys
        ]
        models = db.query(LifecycleModel).filter(
            LifecycleModel.project_id == resolved,
            LifecycleModel.status.in_(["active", "shadow"]),
        ).order_by(LifecycleModel.version).all()
        lifecycle = []
        for model in models:
            positioned = 0
            if recent_contact_ids:
                positioned = db.query(ContactPosition.id).filter(
                    ContactPosition.project_id == resolved,
                    ContactPosition.lifecycle_model_id == model.id,
                    ContactPosition.user_id.in_(recent_contact_ids),
                ).count()
            lifecycle.append({
                "model_id": model.id,
                "version": model.version,
                "name": model.name,
                "status": model.status,
                "recent_contacts": len(recent_contact_ids),
                "positioned_recent_contacts": positioned,
                "missing_recent_positions": len(recent_contact_ids) - positioned,
            })

        blockers = []
        if not any(
            key["is_active"] and {"identify", "track"}.issubset(set(key["permissions"]))
            for key in key_rows
        ):
            blockers.append({
                "code": "no_active_identify_track_key",
                "message": "No active backend key has both identify and track permissions.",
            })
        if any(item["missing_recent_positions"] for item in lifecycle):
            blockers.append({
                "code": "recent_contacts_missing_positions",
                "message": "At least one active/shadow lifecycle model has not materialized every recent contact.",
            })
        warnings = []
        if total_events > events_with_stable_id:
            warnings.append({
                "code": "events_without_stable_id",
                "count": total_events - events_with_stable_id,
                "message": "Backend events without event_id cannot provide source-level idempotent retry evidence.",
            })
        return {
            "project_id": resolved,
            "window_hours": bounded_hours,
            "ready": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "api_keys": key_rows,
            "backend_events": {
                "total": total_events,
                "with_stable_event_id": events_with_stable_id,
                "without_stable_event_id": total_events - events_with_stable_id,
                "latest_at": latest_event.created_at.isoformat() if latest_event else None,
            },
            "recent_contacts": len(recent_contact_ids),
            "lifecycle": lifecycle,
        }

    return _handle_tool(
        "get_event_ingestion_status",
        project_id,
        {"project_id": project_id, "hours": hours},
        run,
    )


def create_event_ingestion_key(
    name: str,
    project_id: Optional[int] = None,
    rate_limit_per_minute: int = 1000,
    rate_limit_per_day: int = 100000,
    allowed_ips: Optional[List[str]] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Provision a least-privilege identify/track key; its secret is shown once."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.ingestion:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.models.messaging import MessagingApiKey
        from app.services.messaging import key_generator

        normalized_name = str(name or "").strip()
        if not normalized_name or len(normalized_name) > 255:
            raise ValueError("name must contain 1 to 255 characters")
        per_minute = max(1, min(int(rate_limit_per_minute), 100000))
        per_day = max(per_minute, min(int(rate_limit_per_day), 10_000_000))
        ips = sorted({str(value).strip() for value in (allowed_ips or []) if str(value).strip()})
        action_input = {
            "project_id": resolved,
            "name": normalized_name,
            "permissions": ["identify", "track"],
            "rate_limit_per_minute": per_minute,
            "rate_limit_per_day": per_day,
            "allowed_ips": ips,
        }
        if dry_run:
            return {
                "dry_run": True,
                "can_create": True,
                "key": action_input,
                "external_sends": 0,
                "secret_returned": False,
                "next": "Repeat with dry_run=false, review confirmation, then repeat with confirm_token.",
            }
        if not confirm_token:
            pending = create_pending_action(
                db,
                principal,
                "create_event_ingestion_key",
                action_input,
                project_id=resolved,
            )
            return {
                "status": "pending_confirmation",
                "key": action_input,
                "external_sends": 0,
                **pending,
            }
        consume_pending_action(
            db,
            principal,
            "create_event_ingestion_key",
            confirm_token,
            expected_payload=action_input,
        )
        secret_key, key_hash, key_prefix = key_generator.generate_secret_key()
        key = MessagingApiKey(
            project_id=resolved,
            name=normalized_name,
            secret_key_hash=key_hash,
            key_prefix=key_prefix,
            permissions=["identify", "track"],
            rate_limit_per_minute=per_minute,
            rate_limit_per_day=per_day,
            allowed_ips=ips or None,
            is_active=True,
        )
        db.add(key)
        db.commit()
        db.refresh(key)
        return {
            "status": "created",
            "key": {
                "id": key.id,
                "project_id": key.project_id,
                "name": key.name,
                "key_prefix": key.key_prefix,
                "permissions": key.permissions,
                "rate_limit_per_minute": key.rate_limit_per_minute,
                "rate_limit_per_day": key.rate_limit_per_day,
                "has_ip_allowlist": bool(key.allowed_ips),
            },
            "credential": {
                "secret_key": secret_key,
                "shown_once": True,
                "handling": "Store immediately in the source secret manager; do not echo or log it.",
            },
            "external_sends": 0,
        }

    return _handle_tool(
        "create_event_ingestion_key",
        project_id,
        {
            "project_id": project_id,
            "name": name,
            "rate_limit_per_minute": rate_limit_per_minute,
            "rate_limit_per_day": rate_limit_per_day,
            "allowed_ips": allowed_ips or [],
            "dry_run": dry_run,
        },
        run,
    )


def get_project_import_contract(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Return the self-describing Project Import contract and exact agent workflow."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.imports:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.project_import_service import contract_document
        return contract_document()

    return _handle_tool("get_project_import_contract", project_id, {"project_id": project_id}, run)


def create_project_import(data: Dict[str, Any], project_id: Optional[int] = None) -> Dict[str, Any]:
    """Create/resume an idempotent import and return an agent-ready binary upload request."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.imports:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.schemas.project_import import ProjectImportCreate
        from app.services.project_import_service import ProjectImportService
        payload = ProjectImportCreate(**data)
        service = ProjectImportService(db)
        row = service.create(resolved, payload.model_dump(), principal.user_id)
        raw_path = f"/api/v1/projects/{resolved}/project-imports/{row.id}/bundle/raw"
        multipart_path = f"/api/v1/projects/{resolved}/project-imports/{row.id}/bundle"
        upload = None
        if row.status in {"created", "uploading", "uploaded", "blocked", "failed"}:
            grant, upload_token = service.issue_upload_grant(row, principal.user_id)
            upload = {
                "method": "PUT",
                "raw_url": mcp_resource_url(raw_path),
                "raw_path": raw_path,
                "headers": {
                    "Content-Type": "application/zip",
                    "X-Versya-Upload-Token": upload_token,
                },
                "body": "exact ZIP bytes; do not base64 encode",
                "max_bytes": 100 * 1024 * 1024,
                "one_time": True,
                "expires_at": grant.expires_at.isoformat(),
                "multipart_ui_url": mcp_resource_url(multipart_path),
                "multipart_field": "file",
            }
        return {
            "import": _row(row),
            "upload": upload,
            "next_actions": service.next_actions(row),
        }

    return _handle_tool("create_project_import", project_id, {"project_id": project_id, "data": data}, run)


def list_project_imports(project_id: Optional[int] = None, limit: int = 50) -> Dict[str, Any]:
    """List recent imports so an agent can resume an interrupted migration without SQL or UI."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.imports:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.models.project_import import ProjectImport
        from app.services.project_import_service import ProjectImportService
        service = ProjectImportService(db)
        rows = (
            db.query(ProjectImport)
            .filter(ProjectImport.project_id == resolved)
            .order_by(ProjectImport.created_at.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {
            "imports": [
                {**_row(row), "next_actions": service.next_actions(row)}
                for row in rows
            ]
        }

    return _handle_tool(
        "list_project_imports", project_id,
        {"project_id": project_id, "limit": limit}, run,
    )


def get_project_import(import_id: str, project_id: Optional[int] = None) -> Dict[str, Any]:
    """Get durable import state and the next safe agent action; use this for polling."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.imports:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.project_import_service import ProjectImportService
        service = ProjectImportService(db)
        row = service.get(resolved, import_id)
        return {"import": _row(row), "next_actions": service.next_actions(row)}

    return _handle_tool("get_project_import", project_id, {"project_id": project_id, "import_id": import_id}, run)


def validate_project_import(import_id: str, project_id: Optional[int] = None) -> Dict[str, Any]:
    """Validate the bundle against current project identity and return actionable grouped findings."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.imports:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.services.project_import_service import ProjectImportService
        service = ProjectImportService(db)
        row = service.get(resolved, import_id)
        return service.preflight(row)

    return _handle_tool("validate_project_import", project_id, {"project_id": project_id, "import_id": import_id}, run)


def apply_project_import(
    import_id: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Revalidate and safely enqueue an import; approval is bound to checksum and findings."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.imports:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.project_import_service import ProjectImportService
        service = ProjectImportService(db)
        row = service.get(resolved, import_id)
        if row.status == "completed":
            return {"dry_run": dry_run, "status": "completed", "audit": service.audit(row)}
        if row.status in {"queued", "applying", "reconciling"}:
            return {
                "dry_run": dry_run,
                "status": row.status,
                "import_id": row.id,
                "next_actions": service.next_actions(row),
            }
        preflight = service.preflight(row)
        action_input = {
            "project_id": resolved,
            "import_id": import_id,
            "bundle_checksum": preflight.get("bundle_checksum"),
            "validation_fingerprint": preflight.get("validation_fingerprint"),
            "safety_gates": preflight.get("gates"),
        }
        if dry_run:
            return {"dry_run": True, "preflight": preflight}
        if not preflight.get("can_apply"):
            return {
                "dry_run": False,
                "status": "blocked",
                "preflight": preflight,
                "next": "Correct every blocking finding and validate a corrected bundle.",
            }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "apply_project_import", action_input, project_id=resolved,
            )
            return {
                "status": "pending_confirmation",
                "preflight": preflight,
                "approval_binding": action_input,
                **pending,
            }
        consume_pending_action(
            db,
            principal,
            "apply_project_import",
            confirm_token,
            expected_payload=action_input,
        )
        queued = service.enqueue(row, principal.user_id)
        return {
            "dry_run": False,
            "status": queued.status,
            "import_id": queued.id,
            "apply_requested_at": queued.apply_requested_at.isoformat() if queued.apply_requested_at else None,
            "poll_after_seconds": 5,
            "next": "Poll get_project_import until terminal, then call audit_project_import.",
        }

    return _handle_tool(
        "apply_project_import", project_id,
        {"project_id": project_id, "import_id": import_id, "dry_run": dry_run}, run,
    )


def audit_project_import(
    import_id: str,
    project_id: Optional[int] = None,
    issue_limit: int = 100,
) -> Dict[str, Any]:
    """Evaluate machine-checkable acceptance gates and summarize the reconciliation ledger."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.imports:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.project_import_service import ProjectImportService
        service = ProjectImportService(db)
        return service.audit(
            service.get(resolved, import_id),
            issue_limit=max(1, min(issue_limit, 500)),
        )

    return _handle_tool(
        "audit_project_import", project_id,
        {"project_id": project_id, "import_id": import_id, "issue_limit": issue_limit}, run,
    )


def list_project_import_records(
    import_id: str,
    project_id: Optional[int] = None,
    record_type: Optional[str] = None,
    status: Optional[str] = None,
    offset: int = 0,
    limit: int = 100,
) -> Dict[str, Any]:
    """Page through source-to-target ledger mappings for reconciliation without database access."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.imports:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.project_import_service import ProjectImportService
        service = ProjectImportService(db)
        return service.list_records(
            service.get(resolved, import_id),
            record_type=record_type,
            status=status,
            offset=max(0, offset),
            limit=max(1, min(limit, 500)),
        )

    return _handle_tool(
        "list_project_import_records", project_id,
        {
            "project_id": project_id,
            "import_id": import_id,
            "record_type": record_type,
            "status": status,
            "offset": offset,
            "limit": limit,
        },
        run,
    )


def list_lifecycle_models(project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.lifecycle_model_service import LifecycleModelService
        return {"models": [_row(item) for item in LifecycleModelService(db).list(resolved)]}

    return _handle_tool("list_lifecycle_models", project_id, {"project_id": project_id}, run)


def get_lifecycle_model_contract(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Explain canonical lifecycle semantics and the tenant authoring interview."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.lifecycle_model_service import lifecycle_contract_document
        return lifecycle_contract_document()

    return _handle_tool("get_lifecycle_model_contract", project_id, {"project_id": project_id}, run)


def validate_lifecycle_model_definition(
    definition: Dict[str, Any],
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate a proposed project taxonomy without persisting a model."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.lifecycle_model_service import validate_lifecycle_definition
        report = validate_lifecycle_definition(definition)
        return {
            "project_id": resolved,
            "report": report,
            "next": (
                "Create the model only after every blocking error is fixed."
                if not report["valid"] else
                "Call create_lifecycle_model with dry_run=true, review the checksum, then confirm persistence."
            ),
        }

    return _handle_tool(
        "validate_lifecycle_model_definition", project_id,
        {"project_id": project_id, "definition": definition}, run,
    )


def create_lifecycle_model(
    name: str,
    definition: Dict[str, Any],
    requested_status: str = "validated",
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist an immutable lifecycle version after validation and confirmation."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        if requested_status not in {"draft", "validated"}:
            raise ValueError("requested_status must be draft or validated")
        if not str(name or "").strip():
            raise ValueError("name is required")
        from app.models.project_import import LifecycleModel
        from app.services.lifecycle_model_service import (
            LifecycleModelService,
            canonical_checksum,
            validate_lifecycle_definition,
        )
        report = validate_lifecycle_definition(definition)
        checksum = canonical_checksum(definition)
        existing = db.query(LifecycleModel).filter(
            LifecycleModel.project_id == resolved,
            LifecycleModel.checksum == checksum,
        ).order_by(LifecycleModel.version).first()
        if existing:
            return {
                "dry_run": dry_run,
                "status": "existing_lifecycle_checksum",
                "model": _row(existing),
                "validation": report,
            }
        next_version = (db.query(func.max(LifecycleModel.version)).filter(
            LifecycleModel.project_id == resolved,
        ).scalar() or 0) + 1
        consequence = {
            "project_id": resolved,
            "name": str(name).strip(),
            "next_version": next_version,
            "requested_status": requested_status,
            "checksum": checksum,
            "validation": report,
            "external_sends": 0,
            "activates_model": False,
        }
        if dry_run or not report["valid"]:
            return {"dry_run": True, "can_create": report["valid"], "consequence": consequence}
        action_input = {
            "project_id": resolved,
            "name": str(name).strip(),
            "requested_status": requested_status,
            "definition_checksum": checksum,
            "next_version": next_version,
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "create_lifecycle_model", action_input, project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "create_lifecycle_model", confirm_token,
            expected_payload=action_input,
        )
        model = LifecycleModelService(db).create(
            resolved,
            name=str(name).strip(),
            definition=definition,
            actor_user_id=principal.user_id,
            requested_status=requested_status,
        )
        db.commit()
        db.refresh(model)
        return {"dry_run": False, "status": "created", "model": _row(model)}

    return _handle_tool(
        "create_lifecycle_model", project_id,
        {
            "project_id": project_id,
            "name": name,
            "requested_status": requested_status,
            "dry_run": dry_run,
            "definition": definition,
        },
        run,
    )


def get_lifecycle_distribution(
    model_id: int,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return materialized coverage and counts by Type, Stage and Age."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.lifecycle_model_service import LifecycleModelService
        service = LifecycleModelService(db)
        return service.distribution(service.get(resolved, model_id))

    return _handle_tool(
        "get_lifecycle_distribution", project_id,
        {"project_id": project_id, "model_id": model_id}, run,
    )


def compare_lifecycle_materialization(
    model_id: int,
    project_id: Optional[int] = None,
    example_limit: int = 100,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare stored Positions with a fresh calculation from current facts."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.lifecycle_model_service import LifecycleModelService
        service = LifecycleModelService(db)
        parsed_as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00")).replace(tzinfo=None) if as_of else None
        return service.compare_materialized(
            service.get(resolved, model_id),
            as_of=parsed_as_of,
            example_limit=max(1, min(example_limit, 500)),
        )

    return _handle_tool(
        "compare_lifecycle_materialization", project_id,
        {"project_id": project_id, "model_id": model_id, "example_limit": example_limit, "as_of": as_of}, run,
    )


def explain_contact_lifecycle(
    model_id: int,
    contact_id: Optional[int] = None,
    external_id: Optional[str] = None,
    project_id: Optional[int] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Explain one contact's stored and freshly computed Position."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:read", "versya.contacts:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        if (contact_id is None) == (external_id is None):
            raise ValueError("Provide exactly one of contact_id or external_id")
        query = db.query(MessagingUser).filter(MessagingUser.project_id == resolved)
        user = query.filter(
            MessagingUser.id == contact_id if contact_id is not None else MessagingUser.external_id == external_id,
        ).first()
        if not user:
            raise HTTPException(status_code=404, detail="Contact not found")
        from app.services.lifecycle_model_service import LifecycleModelService
        service = LifecycleModelService(db)
        parsed_as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00")).replace(tzinfo=None) if as_of else None
        return service.explain_contact(service.get(resolved, model_id), user, as_of=parsed_as_of)

    return _handle_tool(
        "explain_contact_lifecycle", project_id,
        {
            "project_id": project_id,
            "model_id": model_id,
            "contact_id": contact_id,
            "external_id": external_id,
            "as_of": as_of,
        },
        run,
    )


def compare_lifecycle_models(
    left_model_id: int,
    right_model_id: int,
    project_id: Optional[int] = None,
    sample_limit: int = 250,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.lifecycle_model_service import LifecycleModelService
        service = LifecycleModelService(db)
        parsed_as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00")).replace(tzinfo=None) if as_of else None
        return service.compare(
            service.get(resolved, left_model_id), service.get(resolved, right_model_id),
            sample_limit=max(1, min(sample_limit, 1000)), as_of=parsed_as_of,
        )

    return _handle_tool(
        "compare_lifecycle_models", project_id,
        {"project_id": project_id, "left_model_id": left_model_id, "right_model_id": right_model_id, "sample_limit": sample_limit, "as_of": as_of}, run,
    )


def activate_lifecycle_model(
    model_id: int,
    target_status: str,
    reason: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.lifecycle_model_service import LifecycleModelService
        service = LifecycleModelService(db)
        model = service.get(resolved, model_id)
        report = service.validate(model)
        consequence = {
            "model_id": model.id,
            "target_status": target_status,
            "validation": report,
            "transition_events_on_activation": 0,
            "external_sends": 0,
        }
        action_input = {"project_id": resolved, "model_id": model_id, "target_status": target_status, "reason": reason}
        if dry_run:
            db.rollback()
            return {"dry_run": True, "consequence": consequence}
        if not confirm_token:
            db.rollback()
            pending = create_pending_action(
                db,
                principal,
                "activate_lifecycle_model",
                action_input,
                project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "activate_lifecycle_model", confirm_token,
            expected_payload=action_input,
        )
        result = service.activate(model, target_status=target_status, actor_user_id=principal.user_id)
        db.commit()
        return {"dry_run": False, **result, "reason": reason}

    return _handle_tool(
        "activate_lifecycle_model", project_id,
        {"project_id": project_id, "model_id": model_id, "target_status": target_status, "reason": reason, "dry_run": dry_run}, run,
    )


def list_orchestration_cutovers(project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.models.project_import import ProjectLifecycleCutover
        rows = db.query(ProjectLifecycleCutover).filter(ProjectLifecycleCutover.project_id == resolved).order_by(ProjectLifecycleCutover.purpose_key).all()
        return {"cutovers": [_row(item) for item in rows]}

    return _handle_tool("list_orchestration_cutovers", project_id, {"project_id": project_id}, run)


def get_orchestration_contract(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Explain purpose ownership independently from channels and content."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        return {
            "project_id": resolved,
            "purpose_key": {
                "definition": (
                    "A stable project-scoped key for one business intent and orchestration owner. "
                    "It is not a channel, locale, template, campaign or implementation identifier."
                ),
                "examples": ["trial.activation", "checkout.recovery", "renewal.reminder", "aha.readiness_reminder"],
                "rule": "All channel, locale and template variants serving the same objective share one purpose_key.",
                "syntax": "^[a-z0-9][a-z0-9._-]{0,119}$",
            },
            "purpose_brief": [
                "business objective and measurable success event",
                "eligibility trigger and lifecycle audience",
                "stop conditions and source-of-truth facts",
                "transactional or promotional lane",
                "attention scope and authored ordering against overlapping episodes",
                "episode mode: fact_only, interrupt, or bounded episode",
                "validity, resolve conditions, and resume/recompute policy",
                "consent/permission requirement",
                "cooldown, caps, channels and locale/template variants",
            ],
            "attention_model": {
                "base": (
                    "Type/Stage/Age cells are standing candidates computed from current facts and clocks. "
                    "Age is derived only from type_entered_at, not maintained as a mutable daily counter; "
                    "time in Stage or since another fact is a separate, explicitly named clock."
                ),
                "episode": (
                    "A funnel enrollment or decision-producing Event Action is bounded by relevance, "
                    "resolve/stop facts and optional expiry, and carries an authored attention ordinal."
                ),
                "preemption": (
                    "The highest relevant episode occludes lower episodes and Base candidates. When it ends, "
                    "Selection recomputes every remaining candidate; it never blindly sends a previously hidden message."
                ),
                "event_modes": {
                    "fact_only": "Updates source truth and creates no attention claim or outbound candidate.",
                    "interrupt": "Creates one action or a short-lived episode with a narrow validity window.",
                    "episode": "Owns its attention scope until resolve, stop, expiry or explicit exit.",
                },
                "ordering": (
                    "Non-equivalent overlapping episodes require an explicit tenant ordering policy. "
                    "Arrival order and worker execution order never determine business priority."
                ),
                "activation_gate": {
                    "invariant": (
                        "Every new or materially changed funnel, campaign, decision-producing Event Action, "
                        "or event-triggered template "
                        "must preview its effects on existing episodes before activation. Priority chooses attention; "
                        "it never implies that a losing episode exits."
                    ),
                    "entry_effects": {
                        "occlude": "Keep the lower episode live; re-evaluate it after fall-through.",
                        "suspend": "Keep it live but pause the clocks/actions explicitly named by policy.",
                        "exit": "Resolve and supersede only the declared incompatible episode family.",
                        "reject_entry": "Reject the new enrollment while an incompatible episode owns the scope.",
                        "coexist": "Keep both live because their scopes/effects are compatible.",
                    },
                    "required_attention_policy": {
                        "attention_scope": "Collision domain, normally contact.promotional for nurture and offers.",
                        "ordinal": "Tenant-authored order; higher owns attention. It is copied to durable send candidates.",
                        "ordinal_reason": "Human-readable business justification for the order.",
                        "entry_effect": "What this episode requests when it enters against declared targets.",
                        "exclusive_group": "Episodes in the same group are treated as explicitly related.",
                        "target_purpose_keys": "Existing purpose owners this entrant may affect.",
                        "target_exclusive_groups": "Existing episode families this entrant may affect.",
                        "allowed_incoming_effects": (
                            "Effects this episode permits future entrants to apply to it. Default: occlude and coexist; "
                            "exit/suspend require explicit consent from the existing episode."
                        ),
                        "allow_start_occluded": "Whether this episode may start below an already-higher owner.",
                        "tie_policy": "Both sides must agree on perishability or bounded_learning; otherwise order is required.",
                        "occluded_clock": "wall_clock is implemented; active_attention is blocked until clock suspension exists.",
                        "missed_window": "expire is implemented across all sources; other choices are currently blocked.",
                        "future_reservation": (
                            "Tenant opt-in for a known future intent to hold a lower-ranked due intent inside "
                            "the project's bounded planning window. Requires Future Plan in enforce."
                        ),
                    },
                    "impact_preview": [
                        "overlapping triggers, audiences, purposes, opportunity windows and attention scopes",
                        "current enrollments, runs, candidates and pending sends affected by each entry effect",
                        "the next attention owner after every proposed exit or expiry",
                        "unsupported runtime effects and unresolved relationships",
                        "zero external sends and a definition-bound impact fingerprint",
                        "consequence_at_gate for every affected asset and known contact intent",
                    ],
                    "activation_rule": (
                        "Block when an overlap has no explicit order, either side disagrees with a destructive effect, "
                        "the runtime cannot enforce the chosen effect, an active definition is edited in place, or the "
                        "reviewed definition/impact fingerprint changed before confirmation."
                    ),
                    "source_registration_gate": {
                        "rule": (
                            "Every outbound caller must use a platform-registered source_type. Unknown sources fail "
                            "before provider I/O; promotional automations also fail unless Candidate and Selection "
                            "are both enforce and the source resolves to an enabled authored definition."
                        ),
                        "registered_attention_sources": [
                            "campaign", "event_action", "funnel", "template",
                        ],
                        "template_rule": (
                            "is_active makes content available; automation_enabled is a separate, gated state. "
                            "Create/import/update never enables event execution."
                        ),
                        "agent_sequence": [
                            "author trigger_events, purpose_key and attention_policy while disabled",
                            "call preview_orchestration_impact with asset_kind=template",
                            "resolve blockers and request the exact-impact confirmation",
                            "call activate_template_automation",
                        ],
                    },
                },
                "stack_semantics": (
                    "The apparent stack is emergent Selection behavior over durable candidates, not a LIFO queue. "
                    "An occluded intermediate episode resumes only if it remains eligible and unexpired."
                ),
                "fall_through": {
                    "rule": (
                        "When the winner resolves, expires or stops, rebuild the remaining candidate set, "
                        "revalidate every contender, then rank the survivors. Active status alone is insufficient."
                    ),
                    "revalidation": [
                        "current Base Position or source-specific relevance predicate",
                        "resolve and stop facts",
                        "purpose owner and orchestration epoch",
                        "validity and attention windows",
                        "run/enrollment state and prior occurrence consumption",
                    ],
                    "ordering": [
                        "lane rules",
                        "authored attention ordinal",
                        "perishability",
                        "candidate waiting age",
                        "explicit tie policy or bounded learning among equivalent candidates",
                    ],
                    "policy_choices": [
                        "occluded funnel timers use wall-clock or active-attention time",
                        "missed campaign window expires, catches up in a grace period, or waits for next occurrence",
                        "future high-priority opportunity may or may not reserve an upcoming attention slot",
                        "episode re-entry key and duplicate policy",
                    ],
                },
            },
            "modes": {
                "legacy": "The source system decides; purpose-aware Versya automations are blocked.",
                "shadow": "The source remains authoritative; Versya computes for comparison but remains blocked.",
                "versya": "Versya owns final decisions; source emits facts only.",
            },
            "epoch": "Every ownership change increments a monotonic epoch; rollback is another forward change.",
        }

    return _handle_tool("get_orchestration_contract", project_id, {"project_id": project_id}, run)


def get_attention_planning_contract(
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Explain bounded future arbitration before an agent authors priorities."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.channels.selection import future_plan_config, future_plan_mode

        return {
            "project_id": resolved,
            "mode": future_plan_mode(db, resolved),
            "tenant_policy": future_plan_config(db, resolved),
            "tenant_decides": [
                "business ordinal and ordinal_reason for each purpose/episode",
                "whether that episode may reserve attention before it is due",
                "how far ahead known intents are materialized",
                "which temporal distance counts as the same collision window",
                "how often an enforced hold is rechecked",
            ],
            "platform_invariants": [
                "planning never calls a provider",
                "only future_reservation=true can bind an early hold",
                "a hold is bounded by horizon, collision window, expiry and recheck interval",
                "the complete contest is recomputed under the contact/scope lock at dispatch",
                "Guardian then rechecks consent, quiet hours, cooldowns and caps",
                "unknown future facts cannot retroactively reclaim attention already consumed",
            ],
            "consequence_at_gate": {
                "off": "No future intent can hold a due intent.",
                "shadow": "Show who would be held and why without changing dispatch.",
                "enforce": "A higher-ranked opted-in future intent can temporarily hold a due intent in the same window.",
                "required_preview": (
                    "Use preview_contact_attention_plan for representative contacts and require every "
                    "automation activation preview to expose consequence_at_gate."
                ),
            },
            "external_sends": 0,
        }

    return _handle_tool(
        "get_attention_planning_contract",
        project_id,
        {"project_id": project_id},
        run,
    )


def preview_contact_attention_plan(
    contact_id: int,
    project_id: Optional[int] = None,
    attention_scope: Optional[str] = None,
    as_of: Optional[str] = None,
    horizon_minutes: Optional[int] = None,
    collision_window_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """Explain backward/due and known-future arbitration for one contact."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.contacts:read", "versya.automations:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        contact = db.query(MessagingUser.id).filter(
            MessagingUser.id == contact_id,
            MessagingUser.project_id == resolved,
        ).first()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
        evaluated_at = None
        if as_of:
            try:
                evaluated_at = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                if evaluated_at.tzinfo is not None:
                    evaluated_at = evaluated_at.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="as_of must be ISO-8601") from exc
        from app.services.channels.future_attention_planner import FutureAttentionPlanner

        return FutureAttentionPlanner(db).preview_contact(
            resolved,
            user_id=contact_id,
            attention_scope=attention_scope,
            as_of=evaluated_at,
            horizon_minutes=horizon_minutes,
            collision_window_minutes=collision_window_minutes,
        )

    return _handle_tool(
        "preview_contact_attention_plan",
        project_id,
        {
            "project_id": project_id,
            "contact_id": contact_id,
            "attention_scope": attention_scope,
            "as_of": as_of,
            "horizon_minutes": horizon_minutes,
            "collision_window_minutes": collision_window_minutes,
        },
        run,
    )


def configure_attention_planning(
    mode: str,
    config: Dict[str, int],
    expected_version: int,
    reason: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Configure tenant future policy through consequence-bound confirmation."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        if len(str(reason).strip()) < 3:
            raise HTTPException(status_code=422, detail="reason must explain the tenant decision")
        from app.services.channels.future_attention_planner import FutureAttentionPlanner
        from app.services.channels.selection import future_plan_config
        from app.services.engine_rollout_service import EngineRolloutService

        service = EngineRolloutService(db)
        update = {
            "feature_key": "future_plan",
            "mode": mode,
            "config": config,
            "expected_version": expected_version,
        }
        errors = service.validate(resolved, [update])
        effective_config = future_plan_config(
            db,
            resolved,
            override_config=config or {},
        )
        resolved_mode = service.effective_modes(
            resolved, {"future_plan": mode},
        )["future_plan"]
        consequence = FutureAttentionPlanner(db).configuration_consequence(
            resolved,
            mode=mode,
            config=effective_config,
            effective_mode=resolved_mode,
        )
        consequence["dependency_blockers"] = errors
        consequence["valid"] = not errors
        action_input = {
            "project_id": resolved,
            "mode": mode,
            "config": config,
            "expected_version": expected_version,
            "reason": reason,
        }
        if dry_run:
            return {"dry_run": True, "consequence_at_gate": consequence, "external_sends": 0}
        if errors:
            raise HTTPException(
                status_code=409,
                detail={"code": "attention_planning_dependencies", "consequence_at_gate": consequence},
            )
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "configure_attention_planning", action_input,
                project_id=resolved,
            )
            return {
                "status": "pending_confirmation",
                "consequence_at_gate": consequence,
                "external_sends": 0,
                **pending,
            }
        consume_pending_action(
            db,
            principal,
            "configure_attention_planning",
            confirm_token,
            expected_payload=action_input,
        )
        features = service.update(
            resolved,
            [update],
            actor_user_id=principal.user_id,
        )
        return {
            "dry_run": False,
            "feature": next(item for item in features if item["feature_key"] == "future_plan"),
            "consequence_at_gate": consequence,
            "external_sends": 0,
        }

    return _handle_tool(
        "configure_attention_planning",
        project_id,
        {
            "project_id": project_id,
            "mode": mode,
            "config": config,
            "expected_version": expected_version,
            "reason": reason,
            "dry_run": dry_run,
        },
        run,
    )


def _orchestration_inventory(db: Session, project_id: int) -> Dict[str, Any]:
    from app.models.campaigns import Campaign
    from app.models.project_import import ProjectLifecycleCutover

    grouped: Dict[str, Dict[str, Any]] = {}
    unmapped: List[Dict[str, Any]] = []

    def add(kind: str, row: Any, status: str, purpose_key: Optional[str], active: bool):
        item = {"kind": kind, "id": row.id, "name": row.name, "status": status, "active": active}
        if not purpose_key:
            unmapped.append(item)
            return
        purpose = grouped.setdefault(purpose_key, {"purpose_key": purpose_key, "assets": [], "cutover": None})
        purpose["assets"].append(item)

    for row in db.query(models.EventAction).filter(models.EventAction.project_id == project_id).all():
        add("event_action", row, "active" if row.is_active else "disabled", row.purpose_key, bool(row.is_active))
    for row in db.query(models.Funnel).filter(
        models.Funnel.project_id == project_id,
        models.Funnel.source != "event_action",
    ).all():
        add("funnel", row, row.status, row.purpose_key, row.status == "active")
    for row in db.query(Campaign).filter(Campaign.project_id == project_id).all():
        add("campaign", row, row.status, row.purpose_key, row.status == "active")
    for row in db.query(MessagingTemplate).filter(
        MessagingTemplate.project_id == project_id,
        MessagingTemplate.trigger_events.isnot(None),
    ).all():
        add(
            "template", row,
            "active" if row.automation_enabled else "draft",
            row.purpose_key, bool(row.automation_enabled),
        )
    for row in db.query(ProjectLifecycleCutover).filter(ProjectLifecycleCutover.project_id == project_id).all():
        purpose = grouped.setdefault(row.purpose_key, {"purpose_key": row.purpose_key, "assets": [], "cutover": None})
        purpose["cutover"] = _row(row)

    purposes = []
    for key in sorted(grouped):
        item = grouped[key]
        item["asset_count"] = len(item["assets"])
        item["active_asset_count"] = sum(1 for asset in item["assets"] if asset["active"])
        item["mode"] = item["cutover"]["mode"] if item["cutover"] else "legacy"
        item["orchestration_epoch"] = item["cutover"]["orchestration_epoch"] if item["cutover"] else 0
        purposes.append(item)
    return {
        "purposes": purposes,
        "unmapped_assets": unmapped,
        "unmapped_active_count": sum(1 for item in unmapped if item["active"]),
        "system_funnel_mirrors_excluded": True,
    }


def list_orchestration_purposes(project_id: Optional[int] = None) -> Dict[str, Any]:
    """Inventory purpose ownership and automations that still lack a purpose."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        return {"project_id": resolved, **_orchestration_inventory(db, resolved)}

    return _handle_tool("list_orchestration_purposes", project_id, {"project_id": project_id}, run)


def assess_orchestration_readiness(
    purpose_key: str,
    lifecycle_model_id: int,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return machine-readable gates for shadow evaluation and final cutover."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:read", "versya.lifecycle:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,119}", purpose_key or ""):
            raise ValueError("purpose_key is invalid")
        from app.services.lifecycle_model_service import LifecycleModelService
        from app.services.orchestration_cutover_service import OrchestrationCutoverService
        model = LifecycleModelService(db).get(resolved, lifecycle_model_id)
        inventory = _orchestration_inventory(db, resolved)
        purpose = next((item for item in inventory["purposes"] if item["purpose_key"] == purpose_key), None)
        assets = purpose["assets"] if purpose else []
        active_assets = [item for item in assets if item["active"]]
        gate = OrchestrationCutoverService(db).execution_gate(resolved, purpose_key)
        shadow_blockers = []
        cutover_blockers = []
        warnings = []
        if model.status not in {"shadow", "active"}:
            shadow_blockers.append({"code": "shadow_model_required", "message": "Lifecycle model must be shadow or active."})
        if model.status != "active":
            cutover_blockers.append({"code": "active_model_required", "message": "Final Versya ownership requires an active lifecycle model."})
        if not assets:
            warnings.append({"code": "no_purpose_assets", "message": "No Versya automation declares this purpose yet."})
        if not active_assets:
            cutover_blockers.append({"code": "no_active_native_automation", "message": "No active Versya automation is ready to own this purpose."})
        if inventory["unmapped_active_count"]:
            warnings.append({
                "code": "unmapped_active_automations",
                "count": inventory["unmapped_active_count"],
                "message": "Active automations without purpose_key remain unmanaged and must be reviewed independently.",
            })
        return {
            "project_id": resolved,
            "purpose_key": purpose_key,
            "lifecycle_model": _row(model),
            "execution_gate": gate,
            "assets": assets,
            "safe_to_evaluate_in_shadow": not shadow_blockers,
            "shadow_blockers": shadow_blockers,
            "safe_to_cutover_to_versya": not cutover_blockers,
            "cutover_blockers": cutover_blockers,
            "warnings": warnings,
            "safety": {
                "shadow_external_sends_by_purpose_aware_versya_assets": 0,
                "reason": "Both legacy and shadow modes block purpose-aware Versya execution.",
            },
        }

    return _handle_tool(
        "assess_orchestration_readiness", project_id,
        {"project_id": project_id, "purpose_key": purpose_key, "lifecycle_model_id": lifecycle_model_id}, run,
    )


def set_orchestration_cutover(
    purpose_key: str,
    mode: str,
    expected_epoch: int,
    reason: str,
    lifecycle_model_id: Optional[int] = None,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.lifecycle:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.orchestration_cutover_service import OrchestrationCutoverService
        service = OrchestrationCutoverService(db)
        consequence = service.preview(resolved, purpose_key, mode, expected_epoch, lifecycle_model_id)
        action_input = {
            "project_id": resolved, "purpose_key": purpose_key, "mode": mode,
            "expected_epoch": expected_epoch, "reason": reason,
            "lifecycle_model_id": lifecycle_model_id,
        }
        if dry_run:
            return {"dry_run": True, "consequence": consequence}
        if not confirm_token:
            pending = create_pending_action(db, principal, "set_orchestration_cutover", action_input)
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "set_orchestration_cutover", confirm_token,
            expected_payload=action_input,
        )
        row = service.update(
            resolved, purpose_key=purpose_key, mode=mode, expected_epoch=expected_epoch,
            lifecycle_model_id=lifecycle_model_id, reason=reason, actor_user_id=principal.user_id,
        )
        db.commit()
        return {"dry_run": False, "cutover": _row(row)}

    return _handle_tool(
        "set_orchestration_cutover", project_id,
        {"project_id": project_id, "purpose_key": purpose_key, "mode": mode, "expected_epoch": expected_epoch, "dry_run": dry_run}, run,
    )


def _campaign_payload(campaign: Any) -> Dict[str, Any]:
    from app.routers.campaigns import _campaign_dict
    return _campaign_dict(campaign)


def _activation_impact(db: Session, kind: str, row: Any, *, enabled: bool = True) -> Dict[str, Any]:
    """Return the same deterministic preflight used by REST and workers."""
    if not enabled:
        return {
            "project_id": int(row.project_id),
            "candidate": {"kind": kind, "id": getattr(row, "id", None)},
            "evaluated": False,
            "reason": "asset_will_remain_inactive",
            "safe_to_activate": True,
            "requires_confirmation": False,
            "external_sends": 0,
        }
    from app.services.orchestration_impact_service import OrchestrationImpactService

    return OrchestrationImpactService(db).preview(kind, row)


def _reject_attention_impact(report: Dict[str, Any]) -> None:
    if not report.get("safe_to_activate", False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "orchestration_impact_blocked",
                "message": "Resolve the declared attention impacts before activation.",
                "impact": report,
            },
        )


def preview_orchestration_impact(
    asset_kind: str,
    asset_id: int,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Preview cross-funnel/campaign/Event-Action effects without changing state."""
    def run(db: Session, principal: McpPrincipal):
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        kind = str(asset_kind).strip().lower()
        if kind == "campaign":
            require_scopes(["versya.campaigns:read"])
            from app.models.campaigns import Campaign
            row = db.query(Campaign).filter(
                Campaign.id == asset_id,
                Campaign.project_id == resolved,
            ).first()
        elif kind == "funnel":
            require_scopes(["versya.funnels:read"])
            row = db.query(models.Funnel).filter(
                models.Funnel.id == asset_id,
                models.Funnel.project_id == resolved,
                models.Funnel.is_system == False,  # noqa: E712
            ).first()
        elif kind == "event_action":
            require_scopes(["versya.automations:read"])
            row = db.query(models.EventAction).filter(
                models.EventAction.id == asset_id,
                models.EventAction.project_id == resolved,
            ).first()
        elif kind == "template":
            require_scopes(["versya.templates:read"])
            row = db.query(MessagingTemplate).filter(
                MessagingTemplate.id == asset_id,
                MessagingTemplate.project_id == resolved,
            ).first()
        else:
            raise ValueError("asset_kind must be campaign, funnel, event_action or template")
        if not row:
            raise HTTPException(status_code=404, detail="Orchestration asset not found")
        return _activation_impact(db, kind, row)

    return _handle_tool(
        "preview_orchestration_impact",
        project_id,
        {"project_id": project_id, "asset_kind": asset_kind, "asset_id": asset_id},
        run,
    )


def get_commercial_calendar_contract(
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Describe how agents declare and preview market-scoped opportunities."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.commercial_calendar import CommercialCalendarService
        return {
            "project_id": resolved,
            "contract": CommercialCalendarService.contract(),
            "external_sends": 0,
        }

    return _handle_tool(
        "get_commercial_calendar_contract", project_id,
        {"project_id": project_id}, run,
    )


def list_commercial_opportunities(
    project_id: Optional[int] = None,
    horizon_start: Optional[datetime] = None,
    horizon_end: Optional[datetime] = None,
    include_archived: bool = False,
) -> Dict[str, Any]:
    """List explicit holidays, seasons and other tenant calendar occurrences."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.commercial_calendar import CommercialCalendarService
        rows = CommercialCalendarService(db).list(
            resolved,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            include_archived=include_archived,
        )
        return {
            "project_id": resolved,
            "opportunities": [_row(item) for item in rows],
            "external_sends": 0,
        }

    return _handle_tool(
        "list_commercial_opportunities", project_id,
        {
            "project_id": project_id,
            "horizon_start": horizon_start,
            "horizon_end": horizon_end,
            "include_archived": include_archived,
        },
        run,
    )


def upsert_commercial_opportunity(
    data: Dict[str, Any],
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update one calendar occurrence; never creates a campaign run."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.schemas.commercial_calendar import CommercialOpportunityInput
        from app.services.campaigns.commercial_calendar import CommercialCalendarService
        payload = CommercialOpportunityInput(**data)
        binding = payload.model_dump(mode="json")
        service = CommercialCalendarService(db)
        preview = service.upsert(resolved, payload, principal.user_id, commit=False)
        consequence = {
            "opportunity": _row(preview),
            "external_sends": 0,
            "creates_run": False,
            "activates_campaign": False,
        }
        db.rollback()
        if dry_run:
            return {"dry_run": True, "can_upsert": True, "consequence": consequence}
        action_input = {
            "project_id": resolved,
            "opportunity": binding,
            "operation": "upsert_commercial_opportunity",
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "upsert_commercial_opportunity", action_input,
                project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "upsert_commercial_opportunity", confirm_token,
            expected_payload=action_input,
        )
        row = service.upsert(resolved, payload, principal.user_id, commit=True)
        return {"dry_run": False, "status": "upserted", "opportunity": _row(row)}

    return _handle_tool(
        "upsert_commercial_opportunity", project_id,
        {"project_id": project_id, "data": data, "dry_run": dry_run}, run,
    )


def preview_commercial_opportunities(
    data: Dict[str, Any],
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate predictable windows and explain calendar overlaps without sends."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.schemas.commercial_calendar import OpportunityPreviewInput
        from app.services.campaigns.commercial_calendar import CommercialCalendarService
        request = OpportunityPreviewInput(**data)
        return {
            "project_id": resolved,
            **CommercialCalendarService(db).preview(resolved, request),
        }

    return _handle_tool(
        "preview_commercial_opportunities", project_id,
        {"project_id": project_id, "data": data}, run,
    )


def get_campaign_recipe_contract(
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the complete agent workflow for recurring campaign planning."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.recipes import CampaignRecipeService
        return {
            "project_id": resolved,
            "contract": CampaignRecipeService.contract(),
            "next": "Call preview_campaign_recipe with the tenant's declared decisions.",
            "external_sends": 0,
        }

    return _handle_tool(
        "get_campaign_recipe_contract", project_id,
        {"project_id": project_id}, run,
    )


def preview_campaign_recipe(
    data: Dict[str, Any],
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Preview occurrences, copy workload and collisions without persistence."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.schemas.campaign_recipes import CampaignRecipeCreate
        from app.services.campaigns.recipes import CampaignRecipeService
        payload = CampaignRecipeCreate(**data)
        return CampaignRecipeService(db).preview(resolved, payload)

    return _handle_tool(
        "preview_campaign_recipe", project_id,
        {"project_id": project_id, "data": data}, run,
    )


def list_campaign_recipes(
    project_id: Optional[int] = None,
    include_archived: bool = False,
) -> Dict[str, Any]:
    """List persistent recipe policies; no episode or send is created."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.recipes import CampaignRecipeService
        service = CampaignRecipeService(db)
        return {
            "project_id": resolved,
            "recipes": [
                service.recipe_payload(row)
                for row in service.list(resolved, include_archived=include_archived)
            ],
            "external_sends": 0,
        }

    return _handle_tool(
        "list_campaign_recipes", project_id,
        {"project_id": project_id, "include_archived": include_archived}, run,
    )


def get_campaign_recipe(
    recipe_id: int,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Read one recipe and its current deterministic preview."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.recipes import CampaignRecipeService
        service = CampaignRecipeService(db)
        row = service.get(resolved, recipe_id)
        if not row:
            raise HTTPException(status_code=404, detail="Campaign recipe not found")
        return {
            "recipe": service.recipe_payload(row),
            "preview": service.preview(
                resolved, service._definition_from_row(row), exclude_recipe_id=row.id,
            ),
            "external_sends": 0,
        }

    return _handle_tool(
        "get_campaign_recipe", project_id,
        {"project_id": project_id, "recipe_id": recipe_id}, run,
    )


def create_draft_campaign_recipe(
    data: Dict[str, Any],
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a recipe as draft; never activates a campaign or creates a run."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.models.campaigns import CampaignRecipe
        from app.schemas.campaign_recipes import CampaignRecipeCreate
        from app.services.campaigns.recipes import CampaignRecipeService
        payload = CampaignRecipeCreate(**data)
        binding = payload.model_dump(mode="json")
        service = CampaignRecipeService(db)
        existing = db.query(CampaignRecipe).filter(
            CampaignRecipe.project_id == resolved,
            CampaignRecipe.external_key == payload.external_key,
        ).first()
        if existing:
            return {
                "dry_run": dry_run,
                "status": "existing_external_key",
                "recipe": service.recipe_payload(existing),
            }
        preview = service.preview(resolved, payload)
        consequence = {
            "recipe": binding,
            "preview": preview,
            "creates_recipe": True,
            "creates_episode_records": False,
            "creates_campaign_drafts": False,
            "activates_campaigns": False,
            "creates_runs": False,
            "external_sends": 0,
        }
        if dry_run:
            return {"dry_run": True, "can_create": True, "consequence": consequence}
        action_input = {"project_id": resolved, "recipe": binding, "operation": "create_draft"}
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "create_draft_campaign_recipe", action_input,
                project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "create_draft_campaign_recipe", confirm_token,
            expected_payload=action_input,
        )
        row = service.create(resolved, payload, principal.user_id, commit=True)
        return {"dry_run": False, "status": "created", "recipe": service.recipe_payload(row), "external_sends": 0}

    return _handle_tool(
        "create_draft_campaign_recipe", project_id,
        {"project_id": project_id, "data": data, "dry_run": dry_run}, run,
    )


def update_campaign_recipe(
    recipe_id: int,
    data: Dict[str, Any],
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Edit a draft/paused recipe with optimistic confirmation binding."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.schemas.campaign_recipes import CampaignRecipeUpdate
        from app.services.campaigns.recipes import CampaignRecipeService
        service = CampaignRecipeService(db)
        row = service.get(resolved, recipe_id)
        if not row:
            raise HTTPException(status_code=404, detail="Campaign recipe not found")
        expected_version = int(row.version or 1)
        changes = CampaignRecipeUpdate(**data)
        binding = changes.model_dump(mode="json", exclude_unset=True)
        preview_row = service.update(row, changes, commit=False)
        consequence = {
            "recipe": service.recipe_payload(preview_row),
            "from_version": expected_version,
            "to_version": expected_version + 1,
            "supersedes_unmaterialized_episodes": True,
            "external_sends": 0,
        }
        db.rollback()
        if dry_run:
            return {"dry_run": True, "can_update": True, "consequence": consequence}
        action_input = {
            "project_id": resolved,
            "recipe_id": recipe_id,
            "expected_version": expected_version,
            "changes": binding,
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "update_campaign_recipe", action_input,
                project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "update_campaign_recipe", confirm_token,
            expected_payload=action_input,
        )
        row = service.get(resolved, recipe_id)
        if not row or int(row.version or 1) != expected_version:
            raise ValueError("Campaign recipe changed after approval; preview again")
        row = service.update(row, changes, commit=True)
        return {"dry_run": False, "status": "updated", "recipe": service.recipe_payload(row), "external_sends": 0}

    return _handle_tool(
        "update_campaign_recipe", project_id,
        {"project_id": project_id, "recipe_id": recipe_id, "data": data, "dry_run": dry_run}, run,
    )


def set_campaign_recipe_state(
    recipe_id: int,
    target_status: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Activate/pause/archive recipe planning; activation still cannot send."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.campaigns.recipes import CampaignRecipeService
        service = CampaignRecipeService(db)
        row = service.get(resolved, recipe_id)
        if not row:
            raise HTTPException(status_code=404, detail="Campaign recipe not found")
        if target_status not in {"active", "paused", "archived"}:
            raise ValueError("target_status must be active, paused or archived")
        expected_version = int(row.version or 1)
        if target_status == "active":
            preview = service.preview(
                resolved, service._definition_from_row(row), exclude_recipe_id=row.id,
            )
            can_apply = preview["can_activate"]
        else:
            preview = {
                "can_activate": None,
                "state_change": f"{row.status}->{target_status}",
                "note": "Stopping recipe planning is always available.",
                "external_sends": 0,
            }
            can_apply = True
        consequence = {
            "from_status": row.status,
            "to_status": target_status,
            "preview": preview,
            "creates_episode_records": target_status == "active",
            "may_create_fixed_campaign_drafts": (
                target_status == "active"
                and row.content_mode == "fixed"
                and (row.autonomy_policy or {}).get("episode_materialization") == "automatic"
            ),
            "activates_campaigns": False,
            "creates_runs": False,
            "external_sends": 0,
        }
        if dry_run:
            return {"dry_run": True, "can_apply": can_apply, "consequence_at_gate": consequence}
        if not can_apply:
            raise HTTPException(status_code=409, detail={"code": "recipe_activation_blocked", "consequence": consequence})
        action_input = {
            "project_id": resolved,
            "recipe_id": recipe_id,
            "expected_version": expected_version,
            "target_status": target_status,
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "set_campaign_recipe_state", action_input,
                project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "set_campaign_recipe_state", confirm_token,
            expected_payload=action_input,
        )
        row = service.get(resolved, recipe_id)
        if not row or int(row.version or 1) != expected_version:
            raise ValueError("Campaign recipe changed after approval; preview again")
        row, applied_preview = service.set_state(row, target_status, commit=True)
        return {
            "dry_run": False,
            "status": target_status,
            "recipe": service.recipe_payload(row),
            "preview": applied_preview,
            "external_sends": 0,
        }

    return _handle_tool(
        "set_campaign_recipe_state", project_id,
        {"project_id": project_id, "recipe_id": recipe_id, "target_status": target_status, "dry_run": dry_run}, run,
    )


def get_campaign_planning_inbox(
    project_id: Optional[int] = None,
    include_terminal: bool = False,
    limit: int = 100,
) -> Dict[str, Any]:
    """Return due episode work and explicit next actions for a code agent."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.recipes import CampaignRecipeService
        return CampaignRecipeService(db).planning_inbox(
            resolved, include_terminal=include_terminal, limit=limit,
        )

    return _handle_tool(
        "get_campaign_planning_inbox", project_id,
        {"project_id": project_id, "include_terminal": include_terminal, "limit": limit}, run,
    )


def materialize_campaign_episode(
    episode_id: int,
    data: Optional[Dict[str, Any]] = None,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Turn one ready episode into an inactive draft campaign."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.schemas.campaign_recipes import CampaignEpisodeMaterialize
        from app.services.campaigns.recipes import CampaignRecipeService
        service = CampaignRecipeService(db)
        episode = service.get_episode(resolved, episode_id)
        if not episode:
            raise HTTPException(status_code=404, detail="Campaign recipe episode not found")
        if episode.campaign_id:
            return {
                "dry_run": dry_run,
                "status": "already_materialized",
                "episode": service.episode_payload(episode),
                "campaign": _campaign_payload(episode.campaign),
            }
        request = CampaignEpisodeMaterialize(**(data or {}))
        binding = request.model_dump(mode="json", exclude_unset=True)
        expected_status = episode.status
        expected_recipe_version = episode.recipe_version
        preview_episode = service.materialize_episode(
            episode, request, principal.user_id, commit=False,
        )
        campaign = preview_episode.campaign
        consequence = {
            "episode": service.episode_payload(preview_episode),
            "campaign": _campaign_payload(campaign),
            "creates_campaign_draft": True,
            "activates_campaign": False,
            "creates_run": False,
            "external_sends": 0,
        }
        db.rollback()
        if dry_run:
            return {"dry_run": True, "can_materialize": True, "consequence": consequence}
        action_input = {
            "project_id": resolved,
            "episode_id": episode_id,
            "expected_status": expected_status,
            "expected_recipe_version": expected_recipe_version,
            "content": binding,
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "materialize_campaign_episode", action_input,
                project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "materialize_campaign_episode", confirm_token,
            expected_payload=action_input,
        )
        episode = service.get_episode(resolved, episode_id)
        if (
            not episode
            or episode.status != expected_status
            or episode.recipe_version != expected_recipe_version
        ):
            raise ValueError("Campaign episode changed after approval; preview again")
        episode = service.materialize_episode(episode, request, principal.user_id, commit=True)
        return {
            "dry_run": False,
            "status": "materialized",
            "episode": service.episode_payload(episode),
            "campaign": _campaign_payload(episode.campaign),
            "external_sends": 0,
        }

    return _handle_tool(
        "materialize_campaign_episode", project_id,
        {"project_id": project_id, "episode_id": episode_id, "data": data or {}, "dry_run": dry_run}, run,
    )


def list_campaigns(
    project_id: Optional[int] = None,
    include_archived: bool = False,
) -> Dict[str, Any]:
    """List project campaigns, including their purpose and draft/active state."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.service import CampaignService
        return {
            "campaigns": [
                _campaign_payload(item)
                for item in CampaignService(db).list(resolved, include_archived=include_archived)
            ]
        }

    return _handle_tool(
        "list_campaigns", project_id,
        {"project_id": project_id, "include_archived": include_archived}, run,
    )


def get_campaign(campaign_id: int, project_id: Optional[int] = None) -> Dict[str, Any]:
    """Get a campaign definition without creating a run or external send."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.service import CampaignService
        campaign = CampaignService(db).get(resolved, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        return {"campaign": _campaign_payload(campaign)}

    return _handle_tool(
        "get_campaign", project_id,
        {"project_id": project_id, "campaign_id": campaign_id}, run,
    )


def create_draft_campaign(
    data: Dict[str, Any],
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and persist a draft campaign; this tool never activates or runs it."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.models.campaigns import Campaign
        from app.schemas.campaigns import CampaignCreate
        from app.services.campaigns.service import CampaignService
        payload = CampaignCreate(**data)
        if payload.status != "draft":
            raise ValueError("create_draft_campaign only accepts status=draft")
        values = payload.model_dump(mode="python", exclude_unset=True)
        binding_values = payload.model_dump(mode="json", exclude_unset=True)
        if payload.external_key:
            existing = db.query(Campaign).filter(
                Campaign.project_id == resolved,
                Campaign.external_key == payload.external_key,
            ).first()
            if existing:
                return {
                    "dry_run": dry_run,
                    "status": "existing_external_key",
                    "campaign": _campaign_payload(existing),
                }
        service = CampaignService(db)
        preview_campaign = service.create(
            resolved, values, principal.user_id, commit=False,
        )
        consequence = {
            "campaign": _campaign_payload(preview_campaign),
            "external_sends": 0,
            "creates_run": False,
            "activates_campaign": False,
        }
        db.rollback()
        if dry_run:
            return {"dry_run": True, "can_create": True, "consequence": consequence}
        action_input = {
            "project_id": resolved,
            "campaign": binding_values,
            "operation": "create_draft",
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "create_draft_campaign", action_input, project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "create_draft_campaign", confirm_token,
            expected_payload=action_input,
        )
        campaign = service.create(resolved, values, principal.user_id, commit=False)
        db.commit()
        db.refresh(campaign)
        return {"dry_run": False, "status": "created", "campaign": _campaign_payload(campaign)}

    return _handle_tool(
        "create_draft_campaign", project_id,
        {"project_id": project_id, "data": data, "dry_run": dry_run}, run,
    )


def update_draft_campaign(
    campaign_id: int,
    data: Dict[str, Any],
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Update a draft campaign with optimistic, confirmation-bound versioning."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.schemas.campaigns import CampaignUpdate
        from app.services.campaigns.service import CampaignService
        service = CampaignService(db)
        campaign = service.get(resolved, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if campaign.status != "draft":
            raise ValueError("Only draft campaigns can be updated through this MCP tool")
        expected_version = int(campaign.version or 1)
        payload = CampaignUpdate(**data)
        if payload.status not in {None, "draft"}:
            raise ValueError("update_draft_campaign cannot activate or pause a campaign")
        values = payload.model_dump(mode="python", exclude_unset=True)
        binding_values = payload.model_dump(mode="json", exclude_unset=True)
        preview_campaign = service.update(
            campaign, values, principal.user_id, commit=False,
        )
        consequence = {
            "campaign": _campaign_payload(preview_campaign),
            "from_version": expected_version,
            "to_version": expected_version + 1,
            "external_sends": 0,
            "creates_run": False,
            "activates_campaign": False,
        }
        db.rollback()
        if dry_run:
            return {"dry_run": True, "can_update": True, "consequence": consequence}
        action_input = {
            "project_id": resolved,
            "campaign_id": campaign_id,
            "expected_version": expected_version,
            "changes": binding_values,
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "update_draft_campaign", action_input, project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "update_draft_campaign", confirm_token,
            expected_payload=action_input,
        )
        campaign = service.get(resolved, campaign_id)
        if not campaign or int(campaign.version or 1) != expected_version:
            raise ValueError("Campaign changed after approval; run dry-run again")
        campaign = service.update(campaign, values, principal.user_id, commit=False)
        db.commit()
        db.refresh(campaign)
        return {"dry_run": False, "status": "updated", "campaign": _campaign_payload(campaign)}

    return _handle_tool(
        "update_draft_campaign", project_id,
        {"project_id": project_id, "campaign_id": campaign_id, "data": data, "dry_run": dry_run}, run,
    )


def preview_campaign_plan(
    campaign_id: int,
    schedule: Optional[Dict[str, Any]] = None,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Calculate audience, suppression and capacity without creating a run."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.services.campaigns.service import CampaignService
        service = CampaignService(db)
        campaign = service.get(resolved, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        return {
            "campaign": _campaign_payload(campaign),
            "plan": service.preview_plan(campaign, schedule or {}),
            "external_sends": 0,
            "creates_run": False,
        }

    return _handle_tool(
        "preview_campaign_plan", project_id,
        {"project_id": project_id, "campaign_id": campaign_id, "schedule": schedule or {}}, run,
    )


def activate_campaign(
    campaign_id: int,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Preflight and activate a campaign; activation never creates a run or sends."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.services.campaigns.service import CampaignService

        service = CampaignService(db)
        campaign = service.get(resolved, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        impact = _activation_impact(db, "campaign", campaign)
        consequence = {
            "campaign": _campaign_payload(campaign),
            "impact": impact,
            "creates_run": False,
            "external_sends": 0,
        }
        if dry_run:
            return {
                "dry_run": True,
                "can_activate": bool(impact["safe_to_activate"]),
                "consequence": consequence,
            }
        _reject_attention_impact(impact)
        action_input = {
            "project_id": resolved,
            "campaign_id": campaign_id,
            "expected_version": int(campaign.version or 1),
            "impact_fingerprint": impact["impact_fingerprint"],
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "activate_campaign", action_input, project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence": consequence, **pending}
        consume_pending_action(
            db, principal, "activate_campaign", confirm_token,
            expected_payload=action_input,
        )
        campaign, applied_impact = service.activate(
            campaign,
            impact_fingerprint=impact["impact_fingerprint"],
            commit=True,
        )
        return {
            "dry_run": False,
            "status": "activated",
            "campaign": _campaign_payload(campaign),
            "impact": applied_impact,
            "creates_run": False,
            "external_sends": 0,
        }

    return _handle_tool(
        "activate_campaign", project_id,
        {"project_id": project_id, "campaign_id": campaign_id, "dry_run": dry_run}, run,
    )


def evaluate_campaign_run_consequences(
    campaign_id: int,
    schedule: Optional[Dict[str, Any]] = None,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Persist a short-lived exact consequence gate; this never schedules a run."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        from app.services.campaigns.service import CampaignService
        from app.services.decision_gate_service import DecisionGateService
        campaign = CampaignService(db).get(resolved, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if campaign.status != "active":
            raise ValueError("Activate the campaign before evaluating a run")
        evaluation = DecisionGateService(db).evaluate_campaign(
            campaign, schedule or {}, principal.user_id,
        )
        return {
            "project_id": resolved,
            "campaign_id": campaign_id,
            "evaluation": _row(evaluation),
            "instructions": [
                "Present the exact options and consequences to the tenant or apply its predeclared policy.",
                "Never choose meet_deadline unless the tenant explicitly accepted the listed override risk.",
                "Call choose_campaign_run_consequence before schedule_campaign_run.",
            ],
            "creates_run": False,
            "external_sends": 0,
        }

    return _handle_tool(
        "evaluate_campaign_run_consequences", project_id,
        {"project_id": project_id, "campaign_id": campaign_id, "schedule": schedule or {}}, run,
    )


def choose_campaign_run_consequence(
    evaluation_id: int,
    option_key: str,
    reason: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Choose one short-lived consequence option with explicit confirmation."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.models.engine_control import DecisionGateEvaluation
        from app.services.decision_gate_service import DecisionGateService
        evaluation = db.query(DecisionGateEvaluation).filter(
            DecisionGateEvaluation.id == evaluation_id,
            DecisionGateEvaluation.project_id == resolved,
            DecisionGateEvaluation.gate_type == "campaign_run",
        ).first()
        if not evaluation:
            raise HTTPException(status_code=404, detail="Campaign run consequence evaluation not found")
        if evaluation.status != "valid" or evaluation.expires_at <= datetime.utcnow():
            raise ValueError("Campaign run consequence evaluation expired; evaluate again")
        option = next(
            (item for item in (evaluation.options or []) if item.get("key") == option_key),
            None,
        )
        if not option:
            raise ValueError("The requested consequence option is not available")
        if len(str(reason).strip()) < 3:
            raise ValueError("reason must explain why this consequence was chosen")
        consequence = {
            "evaluation_id": evaluation.id,
            "option": option,
            "choice_authorizes_run": option_key != "do_nothing",
            "creates_run": False,
            "external_sends": 0,
        }
        if dry_run:
            return {"dry_run": True, "can_choose": True, "consequence_at_gate": consequence}
        action_input = {
            "project_id": resolved,
            "evaluation_id": evaluation_id,
            "option_key": option_key,
            "reason": str(reason).strip(),
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "choose_campaign_run_consequence", action_input,
                project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "choose_campaign_run_consequence", confirm_token,
            expected_payload=action_input,
        )
        choice = DecisionGateService(db).choose(
            resolved, evaluation_id, option_key, principal.user_id, str(reason).strip(),
        )
        return {
            "dry_run": False,
            "status": "chosen",
            "choice": _row(choice),
            "next": (
                "No run will be created for do_nothing."
                if option_key == "do_nothing"
                else "Call schedule_campaign_run before this choice expires."
            ),
            "external_sends": 0,
        }

    return _handle_tool(
        "choose_campaign_run_consequence", project_id,
        {
            "project_id": project_id,
            "evaluation_id": evaluation_id,
            "option_key": option_key,
            "reason": reason,
            "dry_run": dry_run,
        },
        run,
    )


def schedule_campaign_run(
    campaign_id: int,
    decision_choice_id: int,
    run_key: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a run from an approved consequence choice; this authorizes future sends."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.models.campaigns import CampaignRecipeEpisode, CampaignRun
        from app.models.engine_control import DecisionGateChoice, DecisionGateEvaluation
        from app.services.campaigns.service import CampaignService
        from app.services.decision_gate_service import DecisionGateService
        if len(str(run_key).strip()) < 8:
            raise ValueError("run_key must be stable and at least 8 characters")
        service = CampaignService(db)
        campaign = service.get(resolved, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if campaign.status != "active":
            raise ValueError("Only an active campaign can schedule a run")
        existing = db.query(CampaignRun).filter(
            CampaignRun.project_id == resolved,
            CampaignRun.campaign_id == campaign_id,
            CampaignRun.run_key == str(run_key).strip(),
        ).first()
        if existing:
            return {"dry_run": dry_run, "status": "existing_run_key", "run": _row(existing)}
        choice = db.query(DecisionGateChoice).filter(
            DecisionGateChoice.id == decision_choice_id,
            DecisionGateChoice.project_id == resolved,
        ).first()
        if not choice:
            raise HTTPException(status_code=404, detail="Campaign run consequence choice not found")
        evaluation = db.query(DecisionGateEvaluation).filter(
            DecisionGateEvaluation.id == choice.evaluation_id,
            DecisionGateEvaluation.project_id == resolved,
        ).first()
        DecisionGateService(db).choice_for_run(resolved, campaign, decision_choice_id)
        snapshot = evaluation.input_snapshot or {}
        schedule = snapshot.get("schedule") or {}
        expected = {
            "campaign_version": int(snapshot.get("campaign_version") or campaign.version or 1),
            "candidate_count": int(snapshot.get("candidate_count") or 0),
            "eligible_count": int(snapshot.get("eligible_count") or 0),
            "planned_count": int(snapshot.get("planned_count") or 0),
        }
        consequence = {
            "campaign_id": campaign_id,
            "decision_choice": _row(choice),
            "schedule": schedule,
            "expected": expected,
            "creates_run": True,
            "creates_durable_recipients": expected["planned_count"],
            "authorizes_future_external_sends": expected["planned_count"],
            "provider_calls_during_dry_run": 0,
            "dispatch_note": (
                "After confirmation the worker may dispatch at the approved schedule; "
                "Selection and Guardian still re-evaluate every recipient."
            ),
        }
        if dry_run:
            return {"dry_run": True, "can_schedule": True, "consequence_at_gate": consequence}
        action_input = {
            "project_id": resolved,
            "campaign_id": campaign_id,
            "decision_choice_id": decision_choice_id,
            "run_key": str(run_key).strip(),
            "evaluation_inputs_hash": evaluation.inputs_hash,
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "schedule_campaign_run", action_input,
                project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "schedule_campaign_run", confirm_token,
            expected_payload=action_input,
        )
        run_row = service.create_run(
            campaign,
            schedule=schedule,
            requested_by_user_id=principal.user_id,
            expected=expected,
            run_key=str(run_key).strip(),
            decision_choice_id=decision_choice_id,
        )
        episode = db.query(CampaignRecipeEpisode).filter(
            CampaignRecipeEpisode.project_id == resolved,
            CampaignRecipeEpisode.campaign_id == campaign_id,
        ).first()
        if episode:
            episode.run_id = run_row.id
            episode.status = "scheduled"
            db.commit()
            db.refresh(run_row)
        return {
            "dry_run": False,
            "status": "scheduled",
            "run": _row(run_row),
            "future_external_sends_authorized": run_row.planned_count,
            "next": "Monitor with get_campaign_run_status; call cancel_campaign_run if the business decision changes.",
        }

    return _handle_tool(
        "schedule_campaign_run", project_id,
        {
            "project_id": project_id,
            "campaign_id": campaign_id,
            "decision_choice_id": decision_choice_id,
            "run_key": run_key,
            "dry_run": dry_run,
        },
        run,
    )


def get_campaign_run_status(
    campaign_id: int,
    run_id: int,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Read aggregate run progress and consequence provenance."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:read"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "viewer")
        from app.models.campaigns import CampaignRun, CampaignWave
        row = db.query(CampaignRun).filter(
            CampaignRun.id == run_id,
            CampaignRun.project_id == resolved,
            CampaignRun.campaign_id == campaign_id,
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="Campaign run not found")
        waves = db.query(CampaignWave).filter(
            CampaignWave.project_id == resolved,
            CampaignWave.run_id == run_id,
        ).order_by(CampaignWave.position).all()
        return {
            "run": _row(row),
            "waves": [_row(wave) for wave in waves],
            "next": (
                "Run is terminal; review outcomes."
                if row.status in {"completed", "canceled", "failed"}
                else "Continue monitoring; dispatch remains subject to Selection and Guardian."
            ),
        }

    return _handle_tool(
        "get_campaign_run_status", project_id,
        {"project_id": project_id, "campaign_id": campaign_id, "run_id": run_id}, run,
    )


def cancel_campaign_run(
    campaign_id: int,
    run_id: int,
    reason: str,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel preventable campaign work; provider calls already submitted cannot be recalled."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.campaigns:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "admin")
        from app.models.campaigns import CampaignRecipeEpisode, CampaignRecipient, CampaignRun
        from app.services.campaigns.service import CampaignService
        row = db.query(CampaignRun).filter(
            CampaignRun.id == run_id,
            CampaignRun.project_id == resolved,
            CampaignRun.campaign_id == campaign_id,
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="Campaign run not found")
        normalized_reason = str(reason).strip()
        if len(normalized_reason) < 3:
            raise ValueError("reason must explain why this run should be canceled")
        counts = dict(
            db.query(CampaignRecipient.status, func.count(CampaignRecipient.id)).filter(
                CampaignRecipient.project_id == resolved,
                CampaignRecipient.run_id == run_id,
            ).group_by(CampaignRecipient.status).all()
        )
        terminal = row.status in {"completed", "canceled", "failed"}
        preventable = sum(int(counts.get(state, 0)) for state in ("pending", "held", "deferred"))
        consequence = {
            "campaign_id": campaign_id,
            "run_id": run_id,
            "current_status": row.status,
            "recipient_status_counts": counts,
            "preventable_recipient_attempts": preventable,
            "in_flight_attempts": int(counts.get("processing", 0)),
            "already_sent": int(row.sent_count or 0),
            "already_submitted_calls_cannot_be_recalled": True,
            "provider_calls_by_this_tool": 0,
            "external_sends_authorized_by_this_tool": 0,
        }
        if terminal:
            return {
                "dry_run": dry_run,
                "status": "already_terminal",
                "can_cancel": False,
                "run": _row(row),
                "consequence_at_gate": consequence,
            }
        if dry_run:
            return {"dry_run": True, "can_cancel": True, "consequence_at_gate": consequence}
        action_input = {
            "project_id": resolved,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "expected_status": row.status,
            "reason": normalized_reason,
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "cancel_campaign_run", action_input, project_id=resolved,
            )
            return {"status": "pending_confirmation", "consequence_at_gate": consequence, **pending}
        consume_pending_action(
            db, principal, "cancel_campaign_run", confirm_token,
            expected_payload=action_input,
        )
        row = db.query(CampaignRun).filter(
            CampaignRun.id == run_id,
            CampaignRun.project_id == resolved,
            CampaignRun.campaign_id == campaign_id,
        ).first()
        if not row or row.status != action_input["expected_status"]:
            raise ValueError("Campaign run changed after approval; preview cancellation again")
        row = CampaignService(db).cancel_run(row)
        episode = db.query(CampaignRecipeEpisode).filter(
            CampaignRecipeEpisode.project_id == resolved,
            CampaignRecipeEpisode.run_id == run_id,
        ).first()
        if episode and row.status == "canceled":
            episode.status = "canceled"
            db.commit()
        return {
            "dry_run": False,
            "status": row.status,
            "run": _row(row),
            "reason": normalized_reason,
            "in_flight_warning": (
                "Some provider-bound work may still complete before cancellation converges."
                if row.status == "cancel_requested" else None
            ),
            "external_sends_authorized_by_this_tool": 0,
        }

    return _handle_tool(
        "cancel_campaign_run", project_id,
        {
            "project_id": project_id,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "reason": reason,
            "dry_run": dry_run,
        },
        run,
    )


def list_templates(project_id: Optional[int] = None, folder: Optional[str] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.templates:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        query = db.query(MessagingTemplate).filter(MessagingTemplate.project_id == resolved_project_id)
        if folder is not None:
            query = query.filter(MessagingTemplate.folder == (None if folder == "__uncategorized__" else folder))
        templates = query.order_by(MessagingTemplate.created_at.desc()).all()
        return {"templates": [_row(t) for t in templates]}

    return _handle_tool("list_templates", project_id, {"project_id": project_id, "folder": folder}, run)


def create_template(data: Dict[str, Any], project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.templates:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        payload = MessagingTemplateCreate(**data)
        existing = (
            db.query(MessagingTemplate)
            .filter(MessagingTemplate.project_id == resolved_project_id, MessagingTemplate.slug == payload.slug)
            .first()
        )
        if existing:
            raise HTTPException(status_code=409, detail="Template slug already exists")
        template = MessagingTemplate(
            project_id=resolved_project_id,
            channel_id=payload.channel_id,
            channel_type=payload.channel_type,
            from_email=payload.from_email,
            from_name=payload.from_name,
            reply_to=payload.reply_to,
            folder=payload.folder,
            body_format=payload.body_format or "html",
            media_url=payload.media_url,
            slug=payload.slug,
            name=payload.name,
            subject=payload.subject,
            body=payload.body,
            template_metadata=payload.metadata,
            trigger_events=payload.trigger_events,
            automation_enabled=False,
            purpose_key=payload.purpose_key,
            attention_policy=(
                _model_dump_json(payload.attention_policy)
                if payload.attention_policy else None
            ),
            meta_template_name=payload.meta_template_name,
            meta_language=payload.meta_language,
            meta_components=payload.meta_components,
            whatsapp_instance_id=payload.whatsapp_instance_id,
        )
        db.add(template)
        db.commit()
        db.refresh(template)
        return {"template": _row(template)}

    return _handle_tool("create_template", project_id, {"project_id": project_id, "data": data}, run)


def update_template(template_id: int, data: Dict[str, Any], project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.templates:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        payload = MessagingTemplateUpdate(**data)
        template = (
            db.query(MessagingTemplate)
            .filter(MessagingTemplate.id == template_id, MessagingTemplate.project_id == resolved_project_id)
            .first()
        )
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        changes = _model_dump_json(payload, exclude_unset=True)
        if changes.get("automation_enabled") is True:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "dedicated_activation_required",
                    "message": "Use preview_orchestration_impact and activate_template_automation.",
                },
            )
        if template.automation_enabled and changes != {"automation_enabled": False}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "active_automation_immutable",
                    "message": "Disable template automation before editing it.",
                },
            )
        for field, value in changes.items():
            if field == "metadata":
                template.template_metadata = value
            elif hasattr(template, field):
                setattr(template, field, value)
        db.commit()
        db.refresh(template)
        return {"template": _row(template)}

    return _handle_tool("update_template", project_id, {"project_id": project_id, "template_id": template_id, "data": data}, run)


def preview_template(template_id: int, project_id: Optional[int] = None, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.templates:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        request = MessagingTemplatePreviewRequest(variables=variables or {})
        template = (
            db.query(MessagingTemplate)
            .filter(MessagingTemplate.id == template_id, MessagingTemplate.project_id == resolved_project_id)
            .first()
        )
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        rendered_body, rendered_subject, used, missing = template_renderer.render_template(
            template.body, dict(request.variables), template.subject
        )
        return {
            "rendered_subject": rendered_subject,
            "rendered_body": rendered_body,
            "variables_used": used,
            "missing_variables": missing,
        }

    return _handle_tool("preview_template", project_id, {"project_id": project_id, "template_id": template_id}, run)


def activate_template_automation(
    template_id: int,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Enable a triggered template through the common exact-impact gate."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.templates:write"])
        resolved = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved, "editor")
        template = db.query(MessagingTemplate).filter(
            MessagingTemplate.id == template_id,
            MessagingTemplate.project_id == resolved,
        ).first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        impact = _activation_impact(db, "template", template)
        consequence = {
            "template": _row(template),
            "impact": impact,
            "external_sends": 0,
        }
        if dry_run:
            return {
                "dry_run": True,
                "can_activate": bool(impact["safe_to_activate"]),
                "consequence": consequence,
            }
        _reject_attention_impact(impact)
        action_input = {
            "project_id": resolved,
            "template_id": template_id,
            "expected_updated_at": (
                template.updated_at.isoformat() if template.updated_at else None
            ),
            "impact_fingerprint": impact["impact_fingerprint"],
        }
        if not confirm_token:
            pending = create_pending_action(
                db, principal, "activate_template_automation", action_input,
                project_id=resolved,
            )
            return {
                "status": "pending_confirmation",
                "consequence": consequence,
                **pending,
            }
        consume_pending_action(
            db, principal, "activate_template_automation", confirm_token,
            expected_payload=action_input,
        )
        # Recompute under the same current row. The confirmation payload binds
        # both updated_at and the complete impact graph.
        current = _activation_impact(db, "template", template)
        _reject_attention_impact(current)
        if current["impact_fingerprint"] != impact["impact_fingerprint"]:
            raise HTTPException(status_code=409, detail="Orchestration impact changed; preview again")
        template.automation_enabled = True
        db.commit()
        db.refresh(template)
        return {
            "dry_run": False,
            "status": "activated",
            "template": _row(template),
            "impact": current,
            "external_sends": 0,
        }

    return _handle_tool(
        "activate_template_automation", project_id,
        {"project_id": project_id, "template_id": template_id, "dry_run": dry_run},
        run,
    )


def list_project_variables(project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.variables:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        variables = ProjectVariableService(db).list(resolved_project_id)
        return {"variables": [_row(v) for v in variables]}

    return _handle_tool("list_project_variables", project_id, {"project_id": project_id}, run)


def create_project_variable(data: Dict[str, Any], project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.variables:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        payload = ProjectVariableCreate(**data)
        variable = ProjectVariableService(db).create(resolved_project_id, payload.model_dump())
        return {"variable": _row(variable)}

    return _handle_tool("create_project_variable", project_id, {"project_id": project_id, "data": data}, run)


def update_project_variable(variable_id: int, data: Dict[str, Any], project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.variables:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        payload = ProjectVariableUpdate(**data)
        variable = ProjectVariableService(db).update(resolved_project_id, variable_id, payload.model_dump(exclude_unset=True))
        if not variable:
            raise HTTPException(status_code=404, detail="Variable not found")
        return {"variable": _row(variable)}

    return _handle_tool("update_project_variable", project_id, {"project_id": project_id, "variable_id": variable_id, "data": data}, run)


# ---------------------------------------------------------------------------
# Product MCP automation tools
# ---------------------------------------------------------------------------


def _model_dump_json(payload: Any, *, exclude_unset: bool = False) -> Dict[str, Any]:
    """Dump Pydantic v1/v2 models into JSON-safe values for JSON columns."""
    if hasattr(payload, "model_dump"):
        try:
            return payload.model_dump(mode="json", exclude_unset=exclude_unset)
        except TypeError:
            return payload.model_dump(exclude_unset=exclude_unset)
    return payload.dict(exclude_unset=exclude_unset)


def _project_automation_conflicts(
    db: Session,
    project_id: int,
    *,
    include_inactive: bool = False,
    candidate: Any = None,
    exclude_event_action_id: Optional[int] = None,
) -> Dict[str, Any]:
    query = db.query(models.EventAction).filter(
        models.EventAction.project_id == project_id,
    )
    if exclude_event_action_id is not None:
        query = query.filter(models.EventAction.id != exclude_event_action_id)
    event_actions = query.all()
    if candidate is not None:
        event_actions = [*event_actions, candidate]
    funnels = db.query(models.Funnel).filter(
        models.Funnel.project_id == project_id,
    ).all()
    return analyze_automation_conflicts(
        event_actions,
        funnels,
        include_inactive=include_inactive,
    )


def _reject_blocking_activation(conflicts: Dict[str, Any], *, is_active: bool) -> None:
    """Do not turn a known duplicate automation into a live rule."""
    if is_active and conflicts.get("error_count", 0):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "automation_conflict",
                "message": "Resolve blocking automation conflicts before activation",
                "conflicts": conflicts,
            },
        )


def list_event_actions(
    project_id: Optional[int] = None,
    trigger_event: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Dict[str, Any]:
    """List project Event Actions, including lane and priority metadata."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        query = db.query(models.EventAction).filter(
            models.EventAction.project_id == resolved_project_id,
        )
        if trigger_event:
            query = query.filter(models.EventAction.trigger_event == trigger_event)
        if is_active is not None:
            query = query.filter(models.EventAction.is_active == is_active)
        actions = query.order_by(
            models.EventAction.trigger_event,
            models.EventAction.priority.desc(),
            models.EventAction.id,
        ).all()
        return {
            "event_actions": [_row(item) for item in actions],
            "count": len(actions),
        }

    return _handle_tool(
        "list_event_actions",
        project_id,
        {"project_id": project_id, "trigger_event": trigger_event, "is_active": is_active},
        run,
    )


def inspect_automation_conflicts(
    project_id: Optional[int] = None,
    include_inactive: bool = False,
) -> Dict[str, Any]:
    """Inspect cross-system trigger overlap before changing automation."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        result = _project_automation_conflicts(
            db,
            resolved_project_id,
            include_inactive=include_inactive,
        )
        event_actions = db.query(models.EventAction).filter(
            models.EventAction.project_id == resolved_project_id,
            *([] if include_inactive else [models.EventAction.is_active == True]),
        ).count()
        funnels = db.query(models.Funnel).filter(
            models.Funnel.project_id == resolved_project_id,
            models.Funnel.is_system == False,
            *([] if include_inactive else [models.Funnel.status == "active"]),
        ).count()
        return {
            "project_id": resolved_project_id,
            "automation_counts": {"event_actions": event_actions, "user_funnels": funnels},
            **result,
        }

    return _handle_tool(
        "inspect_automation_conflicts",
        project_id,
        {"project_id": project_id, "include_inactive": include_inactive},
        run,
    )


def lint_funnel_configuration(
    funnel_id: int,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return non-blocking funnel risk findings for an MCP-only operator."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        funnel = db.query(models.Funnel).filter(
            models.Funnel.id == funnel_id,
            models.Funnel.project_id == resolved_project_id,
        ).first()
        if not funnel:
            raise HTTPException(status_code=404, detail="Funnel not found")
        findings = lint_funnel(funnel)
        return {
            "project_id": resolved_project_id,
            "funnel": _row(funnel, ["id", "name", "status", "trigger_type", "trigger_config", "global_exit_config", "is_system", "event_action_id"]),
            "safe_to_activate": not any(item.get("severity") == "error" for item in findings),
            "findings": findings,
        }

    return _handle_tool(
        "lint_funnel_configuration",
        project_id,
        {"project_id": project_id, "funnel_id": funnel_id},
        run,
    )


def create_event_action(
    data: Dict[str, Any],
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate or create an Event Action; writes always require confirmation."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        payload = _model_dump_json(EventActionCreate(**data))
        candidate = models.EventAction(project_id=resolved_project_id, **payload)
        conflicts = _project_automation_conflicts(
            db,
            resolved_project_id,
            candidate=candidate,
        )
        impact = _activation_impact(
            db, "event_action", candidate,
            enabled=bool(payload.get("is_active", True)),
        )
        if dry_run:
            return {
                "dry_run": True,
                "validated": payload,
                "conflicts": conflicts,
                "impact": impact,
                "can_activate": bool(conflicts.get("safe_to_activate", False) and impact["safe_to_activate"]),
                "requires_confirmation": True,
            }
        _reject_blocking_activation(conflicts, is_active=bool(payload.get("is_active", True)))
        _reject_attention_impact(impact)
        action_input = {
            "project_id": resolved_project_id,
            "data": payload,
            "impact_fingerprint": impact.get("impact_fingerprint"),
        }
        if not confirm_token:
            pending = create_pending_action(
                db,
                principal,
                "create_event_action",
                action_input,
                project_id=resolved_project_id,
            )
            return {"status": "pending_confirmation", "conflicts": conflicts, "impact": impact, **pending}
        consume_pending_action(
            db, principal, "create_event_action", confirm_token,
            expected_payload=action_input,
        )
        event_action = models.EventAction(project_id=resolved_project_id, **payload)
        db.add(event_action)
        db.flush()
        compile_event_action(db, event_action)
        db.commit()
        db.refresh(event_action)
        return {"event_action": _row(event_action), "conflicts": conflicts}

    return _handle_tool(
        "create_event_action",
        project_id,
        {"project_id": project_id, "data": data, "dry_run": dry_run},
        run,
    )


def update_event_action(
    action_id: int,
    data: Dict[str, Any],
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate or update an Event Action; writes always require confirmation."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        event_action = db.query(models.EventAction).filter(
            models.EventAction.id == action_id,
            models.EventAction.project_id == resolved_project_id,
        ).first()
        if not event_action:
            raise HTTPException(status_code=404, detail="Event Action not found")
        update_payload = _model_dump_json(EventActionUpdate(**data), exclude_unset=True)
        candidate = models.EventAction(
            id=action_id,
            project_id=resolved_project_id,
            **{column.name: getattr(event_action, column.name) for column in models.EventAction.__table__.columns if column.name not in {"id", "created_at", "updated_at", "project_id"}},
        )
        candidate.updated_at = event_action.updated_at
        for field, value in update_payload.items():
            setattr(candidate, field, value)
        conflicts = _project_automation_conflicts(
            db,
            resolved_project_id,
            candidate=candidate,
            exclude_event_action_id=action_id,
        )
        impact = _activation_impact(
            db, "event_action", candidate, enabled=bool(candidate.is_active),
        )
        if dry_run:
            return {
                "dry_run": True,
                "action_id": action_id,
                "validated_update": update_payload,
                "conflicts": conflicts,
                "impact": impact,
                "can_activate": bool(conflicts.get("safe_to_activate", False) and impact["safe_to_activate"]),
                "requires_confirmation": True,
            }
        _reject_blocking_activation(conflicts, is_active=bool(candidate.is_active))
        _reject_attention_impact(impact)
        action_input = {
            "project_id": resolved_project_id,
            "action_id": action_id,
            "expected_updated_at": event_action.updated_at.isoformat() if event_action.updated_at else None,
            "data": update_payload,
            "impact_fingerprint": impact.get("impact_fingerprint"),
        }
        if not confirm_token:
            pending = create_pending_action(
                db,
                principal,
                "update_event_action",
                action_input,
                project_id=resolved_project_id,
            )
            return {"status": "pending_confirmation", "conflicts": conflicts, "impact": impact, **pending}
        consume_pending_action(
            db, principal, "update_event_action", confirm_token,
            expected_payload=action_input,
        )
        for field, value in update_payload.items():
            setattr(event_action, field, value)
        compile_event_action(db, event_action)
        db.commit()
        db.refresh(event_action)
        return {"event_action": _row(event_action), "conflicts": conflicts}

    return _handle_tool(
        "update_event_action",
        project_id,
        {"project_id": project_id, "action_id": action_id, "data": data, "dry_run": dry_run},
        run,
    )


def set_event_action_state(
    action_id: int,
    is_active: bool,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Pause or activate an Event Action after conflict inspection."""
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.automations:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        event_action = db.query(models.EventAction).filter(
            models.EventAction.id == action_id,
            models.EventAction.project_id == resolved_project_id,
        ).first()
        if not event_action:
            raise HTTPException(status_code=404, detail="Event Action not found")
        candidate = models.EventAction(
            id=action_id,
            project_id=resolved_project_id,
            **{column.name: getattr(event_action, column.name) for column in models.EventAction.__table__.columns if column.name not in {"id", "created_at", "updated_at", "project_id"}},
        )
        candidate.updated_at = event_action.updated_at
        candidate.is_active = is_active
        conflicts = _project_automation_conflicts(
            db,
            resolved_project_id,
            candidate=candidate,
            exclude_event_action_id=action_id,
        ) if is_active else {"safe_to_activate": True, "requires_confirmation": False, "findings": []}
        impact = _activation_impact(db, "event_action", candidate, enabled=is_active)
        if dry_run:
            return {
                "dry_run": True,
                "action_id": action_id,
                "is_active": is_active,
                "conflicts": conflicts,
                "impact": impact,
                "can_apply": bool(conflicts.get("safe_to_activate", False) and impact["safe_to_activate"]),
                "requires_confirmation": True,
            }
        _reject_blocking_activation(conflicts, is_active=is_active)
        _reject_attention_impact(impact)
        action_input = {
            "project_id": resolved_project_id,
            "action_id": action_id,
            "is_active": is_active,
            "expected_updated_at": event_action.updated_at.isoformat() if event_action.updated_at else None,
            "impact_fingerprint": impact.get("impact_fingerprint"),
        }
        if not confirm_token:
            pending = create_pending_action(
                db,
                principal,
                "set_event_action_state",
                action_input,
                project_id=resolved_project_id,
            )
            return {"status": "pending_confirmation", "conflicts": conflicts, "impact": impact, **pending}
        consume_pending_action(
            db, principal, "set_event_action_state", confirm_token,
            expected_payload=action_input,
        )
        event_action.is_active = is_active
        compile_event_action(db, event_action)
        db.commit()
        db.refresh(event_action)
        return {"event_action": _row(event_action), "conflicts": conflicts}

    return _handle_tool(
        "set_event_action_state",
        project_id,
        {"project_id": project_id, "action_id": action_id, "is_active": is_active, "dry_run": dry_run},
        run,
    )


def list_funnels(project_id: Optional[int] = None, include_system: bool = False) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.funnels:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        user = db.query(models.User).filter(models.User.id == principal.user_id).first()
        return {"funnels": FunnelService(db).list_funnels(resolved_project_id, user.workspace_id, include_system)}

    return _handle_tool("list_funnels", project_id, {"project_id": project_id, "include_system": include_system}, run)


def export_funnel(funnel_id: int, project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.funnels:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        user = db.query(models.User).filter(models.User.id == principal.user_id).first()
        funnel = db.query(models.Funnel).filter(models.Funnel.id == funnel_id, models.Funnel.project_id == resolved_project_id).first()
        if not funnel:
            raise HTTPException(status_code=404, detail="Funnel not found")
        return FunnelService(db).export_funnel(funnel_id, user.workspace_id)

    return _handle_tool("export_funnel", project_id, {"project_id": project_id, "funnel_id": funnel_id}, run)


def import_funnel(payload: Dict[str, Any], project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.funnels:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        user = db.query(models.User).filter(models.User.id == principal.user_id).first()
        return FunnelService(db).import_funnel(resolved_project_id, user.workspace_id, principal.user_id, payload)

    return _handle_tool("import_funnel", project_id, {"project_id": project_id, "payload": payload}, run)


def create_draft_funnel(data: Dict[str, Any], project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.funnels:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        user = db.query(models.User).filter(models.User.id == principal.user_id).first()
        return FunnelService(db).create_funnel(resolved_project_id, user.workspace_id, principal.user_id, data)

    return _handle_tool("create_draft_funnel", project_id, {"project_id": project_id, "data": data}, run)


def update_funnel_steps(funnel_id: int, steps: List[Dict[str, Any]], project_id: Optional[int] = None, dry_run: bool = True) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.funnels:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        funnel = db.query(models.Funnel).filter(models.Funnel.id == funnel_id, models.Funnel.project_id == resolved_project_id).first()
        if not funnel:
            raise HTTPException(status_code=404, detail="Funnel not found")
        if dry_run:
            return {"dry_run": True, "funnel_id": funnel_id, "step_count": len(steps), "status": funnel.status}
        if funnel.status == "active":
            raise HTTPException(status_code=400, detail="Pause the funnel before replacing its steps")
        user = db.query(models.User).filter(models.User.id == principal.user_id).first()
        service = FunnelService(db)
        service._get_funnel(funnel_id, user.workspace_id)
        current = db.query(models.FunnelStep).filter(models.FunnelStep.funnel_id == funnel_id).all()
        for step in current:
            step.parent_step_id = None
        db.flush()
        for step in current:
            db.delete(step)
        db.flush()

        export_id_to_real_id: dict[int, int] = {}
        remaining = list(steps)
        while remaining:
            batch = [
                item for item in remaining
                if item.get("parent_export_id") is None or item.get("parent_export_id") in export_id_to_real_id
            ]
            if not batch:
                raise HTTPException(status_code=400, detail="Steps contain unresolved parent references")
            for item in batch:
                parent_id = export_id_to_real_id.get(item.get("parent_export_id")) if item.get("parent_export_id") else None
                step = models.FunnelStep(
                    funnel_id=funnel_id,
                    step_type=item["step_type"],
                    step_config=item.get("step_config") or {},
                    position=item.get("position", 0),
                    parent_step_id=parent_id,
                    branch=item.get("branch", "main"),
                )
                db.add(step)
                db.flush()
                if item.get("_export_id") is not None:
                    export_id_to_real_id[item["_export_id"]] = step.id
                remaining.remove(item)
        db.commit()
        return {"funnel_id": funnel_id, "step_count": len(steps), "status": funnel.status}

    return _handle_tool("update_funnel_steps", project_id, {"project_id": project_id, "funnel_id": funnel_id, "step_count": len(steps), "dry_run": dry_run}, run)


def activate_funnel(funnel_id: int, project_id: Optional[int] = None, dry_run: bool = True, confirm_token: Optional[str] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.funnels:write"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "editor")
        user = db.query(models.User).filter(models.User.id == principal.user_id).first()
        service = FunnelService(db)
        funnel = service._get_funnel(funnel_id, user.workspace_id)
        refs = service.check_references(funnel_id, user.workspace_id)
        unresolved = [ref for ref in refs if not ref.get("resolved")]
        impact = _activation_impact(db, "funnel", funnel)
        if dry_run:
            return {
                "dry_run": True,
                "can_activate": not unresolved and bool(impact["safe_to_activate"]),
                "unresolved_references": unresolved,
                "impact": impact,
                "external_sends": 0,
            }
        if unresolved:
            raise HTTPException(status_code=400, detail="Cannot activate funnel with unresolved references")
        _reject_attention_impact(impact)
        action_input = {
            "project_id": resolved_project_id,
            "funnel_id": funnel_id,
            "impact_fingerprint": impact["impact_fingerprint"],
        }
        if not confirm_token:
            pending = create_pending_action(
                db,
                principal,
                "activate_funnel",
                action_input,
                project_id=resolved_project_id,
            )
            return {"status": "pending_confirmation", "impact": impact, "external_sends": 0, **pending}
        consume_pending_action(
            db, principal, "activate_funnel", confirm_token,
            expected_payload=action_input,
        )
        activated = service.activate_funnel(
            funnel_id, user.workspace_id, impact["impact_fingerprint"],
        )
        return {
            "dry_run": False,
            "status": "activated",
            "funnel": activated,
            "impact": impact,
            "external_sends": 0,
        }

    return _handle_tool("activate_funnel", project_id, {"project_id": project_id, "funnel_id": funnel_id, "dry_run": dry_run}, run)


def search_contacts(query: str, project_id: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.contacts:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "support_agent")
        like = f"%{query}%"
        contacts = (
            db.query(MessagingUser)
            .filter(
                MessagingUser.project_id == resolved_project_id,
                (MessagingUser.name.ilike(like))
                | (MessagingUser.email.ilike(like))
                | (MessagingUser.phone.ilike(like))
                | (MessagingUser.external_id.ilike(like)),
            )
            .order_by(MessagingUser.last_seen_at.desc())
            .limit(min(limit, 100))
            .all()
        )
        return {"contacts": [_row(c) for c in contacts]}

    return _handle_tool("search_contacts", project_id, {"project_id": project_id, "query": query, "limit": limit}, run)


def get_contact_timeline(contact_id: int, project_id: Optional[int] = None, limit: int = 50) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.contacts:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "support_agent")
        contact = db.query(MessagingUser).filter(MessagingUser.id == contact_id, MessagingUser.project_id == resolved_project_id).first()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
        events = (
            db.query(MessagingEvent)
            .filter(MessagingEvent.project_id == resolved_project_id, MessagingEvent.user_id == contact_id)
            .order_by(MessagingEvent.created_at.desc())
            .limit(min(limit, 200))
            .all()
        )
        return {
            "contact": _row(contact),
            "events": [_row(e, ["id", "event_name", "properties", "source", "created_at"]) for e in events],
        }

    return _handle_tool("get_contact_timeline", project_id, {"project_id": project_id, "contact_id": contact_id}, run)


def list_chatbots(project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.agents:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        chatbots = (
            db.query(models.Chatbot)
            .filter(models.Chatbot.project_id == resolved_project_id, models.Chatbot.status != "archived")
            .order_by(models.Chatbot.name)
            .all()
        )
        return {"chatbots": [_row(c) for c in chatbots]}

    return _handle_tool("list_chatbots", project_id, {"project_id": project_id}, run)


def list_agent_teams(project_id: Optional[int] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.agents:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "viewer")
        teams = (
            db.query(models.AgentTeam)
            .filter(models.AgentTeam.project_id == resolved_project_id)
            .order_by(models.AgentTeam.name)
            .all()
        )
        return {"agent_teams": [_row(t) for t in teams]}

    return _handle_tool("list_agent_teams", project_id, {"project_id": project_id}, run)


def list_channel_health(project_id: Optional[int] = None, hours: int = 24) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_scopes(["versya.channels:read"])
        resolved_project_id = _resolve_project_id(db, principal, project_id)
        require_project_access(db, resolved_project_id, "admin")
        svc = ChannelHealthService(db)
        return {
            "summary": svc.get_project_health(resolved_project_id, hours),
            "instances": svc.get_instances_summary(resolved_project_id, hours),
        }

    return _handle_tool("list_channel_health", project_id, {"project_id": project_id, "hours": hours}, run)


# ---------------------------------------------------------------------------
# Admin MCP tools
# ---------------------------------------------------------------------------


def get_runtime_health() -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_admin_principal()
        db_ok = db.execute(text("SELECT 1")).scalar() == 1
        from app.services.realtime import redis_pubsub
        from app.services.event_actions.scheduler import scheduled_action_worker
        from app.services.funnel_scheduler import funnel_scheduler_worker
        from app.services.scoring.scoring_scheduler import scoring_scheduler_worker
        from app.services.segment_scheduler import segment_scheduler_worker
        from app.services.inbox_expiry_worker import inbox_expiry_worker
        from app.services.webhook.webhook_worker import webhook_worker
        from app.services.messaging.destination_worker import destination_delivery_worker
        from app.services.messaging.audience_sync_worker import audience_sync_worker
        from app.services.messaging.audience_webhook_worker import audience_webhook_worker
        from app.services.channels.scheduled_send_worker import scheduled_send_worker
        from app.services.scoring.mes_scheduler import mes_scheduler_worker
        from app.services.journey.materializer import journey_materializer_worker

        return {
            "backend": "healthy",
            "database": "healthy" if db_ok else "unhealthy",
            "redis": "healthy" if redis_pubsub.is_available() else "unavailable",
            "workers": {
                "scheduled_action_worker": getattr(scheduled_action_worker, "_running", False),
                "funnel_scheduler_worker": getattr(funnel_scheduler_worker, "_running", False),
                "scoring_scheduler_worker": getattr(scoring_scheduler_worker, "_running", False),
                "segment_scheduler_worker": getattr(segment_scheduler_worker, "_running", False),
                "inbox_expiry_worker": getattr(inbox_expiry_worker, "_running", False),
                "webhook_worker": getattr(webhook_worker, "_running", False),
                "destination_delivery_worker": getattr(destination_delivery_worker, "_running", False),
                "audience_sync_worker": getattr(audience_sync_worker, "_running", False),
                "audience_webhook_worker": getattr(audience_webhook_worker, "_running", False),
                "scheduled_send_worker": getattr(scheduled_send_worker, "_running", False),
                "mes_scheduler_worker": getattr(mes_scheduler_worker, "_running", False),
                "journey_materializer_worker": getattr(journey_materializer_worker, "_running", False),
            },
            "mcp_enabled": get_mcp_settings().enabled,
            "mcp_public_base_url": mcp_resource_url(""),
        }

    return _handle_tool("get_runtime_health", None, {}, run)


def get_deployed_version() -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_admin_principal()
        version_path = Path(__file__).resolve().parents[2] / "sdk" / "version.py"
        public_version = Path(__file__).resolve().parents[3] / "public" / "version.json"
        return {
            "backend_cwd": os.getcwd(),
            "sdk_version_file_exists": version_path.exists(),
            "frontend_version_file_exists": public_version.exists(),
        }

    return _handle_tool("get_deployed_version", None, {}, run)


def read_deployed_source(path: str, max_bytes: int = 20000) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_admin_principal()
        app_root = Path(__file__).resolve().parents[1]
        target = (app_root / path).resolve()
        if not str(target).startswith(str(app_root.resolve())):
            raise HTTPException(status_code=403, detail="Can only read backend app source files")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Source file not found")
        if target.suffix not in {".py", ".md", ".txt", ".json"}:
            raise HTTPException(status_code=400, detail="File type is not readable through admin MCP")
        data = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(data.encode("utf-8")) > max_bytes
        return {"path": str(target.relative_to(app_root)), "text": data[:max_bytes], "truncated": truncated}

    return _handle_tool("read_deployed_source", None, {"path": path, "max_bytes": max_bytes}, run)


def list_db_tables() -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_admin_principal()
        inspector = inspect(engine)
        return {"tables": sorted(inspector.get_table_names())}

    return _handle_tool("list_db_tables", None, {}, run)


def describe_table(table_name: str) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_admin_principal()
        inspector = inspect(engine)
        if table_name not in inspector.get_table_names():
            raise HTTPException(status_code=404, detail="Table not found")
        return {
            "table": table_name,
            "columns": [
                {"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]}
                for c in inspector.get_columns(table_name)
            ],
            "indexes": inspector.get_indexes(table_name),
        }

    return _handle_tool("describe_table", None, {"table_name": table_name}, run)


MUTATING_SQL_RE = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|execute)\b", re.I)


def _readonly_sql(sql: str) -> str:
    cleaned = sql.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if ";" in cleaned:
        raise HTTPException(status_code=400, detail="Only one SELECT statement is allowed")
    if not re.match(r"^(select|with)\b", cleaned, re.I):
        raise HTTPException(status_code=400, detail="Only SELECT statements are allowed")
    if MUTATING_SQL_RE.search(cleaned):
        raise HTTPException(status_code=400, detail="Mutating SQL is not allowed")
    if not re.search(r"\blimit\b", cleaned, re.I):
        cleaned = f"{cleaned} LIMIT 100"
    return cleaned


def run_readonly_sql(sql: str, limit: int = 100) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_admin_principal()
        safe_sql = _readonly_sql(sql)
        max_limit = max(1, min(limit, 500))
        db.execute(text(f"SET LOCAL statement_timeout = {get_mcp_settings().readonly_sql_timeout_ms}"))
        result = db.execute(text(safe_sql))
        rows = [dict(row._mapping) for row in result.fetchmany(max_limit)]
        return {"rows": rows, "row_count": len(rows), "truncated": len(rows) == max_limit}

    return _handle_tool("run_readonly_sql", None, {"sql": sql, "limit": limit}, run)


def run_maintenance_action(action: str, payload: Optional[Dict[str, Any]] = None, dry_run: bool = True, confirm_token: Optional[str] = None) -> Dict[str, Any]:
    def run(db: Session, principal: McpPrincipal):
        require_admin_principal()
        payload_data = payload or {}
        allowed = {"inspect_scheduler_state", "retry_failed_webhook_ingest"}
        if action not in allowed:
            raise HTTPException(status_code=400, detail=f"Unsupported maintenance action: {action}")

        if action == "inspect_scheduler_state":
            from app.services.event_actions.scheduler import scheduled_action_worker
            from app.services.funnel_scheduler import funnel_scheduler_worker
            from app.services.scoring.scoring_scheduler import scoring_scheduler_worker
            from app.services.segment_scheduler import segment_scheduler_worker
            from app.services.inbox_expiry_worker import inbox_expiry_worker
            from app.services.webhook.webhook_worker import webhook_worker
            from app.services.messaging.destination_worker import destination_delivery_worker
            from app.services.messaging.audience_sync_worker import audience_sync_worker
            from app.services.messaging.audience_webhook_worker import audience_webhook_worker
            from app.services.channels.scheduled_send_worker import scheduled_send_worker
            from app.services.scoring.mes_scheduler import mes_scheduler_worker
            from app.services.journey.materializer import journey_materializer_worker

            return {
                "action": action,
                "scheduler_lock_id": 737373,
                "workers": {
                    "scheduled_action_worker": getattr(scheduled_action_worker, "_running", False),
                    "funnel_scheduler_worker": getattr(funnel_scheduler_worker, "_running", False),
                    "scoring_scheduler_worker": getattr(scoring_scheduler_worker, "_running", False),
                    "segment_scheduler_worker": getattr(segment_scheduler_worker, "_running", False),
                    "inbox_expiry_worker": getattr(inbox_expiry_worker, "_running", False),
                    "webhook_worker": getattr(webhook_worker, "_running", False),
                    "destination_delivery_worker": getattr(destination_delivery_worker, "_running", False),
                    "audience_sync_worker": getattr(audience_sync_worker, "_running", False),
                    "audience_webhook_worker": getattr(audience_webhook_worker, "_running", False),
                    "scheduled_send_worker": getattr(scheduled_send_worker, "_running", False),
                    "mes_scheduler_worker": getattr(mes_scheduler_worker, "_running", False),
                    "journey_materializer_worker": getattr(journey_materializer_worker, "_running", False),
                },
            }

        if action == "retry_failed_webhook_ingest":
            project_id = payload_data.get("project_id")
            if not project_id:
                raise HTTPException(status_code=400, detail="project_id is required")
            source_slug = payload_data.get("source_slug")
            limit = max(1, min(int(payload_data.get("limit") or 100), 500))
            q = db.query(models.WebhookIngest.id).filter(
                models.WebhookIngest.project_id == int(project_id),
                models.WebhookIngest.processing_status == "failed",
            )
            if source_slug:
                q = q.filter(models.WebhookIngest.source_slug == source_slug)
            ingest_ids = [row[0] for row in q.order_by(models.WebhookIngest.received_at.asc()).limit(limit).all()]
            if dry_run:
                return {
                    "dry_run": True,
                    "action": action,
                    "project_id": int(project_id),
                    "source_slug": source_slug,
                    "matched_count": len(ingest_ids),
                    "limit": limit,
                }
            if not confirm_token:
                pending = create_pending_action(db, principal, "run_maintenance_action", {"action": action, "payload": payload_data})
                return {"status": "pending_confirmation", **pending}
            consume_pending_action(db, principal, "run_maintenance_action", confirm_token)
            if not ingest_ids:
                return {"action": action, "queued_count": 0}
            queued_count = db.query(models.WebhookIngest).filter(
                models.WebhookIngest.id.in_(ingest_ids)
            ).update({
                models.WebhookIngest.processing_status: "queued",
                models.WebhookIngest.retry_count: 0,
                models.WebhookIngest.next_retry_at: None,
                models.WebhookIngest.error_message: None,
                models.WebhookIngest.updated_at: datetime.utcnow(),
            }, synchronize_session=False)
            db.commit()
            return {
                "action": action,
                "project_id": int(project_id),
                "source_slug": source_slug,
                "queued_count": queued_count,
            }

        if dry_run:
            return {"dry_run": True, "action": action, "payload": payload_data}
        if not confirm_token:
            pending = create_pending_action(db, principal, "run_maintenance_action", {"action": action, "payload": payload_data})
            return {"status": "pending_confirmation", **pending}
        consume_pending_action(db, principal, "run_maintenance_action", confirm_token)
        raise HTTPException(status_code=400, detail=f"Unsupported maintenance action: {action}")

    return _handle_tool("run_maintenance_action", None, {"action": action, "payload": payload, "dry_run": dry_run}, run)
