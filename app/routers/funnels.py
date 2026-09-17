"""REST API router for the Funnel Engine."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from app.schemas.funnels import (
    FunnelCreate, FunnelUpdate, FunnelResponse, FunnelListResponse,
    FunnelStepCreate, FunnelStepUpdate, FunnelStepResponse,
    StepReorderRequest, MoveStepRequest, StepRestoreRequest,
    FunnelEnrollmentResponse, FunnelEnrollmentLogResponse,
    MilestoneResponse,
    FunnelImportRequest, FunnelImportResponse, ResolveReferencesRequest,
    ManualEnrollRequest, ManualEnrollResponse,
)
from app.schemas.orchestration_attention import AttentionActivationApproval
from app.services.funnel_service import FunnelService
from app.models import FunnelEnrollment, FunnelEnrollmentLog, WhatsAppInstance, Project
from app.models.messaging import MessagingUser

router = APIRouter(tags=["funnels"])


def _ws(user: User):
    if not user.workspace_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User must be associated with a workspace")
    return user.workspace_id


# ============================================================================
# Funnel CRUD
# ============================================================================

@router.get("/projects/{project_id}/funnels", response_model=List[FunnelListResponse])
def list_funnels(
    project_id: int,
    include_system: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.list_funnels(project_id, _ws(current_user), include_system=include_system)


@router.post("/projects/{project_id}/funnels", response_model=FunnelResponse, status_code=status.HTTP_201_CREATED)
def create_funnel(
    project_id: int,
    data: FunnelCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.create_funnel(project_id, _ws(current_user), current_user.id, data.model_dump())


@router.get("/funnels/{funnel_id}", response_model=FunnelResponse)
def get_funnel(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.get_funnel(funnel_id, _ws(current_user))


@router.get("/funnels/{funnel_id}/lint")
def lint_funnel_endpoint(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Escape-hatch lint: opinionated risk findings for a hand-built funnel
    (forks / missing stop / hardcoded channels). Advisory — never blocks."""
    from app.models import Funnel
    from app.services.funnel.lint import lint_funnel
    funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funnel not found")
    findings = lint_funnel(funnel)
    return {"funnel_id": funnel_id, "findings": findings, "count": len(findings)}


@router.get("/funnels/{funnel_id}/attention-impact")
def preview_funnel_attention_impact(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models import Funnel
    from app.services.orchestration_impact_service import OrchestrationImpactService

    funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funnel not found")
    FunnelService(db)._get_funnel(funnel_id, _ws(current_user))
    return OrchestrationImpactService(db).preview("funnel", funnel)


@router.put("/funnels/{funnel_id}", response_model=FunnelResponse)
def update_funnel(
    funnel_id: int,
    data: FunnelUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.update_funnel(funnel_id, _ws(current_user), data.model_dump(exclude_unset=True))


@router.delete("/funnels/{funnel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_funnel(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    svc.delete_funnel(funnel_id, _ws(current_user))


@router.post("/funnels/{funnel_id}/activate", response_model=FunnelResponse)
def activate_funnel(
    funnel_id: int,
    approval: AttentionActivationApproval,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.activate_funnel(
        funnel_id, _ws(current_user), approval.impact_fingerprint,
    )


@router.post("/funnels/{funnel_id}/pause", response_model=FunnelResponse)
def pause_funnel(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.pause_funnel(funnel_id, _ws(current_user))


# ============================================================================
# Steps CRUD
# ============================================================================

@router.get("/funnels/{funnel_id}/steps", response_model=List[FunnelStepResponse])
def list_steps(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.list_steps(funnel_id, _ws(current_user))


@router.post("/funnels/{funnel_id}/steps", response_model=FunnelStepResponse, status_code=status.HTTP_201_CREATED)
def create_step(
    funnel_id: int,
    data: FunnelStepCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.create_step(funnel_id, _ws(current_user), data.model_dump())


@router.put("/funnels/{funnel_id}/steps/restore", response_model=FunnelResponse)
def restore_steps(
    funnel_id: int,
    data: StepRestoreRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.restore_steps(funnel_id, _ws(current_user), [s.model_dump() for s in data.steps])


@router.put("/funnels/{funnel_id}/steps/reorder", response_model=List[FunnelStepResponse])
def reorder_steps(
    funnel_id: int,
    data: StepReorderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.reorder_steps(funnel_id, _ws(current_user), [s.model_dump() for s in data.steps])


@router.put("/funnels/{funnel_id}/steps/{step_id}", response_model=FunnelStepResponse)
def update_step(
    funnel_id: int,
    step_id: int,
    data: FunnelStepUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.update_step(funnel_id, step_id, _ws(current_user), data.model_dump(exclude_unset=True))


@router.delete("/funnels/{funnel_id}/steps/{step_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_step(
    funnel_id: int,
    step_id: int,
    promote_branch: Optional[str] = Query(None, description="Branch to promote when deleting a branching step"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    svc.delete_step(funnel_id, step_id, _ws(current_user), promote_branch=promote_branch)


@router.post("/funnels/{funnel_id}/steps/{step_id}/move", response_model=List[FunnelStepResponse])
def move_step(
    funnel_id: int,
    step_id: int,
    data: MoveStepRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.move_step(
        funnel_id, step_id, _ws(current_user),
        target_parent_step_id=data.target_parent_step_id,
        target_branch=data.target_branch,
        target_position=data.target_position,
    )


# ============================================================================
# Export / Import
# ============================================================================

@router.get("/funnels/{funnel_id}/export")
def export_funnel(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.export_funnel(funnel_id, _ws(current_user))


@router.post("/projects/{project_id}/funnels/import", response_model=FunnelImportResponse, status_code=status.HTTP_201_CREATED)
def import_funnel(
    project_id: int,
    data: FunnelImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.import_funnel(project_id, _ws(current_user), current_user.id, data.model_dump())


@router.get("/funnels/{funnel_id}/references")
def check_references(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.check_references(funnel_id, _ws(current_user))


@router.post("/funnels/{funnel_id}/references/resolve")
def resolve_references(
    funnel_id: int,
    data: ResolveReferencesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    return svc.resolve_references(funnel_id, _ws(current_user), [r.model_dump() for r in data.resolutions])


# ============================================================================
# Enrollments (read-only)
# ============================================================================

@router.get("/funnels/{funnel_id}/enrollments", response_model=List[FunnelEnrollmentResponse])
def list_enrollments(
    funnel_id: int,
    status: str = None,
    search: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import or_, cast, String
    svc = FunnelService(db)
    svc._get_funnel(funnel_id, _ws(current_user))  # access check
    query = db.query(FunnelEnrollment).join(
        MessagingUser, FunnelEnrollment.user_id == MessagingUser.id
    ).filter(FunnelEnrollment.funnel_id == funnel_id)
    if status:
        query = query.filter(FunnelEnrollment.status == status)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                MessagingUser.name.ilike(search_term),
                MessagingUser.email.ilike(search_term),
                cast(MessagingUser.id, String).ilike(search_term),
                MessagingUser.external_id.ilike(search_term),
            )
        )
    rows = query.order_by(FunnelEnrollment.enrolled_at.desc()).limit(200).all()
    result = []
    for enr in rows:
        user = enr.user
        data = FunnelEnrollmentResponse.model_validate(enr)
        if user:
            data.user_name = user.name
            data.user_email = user.email
        result.append(data)
    return result


@router.get("/funnels/{funnel_id}/enrollments/{enrollment_id}/logs", response_model=List[FunnelEnrollmentLogResponse])
def list_enrollment_logs(
    funnel_id: int,
    enrollment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelService(db)
    svc._get_funnel(funnel_id, _ws(current_user))  # access check
    enrollment = db.query(FunnelEnrollment).filter(
        FunnelEnrollment.id == enrollment_id,
        FunnelEnrollment.funnel_id == funnel_id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment not found")
    return db.query(FunnelEnrollmentLog).filter(
        FunnelEnrollmentLog.enrollment_id == enrollment_id
    ).order_by(FunnelEnrollmentLog.created_at).all()


@router.get("/funnels/{funnel_id}/enrollments/{enrollment_id}/steps/{step_id}/post-events")
def get_step_post_events(
    funnel_id: int,
    enrollment_id: int,
    step_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get messaging events for the enrolled user after a specific step was executed."""
    from app.models.messaging import MessagingEvent

    svc = FunnelService(db)
    funnel = svc._get_funnel(funnel_id, _ws(current_user))

    enrollment = db.query(FunnelEnrollment).filter(
        FunnelEnrollment.id == enrollment_id,
        FunnelEnrollment.funnel_id == funnel_id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment not found")

    # Find the first log entry for this step to get the "since" timestamp
    step_log = db.query(FunnelEnrollmentLog).filter(
        FunnelEnrollmentLog.enrollment_id == enrollment_id,
        FunnelEnrollmentLog.step_id == step_id,
    ).order_by(FunnelEnrollmentLog.created_at.asc()).first()

    if not step_log:
        return {"items": [], "total": 0, "send_log_id": 0, "user_id": enrollment.user_id, "since": None}

    since = step_log.created_at

    # Find next step's first log entry as upper bound
    next_step_log = db.query(FunnelEnrollmentLog).filter(
        FunnelEnrollmentLog.enrollment_id == enrollment_id,
        FunnelEnrollmentLog.step_id != step_id,
        FunnelEnrollmentLog.created_at > since,
    ).order_by(FunnelEnrollmentLog.created_at.asc()).first()

    query = db.query(MessagingEvent).filter(
        MessagingEvent.project_id == funnel.project_id,
        MessagingEvent.user_id == enrollment.user_id,
        MessagingEvent.created_at >= since,
    )
    if next_step_log:
        query = query.filter(MessagingEvent.created_at < next_step_log.created_at)
    if event_name:
        query = query.filter(MessagingEvent.event_name == event_name)

    total = query.count()
    items = query.order_by(MessagingEvent.created_at.asc()).offset(offset).limit(limit).all()

    return {
        "items": [
            {
                "id": e.id,
                "event_name": e.event_name,
                "properties": e.properties,
                "source": e.source,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in items
        ],
        "total": total,
        "send_log_id": 0,
        "user_id": enrollment.user_id,
        "since": since.isoformat() if since else None,
    }


# ============================================================================
# WhatsApp instances for send_whatsapp step
# ============================================================================

@router.get("/projects/{project_id}/whatsapp-instances")
def list_whatsapp_instances(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List available WhatsApp instances for the workspace (used by send_whatsapp step config)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    instances = db.query(WhatsAppInstance).filter(
        WhatsAppInstance.workspace_id == project.workspace_id,
        WhatsAppInstance.is_active == True,
    ).all()
    return [
        {
            "id": i.id,
            "name": i.instance_name,
            "provider_type": i.provider_type,
            "phone": i.phone_number or i.meta_phone_number_id,
            "connection_status": i.connection_status,
        }
        for i in instances
    ]


@router.post("/funnels/enrollments/{enrollment_id}/complete-whatsapp")
def complete_whatsapp_handoff(
    enrollment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a send_whatsapp handoff as completed (exit_0). Called by agents or external triggers."""
    from app.services.funnel_engine import FunnelEngine
    engine = FunnelEngine(db)
    result = engine.complete_whatsapp_handoff(enrollment_id)
    if not result.get("resolved"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.get("reason", "unknown"))
    return result


@router.post("/funnels/{funnel_id}/enrollments/{enrollment_id}/resume")
def resume_enrollment(
    funnel_id: int,
    enrollment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually resume a paused enrollment."""
    enrollment = db.query(FunnelEnrollment).filter(
        FunnelEnrollment.id == enrollment_id,
        FunnelEnrollment.funnel_id == funnel_id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment not found")

    from app.services.funnel_engine import FunnelEngine
    engine = FunnelEngine(db)
    result = engine.resume_enrollment(enrollment_id)
    if not result.get("resumed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("reason", "Cannot resume"),
        )
    return result


@router.post("/funnels/{funnel_id}/enrollments/{enrollment_id}/retry-deferred")
def retry_deferred_step(
    funnel_id: int,
    enrollment_id: int,
    skip: bool = Query(False, description="If true, skip the step instead of retrying"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retry or skip a step stuck on a deferred send (urgent_cooldown, suppression, etc.)."""
    enrollment = db.query(FunnelEnrollment).filter(
        FunnelEnrollment.id == enrollment_id,
        FunnelEnrollment.funnel_id == funnel_id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment not found")

    from app.services.funnel_engine import FunnelEngine
    engine = FunnelEngine(db)
    result = engine.retry_deferred_step(enrollment_id, skip=skip)
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("reason", "Cannot retry"),
        )
    return result


@router.post("/funnels/{funnel_id}/enrollments/{enrollment_id}/force-exit")
def force_exit_enrollment(
    funnel_id: int,
    enrollment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Force-exit an active enrollment (manual admin action)."""
    enrollment = db.query(FunnelEnrollment).filter(
        FunnelEnrollment.id == enrollment_id,
        FunnelEnrollment.funnel_id == funnel_id,
        FunnelEnrollment.status == "active",
    ).first()
    if not enrollment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active enrollment not found")

    from app.services.funnel_engine import FunnelEngine
    engine = FunnelEngine(db)
    engine._exit_enrollment(enrollment, "manual_exit")
    db.commit()
    return {"exited": True, "enrollment_id": enrollment_id}


@router.post(
    "/funnels/{funnel_id}/enrollments/manual",
    response_model=ManualEnrollResponse,
    status_code=status.HTTP_201_CREATED,
)
def manual_enroll(
    funnel_id: int,
    data: ManualEnrollRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually enroll a user at a specific funnel step (admin recovery action)."""
    from app.models.messaging import MessagingUser

    # Validate user belongs to a project in the same workspace
    user_exists = db.query(MessagingUser).filter(MessagingUser.id == data.user_id).first()
    if not user_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Validate source enrollment if provided
    if data.source_enrollment_id:
        source = db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == data.source_enrollment_id,
            FunnelEnrollment.funnel_id == funnel_id,
        ).first()
        if not source:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source enrollment not found")
        if source.status == "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Source enrollment is still active. Force-exit it first.",
            )

    from app.services.funnel_engine import FunnelEngine
    engine = FunnelEngine(db)
    result = engine.manual_enroll(
        funnel_id=funnel_id,
        user_id=data.user_id,
        target_step_ids=data.target_step_ids or None,
        source_enrollment_id=data.source_enrollment_id,
        copy_metadata=data.copy_metadata,
        metadata_overrides=data.metadata_overrides,
        force_exit_active=data.force_exit_active,
    )

    if "error" in result:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result["error"])

    return ManualEnrollResponse(**result)


@router.get(
    "/funnels/{funnel_id}/enrollments/{enrollment_id}/milestones",
    response_model=List[MilestoneResponse],
)
def list_enrollment_milestones(
    funnel_id: int,
    enrollment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get milestones achieved by an enrollment."""
    enrollment = db.query(FunnelEnrollment).filter(
        FunnelEnrollment.id == enrollment_id,
        FunnelEnrollment.funnel_id == funnel_id,
    ).first()
    if not enrollment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment not found")

    from app.services.funnel_engine import FunnelEngine
    engine = FunnelEngine(db)
    return engine.get_enrollment_milestones(enrollment_id)
