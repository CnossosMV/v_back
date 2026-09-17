"""
Locale → channel-instance routing map (content i18n, Phase 3).
CRUD for per-locale sender bindings. Unmapped (locale, channel) falls back to the
caller's default instance at send time (see services/messaging/channel_router.py).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models import Project, User, LocaleChannelMap
from app.routers.auth import get_current_user

router = APIRouter(prefix="/projects/{project_id}/locale-channel-map", tags=["i18n-channel-routing"])


def _project_or_404(db: Session, project_id: int, workspace_id: int) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id, Project.workspace_id == workspace_id
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


class LocaleChannelMapIn(BaseModel):
    locale: str = Field(..., max_length=10)
    channel_type: str = Field(..., max_length=20)
    instance_id: int


class LocaleChannelMapOut(LocaleChannelMapIn):
    id: int
    project_id: int

    class Config:
        from_attributes = True


@router.get("/", response_model=List[LocaleChannelMapOut])
def list_maps(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _project_or_404(db, project_id, current_user.workspace_id)
    return db.query(LocaleChannelMap).filter(LocaleChannelMap.project_id == project_id).all()


@router.put("/", response_model=LocaleChannelMapOut)
def upsert_map(
    project_id: int,
    data: LocaleChannelMapIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bind (locale, channel_type) → instance_id. Upsert on the unique key."""
    _project_or_404(db, project_id, current_user.workspace_id)
    row = db.query(LocaleChannelMap).filter(
        LocaleChannelMap.project_id == project_id,
        LocaleChannelMap.locale == data.locale,
        LocaleChannelMap.channel_type == data.channel_type,
    ).first()
    if row:
        row.instance_id = data.instance_id
    else:
        row = LocaleChannelMap(project_id=project_id, **data.model_dump())
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{map_id}", status_code=204)
def delete_map(
    project_id: int,
    map_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _project_or_404(db, project_id, current_user.workspace_id)
    row = db.query(LocaleChannelMap).filter(
        LocaleChannelMap.id == map_id, LocaleChannelMap.project_id == project_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Mapping not found")
    db.delete(row)
    db.commit()
