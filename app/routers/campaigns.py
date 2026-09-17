"""Project-scoped campaign, plan, run and wave API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.models import Project, User
from app.models.campaigns import (
    CampaignPolicyOverride,
    CampaignRecipient,
    CampaignRun,
    CampaignWave,
)
from app.routers.auth import get_current_user
from app.schemas.campaigns import (
    CampaignCreate,
    CampaignConsequenceRequest,
    CampaignPreviewRequest,
    CampaignResponse,
    CampaignRunCreate,
    CampaignRunResponse,
    CampaignPolicyOverrideResponse,
    CampaignUpdate,
)
from app.schemas.orchestration_attention import AttentionActivationApproval
from app.services.campaigns.capacity import CapacityPlanError
from app.services.campaigns.eligibility import CampaignPolicyError
from app.services.campaigns.recurrence import RecurrenceError
from app.services.campaigns.policy_overrides import (
    CampaignOverrideError,
    approve_override,
    revoke_override,
)
from app.services.campaigns.service import (
    AudienceChangedError,
    CampaignError,
    CampaignService,
)
from app.services.contact_groups.compiler import GroupFilterError

router = APIRouter(prefix="/projects/{project_id}/campaigns", tags=["campaigns"])


def _ensure_project(db: Session, project_id: int, current_user: User) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id,
        Project.is_active == True,  # noqa: E712
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _campaign_or_404(db: Session, project_id: int, campaign_id: int):
    row = CampaignService(db).get(project_id, campaign_id)
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return row


def _run_or_404(db: Session, project_id: int, campaign_id: int, run_id: int) -> CampaignRun:
    row = db.query(CampaignRun).filter(
        CampaignRun.id == run_id,
        CampaignRun.project_id == project_id,
        CampaignRun.campaign_id == campaign_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign run not found")
    return row


def _campaign_dict(campaign) -> dict:
    runs = list(campaign.runs or [])
    return {
        "id": campaign.id,
        "project_id": campaign.project_id,
        "name": campaign.name,
        "description": campaign.description,
        "campaign_type": campaign.campaign_type,
        "status": campaign.status,
        "default_channel": campaign.default_channel,
        "selection_config": campaign.selection_config,
        "policy_config": campaign.policy_config,
        "recurrence_config": campaign.recurrence_config,
        "opportunity_config": getattr(campaign, "opportunity_config", None),
        "timezone": campaign.timezone,
        "starts_at": campaign.starts_at,
        "target_at": campaign.target_at,
        "external_key": campaign.external_key,
        "purpose_key": getattr(campaign, "purpose_key", None),
        "attention_policy": getattr(campaign, "attention_policy", None),
        "version": campaign.version,
        "created_by_user_id": campaign.created_by_user_id,
        "created_at": campaign.created_at,
        "updated_at": campaign.updated_at,
        "audience_count": sum(int(run.candidate_count or 0) for run in runs),
        "eligible_count": sum(int(run.eligible_count or 0) for run in runs),
        "sent_count": sum(int(run.sent_count or 0) for run in runs),
        "delivered_count": sum(int(run.delivered_count or 0) for run in runs),
        "failed_count": sum(int(run.failed_count or 0) for run in runs),
        "actions": [{
            "id": action.id,
            "position": action.position,
            "action_type": action.action_type,
            "channel": action.channel,
            "config": action.config,
            "status": action.status,
            "variants": [{
                "id": variant.id,
                "variant_key": variant.variant_key,
                "locale": variant.locale,
                "template_id": variant.template_id,
                "subject": variant.subject,
                "body": variant.body,
                "variant_config": variant.variant_config,
                "weight": variant.weight,
                "status": variant.status,
            } for variant in action.variants if variant.status != "archived"],
        } for action in campaign.actions if action.status != "archived"],
    }


_VALIDATION_ERRORS = (
    CampaignError,
    CampaignOverrideError,
    CampaignPolicyError,
    CapacityPlanError,
    RecurrenceError,
    GroupFilterError,
)


@router.get("", response_model=list[CampaignResponse])
def list_campaigns(
    project_id: int,
    include_archived: bool = False,
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return [
        _campaign_dict(campaign)
        for campaign in CampaignService(db).list(project_id, include_archived)
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_campaign(
    project_id: int,
    payload: CampaignCreate,
    auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        row = CampaignService(db).create(
            project_id,
            payload.model_dump(),
            auth.get("user_id"),
        )
        return _campaign_dict(row)
    except _VALIDATION_ERRORS as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Campaign name or external key already exists") from exc


@router.get("/{campaign_id}")
def get_campaign(
    project_id: int,
    campaign_id: int,
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return _campaign_dict(_campaign_or_404(db, project_id, campaign_id))


@router.put("/{campaign_id}")
def update_campaign(
    project_id: int,
    campaign_id: int,
    payload: CampaignUpdate,
    auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        row = CampaignService(db).update(
            _campaign_or_404(db, project_id, campaign_id),
            payload.model_dump(exclude_unset=True),
            auth.get("user_id"),
        )
        return _campaign_dict(row)
    except _VALIDATION_ERRORS as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Campaign name or external key already exists") from exc


@router.delete("/{campaign_id}", response_model=CampaignResponse)
def archive_campaign(
    project_id: int,
    campaign_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return _campaign_dict(
            CampaignService(db).archive(_campaign_or_404(db, project_id, campaign_id))
        )
    except CampaignError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{campaign_id}/preview-plan")
def preview_campaign_plan(
    project_id: int,
    campaign_id: int,
    payload: CampaignPreviewRequest,
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return CampaignService(db).preview_plan(
            _campaign_or_404(db, project_id, campaign_id),
            payload.schedule,
        )
    except _VALIDATION_ERRORS as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{campaign_id}/attention-impact")
def preview_campaign_attention_impact(
    project_id: int,
    campaign_id: int,
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    from app.services.orchestration_impact_service import OrchestrationImpactService

    return OrchestrationImpactService(db).preview(
        "campaign", _campaign_or_404(db, project_id, campaign_id),
    )


@router.post("/{campaign_id}/consequences")
def evaluate_campaign_consequences(
    project_id: int,
    campaign_id: int,
    payload: CampaignConsequenceRequest,
    auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        from app.services.decision_gate_service import DecisionGateService
        row = DecisionGateService(db).evaluate_campaign(
            _campaign_or_404(db, project_id, campaign_id), payload.schedule, auth.get("user_id"),
            forced_override=payload.policy_override.model_dump() if payload.policy_override else None,
        )
        return {"id": row.id, "project_id": row.project_id, "gate_type": row.gate_type, "subject_type": row.subject_type, "subject_id": row.subject_id, "subject_version": row.subject_version, "status": row.status, "method": row.method, "baseline": row.baseline, "options": row.options, "provenance": row.provenance, "evaluated_at": row.evaluated_at, "expires_at": row.expires_at}
    except _VALIDATION_ERRORS as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{campaign_id}/activate", response_model=CampaignResponse)
def activate_campaign(
    project_id: int,
    campaign_id: int,
    approval: AttentionActivationApproval,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    campaign = _campaign_or_404(db, project_id, campaign_id)
    try:
        campaign, _impact = CampaignService(db).activate(
            campaign,
            impact_fingerprint=approval.impact_fingerprint,
        )
        return _campaign_dict(campaign)
    except Exception as exc:
        from app.services.orchestration_impact_service import OrchestrationImpactError
        if isinstance(exc, OrchestrationImpactError):
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "orchestration_impact_blocked",
                    "message": "Resolve attention impacts before activating this campaign.",
                    "impact": exc.report,
                },
            ) from exc
        if not isinstance(exc, _VALIDATION_ERRORS):
            raise
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{campaign_id}/pause", response_model=CampaignResponse)
def pause_campaign(
    project_id: int,
    campaign_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    campaign = _campaign_or_404(db, project_id, campaign_id)
    campaign.status = "paused"
    db.commit()
    db.refresh(campaign)
    return _campaign_dict(campaign)


@router.post("/{campaign_id}/runs", response_model=CampaignRunResponse, status_code=status.HTTP_201_CREATED)
def create_campaign_run(
    project_id: int,
    campaign_id: int,
    payload: CampaignRunCreate,
    auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    campaign = _campaign_or_404(db, project_id, campaign_id)
    try:
        if payload.policy_override and payload.decision_choice_id:
            raise CampaignError("Use either policy_override or decision_choice_id, not both")
        choice_id = payload.decision_choice_id
        if payload.policy_override and not choice_id:
            from app.services.decision_gate_service import DecisionGateService
            gate = DecisionGateService(db)
            evaluation = gate.evaluate_campaign(
                campaign, payload.schedule, auth.get("user_id"),
                forced_override=payload.policy_override.model_dump(),
            )
            choice_id = gate.choose(
                project_id, evaluation.id, "meet_deadline", auth.get("user_id"),
                "Legacy policy_override converted to an audited decision",
            ).id
        return CampaignService(db).create_run(
            campaign,
            schedule=payload.schedule,
            requested_by_user_id=auth.get("user_id"),
            expected={
                "campaign_version": payload.expected_campaign_version,
                "candidate_count": payload.expected_candidate_count,
                "eligible_count": payload.expected_eligible_count,
                "planned_count": payload.expected_planned_count,
            },
            run_key=payload.run_key,
            policy_override=(
                payload.policy_override.model_dump()
                if payload.policy_override and not choice_id
                else None
            ),
            decision_choice_id=choice_id,
        )
    except AudienceChangedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"message": str(exc), "actual": exc.actual}) from exc
    except _VALIDATION_ERRORS as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Campaign run key already exists") from exc


@router.get("/{campaign_id}/runs", response_model=list[CampaignRunResponse])
def list_campaign_runs(
    project_id: int,
    campaign_id: int,
    limit: int = Query(50, ge=1, le=250),
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    _campaign_or_404(db, project_id, campaign_id)
    return db.query(CampaignRun).filter(
        CampaignRun.project_id == project_id,
        CampaignRun.campaign_id == campaign_id,
    ).order_by(CampaignRun.created_at.desc()).limit(limit).all()


@router.get("/{campaign_id}/runs/{run_id}")
def get_campaign_run(
    project_id: int,
    campaign_id: int,
    run_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=250),
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    run = _run_or_404(db, project_id, campaign_id, run_id)
    recipients = db.query(CampaignRecipient).filter(
        CampaignRecipient.project_id == project_id,
        CampaignRecipient.run_id == run.id,
    ).order_by(CampaignRecipient.id).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "run": CampaignRunResponse.model_validate(run).model_dump(),
        "waves": [{
            "id": wave.id,
            "position": wave.position,
            "status": wave.status,
            "approval_mode": wave.approval_mode,
            "is_canary": wave.is_canary,
            "approved_at": wave.approved_at,
            "approved_by_user_id": wave.approved_by_user_id,
            "scheduled_at": wave.scheduled_at,
            "planned_count": wave.planned_count,
            "sent_count": wave.sent_count,
            "failed_count": wave.failed_count,
            "skipped_count": wave.skipped_count,
        } for wave in run.waves],
        "recipients": [{
            "id": recipient.id,
            "user_id": recipient.user_id,
            "endpoint_id": recipient.endpoint_id,
            "channel": recipient.channel,
            "status": recipient.status,
            "suppression_reason": recipient.suppression_reason,
            "delivery_profile_id": recipient.delivery_profile_id,
            "scheduled_at": recipient.scheduled_at,
            "send_log_id": recipient.send_log_id,
            "attempt_count": recipient.attempt_count,
            "last_error_code": recipient.last_error_code,
        } for recipient in recipients],
        "recipient_total": run.planned_count + run.skipped_count,
        "page": page,
        "page_size": page_size,
    }


@router.post("/{campaign_id}/runs/{run_id}/cancel", response_model=CampaignRunResponse)
def cancel_campaign_run(
    project_id: int,
    campaign_id: int,
    run_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return CampaignService(db).cancel_run(_run_or_404(db, project_id, campaign_id, run_id))


@router.post(
    "/{campaign_id}/runs/{run_id}/policy-override/approve",
    response_model=CampaignPolicyOverrideResponse,
)
def approve_campaign_policy_override(
    project_id: int,
    campaign_id: int,
    run_id: int,
    auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    run = _run_or_404(db, project_id, campaign_id, run_id)
    override = db.query(CampaignPolicyOverride).filter(
        CampaignPolicyOverride.project_id == project_id,
        CampaignPolicyOverride.run_id == run.id,
    ).with_for_update().first()
    if not override:
        raise HTTPException(status_code=404, detail="Campaign policy override not found")
    try:
        return approve_override(db, override, int(auth.get("user_id")))
    except CampaignOverrideError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{campaign_id}/runs/{run_id}/policy-override/revoke",
    response_model=CampaignPolicyOverrideResponse,
)
def revoke_campaign_policy_override(
    project_id: int,
    campaign_id: int,
    run_id: int,
    auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    run = _run_or_404(db, project_id, campaign_id, run_id)
    override = db.query(CampaignPolicyOverride).filter(
        CampaignPolicyOverride.project_id == project_id,
        CampaignPolicyOverride.run_id == run.id,
    ).with_for_update().first()
    if not override:
        raise HTTPException(status_code=404, detail="Campaign policy override not found")
    try:
        row = revoke_override(db, override, int(auth.get("user_id")))
        if run.status not in {"completed", "canceled", "failed"}:
            CampaignService(db).cancel_run(run)
        return row
    except CampaignOverrideError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{campaign_id}/runs/{run_id}/waves/{wave_id}/approve")
def approve_campaign_wave(
    project_id: int,
    campaign_id: int,
    run_id: int,
    wave_id: int,
    auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    _run_or_404(db, project_id, campaign_id, run_id)
    wave = db.query(CampaignWave).filter(
        CampaignWave.id == wave_id,
        CampaignWave.project_id == project_id,
        CampaignWave.run_id == run_id,
    ).first()
    if not wave:
        raise HTTPException(status_code=404, detail="Campaign wave not found")
    try:
        approved = CampaignService(db).approve_wave(wave, auth.get("user_id"))
        return {"id": approved.id, "status": approved.status}
    except CampaignError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
