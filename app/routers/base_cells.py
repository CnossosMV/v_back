"""
Base-cell authoring (Phase 3 UX) — CRUD for standing Messages assigned to
Position cells (Type / Stage / Age). Each carries a stable slot_id.
"""
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.auth import get_current_user
from app.models import Project, BaseCellMessage
from app.models.project_import import LifecycleModel
from app.services.lifecycle_model_service import LifecycleModelService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/base-messages", tags=["base-cells"])


class BaseCellIn(BaseModel):
    lifecycle_model_id: Optional[int] = None
    type: str = "default"
    stage: Optional[str] = None
    age_bucket: Optional[str] = None
    channel: Optional[str] = None
    content_mode: str = "text"  # text | template
    content_text: Optional[str] = None
    content_subject: Optional[str] = None
    template_name: Optional[str] = None
    template_language: Optional[str] = None
    template_components: Optional[list] = None
    instance_id: Optional[int] = None
    is_active: bool = True


class BaseCellOut(BaseCellIn):
    id: int
    slot_id: str

    class Config:
        from_attributes = True


def _project(db: Session, project_id: int):
    if not db.query(Project).filter(Project.id == project_id).first():
        raise HTTPException(status_code=404, detail="Project not found")


@router.get("", response_model=list[BaseCellOut])
def list_base_messages(
    project_id: int,
    lifecycle_model_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    _project(db, project_id)
    if lifecycle_model_id is None:
        model = LifecycleModelService(db).ensure_legacy_model(project_id)
        lifecycle_model_id = model.id
    return db.query(BaseCellMessage).filter(
        BaseCellMessage.project_id == project_id,
        BaseCellMessage.lifecycle_model_id == lifecycle_model_id,
    ).order_by(BaseCellMessage.type, BaseCellMessage.id).all()


@router.post("", response_model=BaseCellOut)
def create_base_message(
    project_id: int,
    data: BaseCellIn,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    _project(db, project_id)
    model_id = data.lifecycle_model_id
    if model_id is None:
        model_id = LifecycleModelService(db).ensure_legacy_model(project_id).id
    elif not db.query(LifecycleModel.id).filter(
        LifecycleModel.id == model_id, LifecycleModel.project_id == project_id,
    ).first():
        raise HTTPException(status_code=400, detail="Lifecycle model does not belong to this project")
    values = data.model_dump(exclude={"lifecycle_model_id"})
    row = BaseCellMessage(
        project_id=project_id, lifecycle_model_id=model_id, slot_id=str(uuid.uuid4()), **values,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{message_id}", response_model=BaseCellOut)
def update_base_message(
    project_id: int,
    message_id: int,
    data: BaseCellIn,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    _project(db, project_id)
    row = db.query(BaseCellMessage).filter(
        BaseCellMessage.id == message_id, BaseCellMessage.project_id == project_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    # slot_id is stable — never reassigned on edit (the learning identity).
    values = data.model_dump(exclude={"lifecycle_model_id"})
    if data.lifecycle_model_id is not None and data.lifecycle_model_id != row.lifecycle_model_id:
        raise HTTPException(status_code=409, detail="A Base message cannot move between lifecycle model versions")
    for k, v in values.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{message_id}")
def delete_base_message(
    project_id: int,
    message_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    _project(db, project_id)
    row = db.query(BaseCellMessage).filter(
        BaseCellMessage.id == message_id, BaseCellMessage.project_id == project_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(row)
    db.commit()
    return {"deleted": True}
