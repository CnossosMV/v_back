"""REST API router for Funnel Sandbox testing."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from app.schemas.sandbox import (
    SandboxContactCreate, SandboxContactResponse,
    SandboxSessionCreate, SandboxSessionUpdate, SandboxSessionResponse,
    SandboxEnrollRequest, SandboxFireEventRequest,
    SandboxMockWebhookRequest, SandboxAdvanceRequest, SandboxResetRequest,
    SandboxPropertyUpdateRequest, SandboxActionLogResponse,
    FunnelManifestResponse,
)
from app.schemas.funnels import FunnelEnrollmentResponse, FunnelEnrollmentLogResponse
from app.services.funnel_sandbox_service import FunnelSandboxService
from app.services.funnel_introspector import FunnelIntrospector
from app.models import Funnel, Project

router = APIRouter(tags=["funnel-sandbox"])


def _ws(user: User):
    if not user.workspace_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User must be associated with a workspace")
    return user.workspace_id


def _verify_funnel(db: Session, funnel_id: int, workspace_id: int) -> Funnel:
    funnel = (
        db.query(Funnel)
        .join(Project)
        .filter(
            Funnel.id == funnel_id,
            Project.workspace_id == workspace_id,
            Project.is_active == True,
        )
        .first()
    )
    if not funnel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Funnel not found")
    return funnel


# ============================================================================
# Introspection
# ============================================================================

@router.post(
    "/funnels/{funnel_id}/sandbox/introspect",
    response_model=FunnelManifestResponse,
)
def introspect_funnel(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    funnel = _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelIntrospector(db)
    manifest = svc.introspect(funnel_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Funnel not found")
    return manifest


# ============================================================================
# Sandbox Contacts
# ============================================================================

@router.post(
    "/funnels/{funnel_id}/sandbox/contacts",
    response_model=SandboxContactResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_sandbox_contact(
    funnel_id: int,
    data: SandboxContactCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    funnel = _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    user = svc.create_sandbox_contact(
        project_id=funnel.project_id,
        name=data.name,
        email=data.email,
        phone=data.phone,
        properties=data.properties,
    )
    db.commit()
    return user


@router.delete(
    "/funnels/{funnel_id}/sandbox/contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_sandbox_contact(
    funnel_id: int,
    contact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    funnel = _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    deleted = svc.delete_sandbox_contact(contact_id, funnel.project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Sandbox contact not found")
    db.commit()


@router.patch(
    "/funnels/{funnel_id}/sandbox/contacts/{contact_id}/properties",
    response_model=SandboxContactResponse,
)
def update_contact_properties(
    funnel_id: int,
    contact_id: int,
    data: SandboxPropertyUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    funnel = _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    user = svc.update_contact_properties(contact_id, funnel.project_id, data.properties)
    if not user:
        raise HTTPException(status_code=404, detail="Sandbox contact not found")
    db.commit()
    return user


# ============================================================================
# Sessions
# ============================================================================

@router.post(
    "/funnels/{funnel_id}/sandbox/sessions",
    response_model=SandboxSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_session(
    funnel_id: int,
    contact_id: int,
    data: SandboxSessionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    funnel = _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    session = svc.create_session(
        project_id=funnel.project_id,
        funnel_id=funnel_id,
        contact_id=contact_id,
        created_by=current_user.id,
        mode=data.mode,
        preview_channel=data.preview_channel,
        preview_destination=data.preview_destination,
        preview_instance_id=data.preview_instance_id,
    )
    db.commit()
    return session


@router.put(
    "/funnels/{funnel_id}/sandbox/sessions/{session_id}",
    response_model=SandboxSessionResponse,
)
def update_session(
    funnel_id: int,
    session_id: int,
    data: SandboxSessionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    session = svc.update_session(
        session_id=session_id,
        mode=data.mode,
        preview_channel=data.preview_channel,
        preview_destination=data.preview_destination,
        preview_instance_id=data.preview_instance_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.commit()
    return session


# ============================================================================
# Enrollment Operations
# ============================================================================

@router.post(
    "/funnels/{funnel_id}/sandbox/sessions/{session_id}/enroll",
    response_model=FunnelEnrollmentResponse,
)
def enroll(
    funnel_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    enrollment = svc.force_enroll(session_id)
    if not enrollment:
        raise HTTPException(status_code=400, detail="Could not enroll — check session status")
    db.commit()
    return enrollment


@router.post("/funnels/{funnel_id}/sandbox/sessions/{session_id}/fire-event")
def fire_event(
    funnel_id: int,
    session_id: int,
    data: SandboxFireEventRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    result = svc.inject_event(session_id, data.event_name, data.properties)
    if result is None:
        raise HTTPException(status_code=400, detail="Session not active")
    return result


@router.post("/funnels/{funnel_id}/sandbox/sessions/{session_id}/mock-webhook")
def mock_webhook(
    funnel_id: int,
    session_id: int,
    data: SandboxMockWebhookRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    ok = svc.mock_webhook_response(session_id, data.step_id, data.response_data)
    if not ok:
        raise HTTPException(status_code=400, detail="Could not mock webhook — check session/step")
    db.commit()
    return {"ok": True}


@router.post("/funnels/{funnel_id}/sandbox/sessions/{session_id}/advance")
def advance(
    funnel_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    result = svc.advance_past_wait(session_id)
    if result is None:
        raise HTTPException(status_code=400, detail="No active enrollment")
    return result


@router.post("/funnels/{funnel_id}/sandbox/sessions/{session_id}/reset")
def reset(
    funnel_id: int,
    session_id: int,
    data: SandboxResetRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    result = svc.reset_enrollment(session_id, re_enroll=data.re_enroll)
    if result is None:
        raise HTTPException(status_code=400, detail="Session not found")
    db.commit()
    return result


# ============================================================================
# Action Log & Enrollment Logs
# ============================================================================

@router.get(
    "/funnels/{funnel_id}/sandbox/sessions/{session_id}/action-log",
    response_model=List[SandboxActionLogResponse],
)
def get_action_log(
    funnel_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    return svc.get_action_log(session_id)


@router.get(
    "/funnels/{funnel_id}/sandbox/sessions/{session_id}/enrollment-logs",
    response_model=List[FunnelEnrollmentLogResponse],
)
def get_enrollment_logs(
    funnel_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _verify_funnel(db, funnel_id, _ws(current_user))
    svc = FunnelSandboxService(db)
    return svc.get_enrollment_logs(session_id)
