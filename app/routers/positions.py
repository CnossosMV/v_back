"""
Contact Position read API (Phase 3) — exposes the computed (Type, Stage, Age)
base machine: a contact's current position + history, and a grid aggregate.
Read-only; the position is maintained by PositionSweepWorker.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.auth import get_current_user
from app.models import Project, ContactPosition, ContactPositionTransition
from app.models.project_import import LifecycleModel
from app.schemas.positions import (
    ContactPositionResponse,
    ContactPositionDetailResponse,
    PositionTransitionResponse,
    PositionCellCount,
    PositionGridResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/positions",
    tags=["positions"],
)


def _get_project(db: Session, project_id: int) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _resolve_model(db: Session, project_id: int, selector: str) -> LifecycleModel:
    query = db.query(LifecycleModel).filter(LifecycleModel.project_id == project_id)
    if selector in {"active", "shadow"}:
        model = query.filter(LifecycleModel.status == selector).first()
    else:
        try:
            model_id = int(selector)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="model must be active, shadow, or a lifecycle model id") from exc
        model = query.filter(LifecycleModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"Lifecycle model not found for selector: {selector}")
    return model


@router.get("/grid", response_model=PositionGridResponse)
def get_position_grid(
    project_id: int,
    model: str = Query("active"),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Aggregate contact counts per (Type, Stage, Age) cell — the base-machine
    grid view."""
    _get_project(db, project_id)
    lifecycle_model = _resolve_model(db, project_id, model)
    rows = db.query(
        ContactPosition.type,
        ContactPosition.stage,
        ContactPosition.age_bucket,
        func.count().label("count"),
    ).filter(
        ContactPosition.project_id == project_id,
        ContactPosition.lifecycle_model_id == lifecycle_model.id,
    ).group_by(
        ContactPosition.type, ContactPosition.stage, ContactPosition.age_bucket,
    ).order_by(func.count().desc()).all()

    cells = [
        PositionCellCount(type=r.type, stage=r.stage, age_bucket=r.age_bucket, count=r.count)
        for r in rows
    ]
    return PositionGridResponse(
        cells=cells,
        total=sum(c.count for c in cells),
        lifecycle_model_id=lifecycle_model.id,
        lifecycle_model_status=lifecycle_model.status,
    )


@router.get("/{user_id}", response_model=ContactPositionDetailResponse)
def get_contact_position(
    project_id: int,
    user_id: int,
    model: str = Query("active"),
    history_limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """A contact's current position + transition history (queryable + historical
    — the Phase 3 exit criterion)."""
    _get_project(db, project_id)
    lifecycle_model = _resolve_model(db, project_id, model)

    pos = db.query(ContactPosition).filter(
        ContactPosition.project_id == project_id,
        ContactPosition.user_id == user_id,
        ContactPosition.lifecycle_model_id == lifecycle_model.id,
    ).first()

    transitions = db.query(ContactPositionTransition).filter(
        ContactPositionTransition.project_id == project_id,
        ContactPositionTransition.user_id == user_id,
        ContactPositionTransition.lifecycle_model_id == lifecycle_model.id,
    ).order_by(ContactPositionTransition.occurred_at.desc()).limit(history_limit).all()

    return ContactPositionDetailResponse(
        user_id=user_id,
        lifecycle_model_id=lifecycle_model.id,
        lifecycle_model_status=lifecycle_model.status,
        position=ContactPositionResponse.model_validate(pos) if pos else None,
        transitions=[PositionTransitionResponse.model_validate(t) for t in transitions],
    )
