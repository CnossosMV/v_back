"""Project delivery-profile and sender-identity administration API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.models import Project, User
from app.models.campaigns import ChannelDeliveryProfile, ChannelSenderIdentity
from app.routers.auth import get_current_user
from app.schemas.channel_delivery_profiles import (
    AvailableSenderResponse,
    DeliveryProfileCreate,
    DeliveryProfileResponse,
    DeliveryProfileUpdate,
    SenderIdentityCreate,
    SenderIdentityUpdate,
)
from app.services.campaigns.delivery_profiles import (
    DeliveryProfileError,
    DeliveryProfileService,
)

router = APIRouter(
    prefix="/projects/{project_id}/channel-delivery-profiles",
    tags=["channel-delivery-profiles"],
)


def _ensure_project(db: Session, project_id: int, current_user: User) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id,
        Project.is_active == True,  # noqa: E712
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _profile_or_404(db: Session, project_id: int, profile_id: int) -> ChannelDeliveryProfile:
    row = db.query(ChannelDeliveryProfile).filter(
        ChannelDeliveryProfile.id == profile_id,
        ChannelDeliveryProfile.project_id == project_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Delivery profile not found")
    return row


def _identity_or_404(db: Session, project_id: int, identity_id: int) -> ChannelSenderIdentity:
    row = db.query(ChannelSenderIdentity).filter(
        ChannelSenderIdentity.id == identity_id,
        ChannelSenderIdentity.project_id == project_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Sender identity not found")
    return row


def _identity_dict(row: ChannelSenderIdentity) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "channel": row.channel,
        "provider": row.provider,
        "identity_key": row.identity_key,
        "address": row.address,
        "display_name": row.display_name,
        "reply_to": row.reply_to,
        "external_instance_id": row.external_instance_id,
        "status": row.status,
        "is_default": row.is_default,
        "capabilities": row.capabilities,
        "metadata": row.metadata_,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("", response_model=list[DeliveryProfileResponse])
def list_profiles(
    project_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return db.query(ChannelDeliveryProfile).filter(
        ChannelDeliveryProfile.project_id == project_id,
    ).order_by(ChannelDeliveryProfile.priority.desc(), ChannelDeliveryProfile.id).all()


@router.get("/available-senders", response_model=list[AvailableSenderResponse])
def list_available_senders(
    project_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return DeliveryProfileService(db).available_senders(project_id)


@router.post("", response_model=DeliveryProfileResponse, status_code=status.HTTP_201_CREATED)
def create_profile(
    project_id: int,
    payload: DeliveryProfileCreate,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return DeliveryProfileService(db).create_profile(project_id, payload.model_dump())
    except DeliveryProfileError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Delivery profile already exists") from exc


@router.get("/sender-identities")
def list_sender_identities(
    project_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return [_identity_dict(row) for row in db.query(ChannelSenderIdentity).filter(
        ChannelSenderIdentity.project_id == project_id,
    ).order_by(ChannelSenderIdentity.channel, ChannelSenderIdentity.id).all()]


@router.post("/sender-identities", status_code=status.HTTP_201_CREATED)
def create_sender_identity(
    project_id: int,
    payload: SenderIdentityCreate,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return _identity_dict(DeliveryProfileService(db).create_identity(
            project_id,
            payload.model_dump(),
        ))
    except (DeliveryProfileError, IntegrityError) as exc:
        db.rollback()
        code = 409 if isinstance(exc, IntegrityError) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.put("/sender-identities/{identity_id}")
def update_sender_identity(
    project_id: int,
    identity_id: int,
    payload: SenderIdentityUpdate,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return _identity_dict(DeliveryProfileService(db).update_identity(
            _identity_or_404(db, project_id, identity_id),
            payload.model_dump(exclude_unset=True),
        ))
    except DeliveryProfileError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{profile_id}", response_model=DeliveryProfileResponse)
def get_profile(
    project_id: int,
    profile_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return _profile_or_404(db, project_id, profile_id)


@router.put("/{profile_id}", response_model=DeliveryProfileResponse)
def update_profile(
    project_id: int,
    profile_id: int,
    payload: DeliveryProfileUpdate,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return DeliveryProfileService(db).update_profile(
            _profile_or_404(db, project_id, profile_id),
            payload.model_dump(exclude_unset=True),
        )
    except DeliveryProfileError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{profile_id}", response_model=DeliveryProfileResponse)
def disable_profile(
    project_id: int,
    profile_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return DeliveryProfileService(db).disable_profile(
            _profile_or_404(db, project_id, profile_id),
        )
    except DeliveryProfileError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{profile_id}/health")
def check_profile_health(
    project_id: int,
    profile_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return DeliveryProfileService(db).check_health(
        _profile_or_404(db, project_id, profile_id),
    )
