"""Project Variables Router — CRUD + preview for project-level template variables."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any

from app.database import get_db
from app.models import User, Project
from app.schemas.project_variables import (
    ProjectVariableCreate,
    ProjectVariableUpdate,
    ProjectVariableResponse,
    ProjectVariablePreview,
)
from app.services.project_variable_service import ProjectVariableService
from app.routers.auth import get_current_user

router = APIRouter(tags=["Project Variables"])


def _get_project(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == user.workspace_id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get(
    "/projects/{project_id}/variables",
    response_model=list[ProjectVariableResponse],
)
def list_variables(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    svc = ProjectVariableService(db)
    return svc.list(project_id)


@router.post(
    "/projects/{project_id}/variables",
    response_model=ProjectVariableResponse,
    status_code=201,
)
def create_variable(
    project_id: int,
    body: ProjectVariableCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    svc = ProjectVariableService(db)
    return svc.create(project_id, body.model_dump())


@router.get(
    "/projects/{project_id}/variables/{variable_id}",
    response_model=ProjectVariableResponse,
)
def get_variable(
    project_id: int,
    variable_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    svc = ProjectVariableService(db)
    variable = svc.get(project_id, variable_id)
    if not variable:
        raise HTTPException(status_code=404, detail="Variable not found")
    return variable


@router.put(
    "/projects/{project_id}/variables/{variable_id}",
    response_model=ProjectVariableResponse,
)
def update_variable(
    project_id: int,
    variable_id: int,
    body: ProjectVariableUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    svc = ProjectVariableService(db)
    variable = svc.update(
        project_id, variable_id, body.model_dump(exclude_unset=True)
    )
    if not variable:
        raise HTTPException(status_code=404, detail="Variable not found")
    return variable


@router.delete(
    "/projects/{project_id}/variables/{variable_id}",
    status_code=204,
)
def delete_variable(
    project_id: int,
    variable_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    svc = ProjectVariableService(db)
    if not svc.delete(project_id, variable_id):
        raise HTTPException(status_code=404, detail="Variable not found")


@router.post(
    "/projects/{project_id}/variables/{variable_id}/preview",
    response_model=ProjectVariablePreview,
)
def preview_variable(
    project_id: int,
    variable_id: int,
    sample_contact: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    svc = ProjectVariableService(db)
    variable = svc.get(project_id, variable_id)
    if not variable:
        raise HTTPException(status_code=404, detail="Variable not found")
    rendered = svc.preview_variable(variable, sample_contact)
    return ProjectVariablePreview(key=variable.key, rendered_value=rendered)
