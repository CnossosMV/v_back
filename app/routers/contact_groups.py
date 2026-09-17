"""Project-scoped reusable contact group API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.models import Project, User
from app.models.campaigns import ContactGroupMembership
from app.models.messaging import MessagingUser
from app.routers.auth import get_current_user
from app.schemas.contact_groups import (
    ContactGroupCreate,
    ContactGroupMembersRequest,
    ContactGroupPreviewRequest,
    ContactGroupResponse,
    ContactGroupUpdate,
)
from app.services.contact_groups.compiler import GroupFilterError
from app.services.contact_groups.service import ContactGroupService

router = APIRouter(prefix="/projects/{project_id}/contact-groups", tags=["contact-groups"])


def _ensure_project(db: Session, project_id: int, current_user: User) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id,
        Project.is_active == True,  # noqa: E712
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _group_or_404(db: Session, project_id: int, group_id: int):
    group = ContactGroupService(db).get(project_id, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Contact group not found")
    return group


@router.get("", response_model=list[ContactGroupResponse])
def list_groups(
    project_id: int,
    include_archived: bool = False,
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return ContactGroupService(db).list(project_id, include_archived=include_archived)


@router.post("", response_model=ContactGroupResponse, status_code=status.HTTP_201_CREATED)
def create_group(
    project_id: int,
    payload: ContactGroupCreate,
    auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return ContactGroupService(db).create(
            project_id,
            payload.model_dump(),
            auth.get("user_id"),
        )
    except GroupFilterError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A contact group with this name already exists") from exc


@router.post("/preview")
def preview_group_rules(
    project_id: int,
    payload: ContactGroupPreviewRequest,
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return ContactGroupService(db).preview(
            project_id,
            rule_config=payload.rule_config,
            limit=payload.limit,
        )
    except GroupFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{group_id}", response_model=ContactGroupResponse)
def get_group(
    project_id: int,
    group_id: int,
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return _group_or_404(db, project_id, group_id)


@router.put("/{group_id}", response_model=ContactGroupResponse)
def update_group(
    project_id: int,
    group_id: int,
    payload: ContactGroupUpdate,
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    group = _group_or_404(db, project_id, group_id)
    try:
        return ContactGroupService(db).update(group, payload.model_dump(exclude_unset=True))
    except GroupFilterError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A contact group with this name already exists") from exc


@router.delete("/{group_id}", response_model=ContactGroupResponse)
def archive_group(
    project_id: int,
    group_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    return ContactGroupService(db).archive(_group_or_404(db, project_id, group_id))


@router.post("/{group_id}/preview")
def preview_saved_group(
    project_id: int,
    group_id: int,
    limit: int = Query(20, ge=0, le=100),
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    group = _group_or_404(db, project_id, group_id)
    return ContactGroupService(db).preview(project_id, group=group, limit=limit)


@router.post("/{group_id}/evaluate")
def evaluate_group(
    project_id: int,
    group_id: int,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        return ContactGroupService(db).evaluate(_group_or_404(db, project_id, group_id))
    except GroupFilterError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{group_id}/members")
def list_group_members(
    project_id: int,
    group_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=250),
    state_filter: str = Query("included", alias="state"),
    _auth=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    group = _group_or_404(db, project_id, group_id)
    query = db.query(ContactGroupMembership, MessagingUser).join(
        MessagingUser,
        MessagingUser.id == ContactGroupMembership.user_id,
    ).filter(
        ContactGroupMembership.project_id == project_id,
        ContactGroupMembership.group_id == group.id,
        MessagingUser.project_id == project_id,
    )
    if state_filter:
        query = query.filter(ContactGroupMembership.state == state_filter)
    total = query.count()
    rows = query.order_by(ContactGroupMembership.updated_at.desc()).offset(
        (page - 1) * page_size,
    ).limit(page_size).all()
    return {
        "items": [{
            "user_id": user.id,
            "name": user.name,
            "email": user.email,
            "locale": user.locale,
            "state": membership.state,
            "source": membership.source,
            "reason": membership.reason,
            "evaluated_at": membership.evaluated_at,
        } for membership, user in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/{group_id}/members")
def add_group_members(
    project_id: int,
    group_id: int,
    payload: ContactGroupMembersRequest,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    try:
        count = ContactGroupService(db).add_members(
            _group_or_404(db, project_id, group_id),
            payload.user_ids,
            source=payload.source,
        )
        return {"included": count}
    except GroupFilterError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{group_id}/members")
def remove_group_members(
    project_id: int,
    group_id: int,
    payload: ContactGroupMembersRequest,
    _auth=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_project(db, project_id, current_user)
    count = ContactGroupService(db).remove_members(
        _group_or_404(db, project_id, group_id),
        payload.user_ids,
    )
    return {"excluded": count}
