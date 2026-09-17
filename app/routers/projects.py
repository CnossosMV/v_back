from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Optional
from app.database import get_db
from app.dependencies import require_project_role
from app.models import Project, User
from app.routers.auth import get_current_user
from pydantic import BaseModel, Field
from datetime import datetime

router = APIRouter(prefix="/projects", tags=["projects"])

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

class ProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    workspace_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

@router.get("/", response_model=List[ProjectResponse])
def get_projects(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not current_user.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace"
        )
    
    projects = db.query(Project).filter(
        Project.workspace_id == current_user.workspace_id,
        Project.is_active == True
    ).all()
    
    return projects

@router.post("/", response_model=ProjectResponse)
def create_project(
    project: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not current_user.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace"
        )
    
    # Check if project name already exists in workspace
    existing_project = db.query(Project).filter(
        Project.workspace_id == current_user.workspace_id,
        Project.name == project.name,
        Project.is_active == True
    ).first()
    
    if existing_project:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project name already exists in workspace"
        )
    
    db_project = Project(
        name=project.name,
        description=project.description,
        workspace_id=current_user.workspace_id
    )
    
    db.add(db_project)
    db.flush()
    # Every project starts with a compatibility model, so Position/Base rows
    # are versioned from day one even before a customer authors its taxonomy.
    from app.services.lifecycle_model_service import LifecycleModelService
    LifecycleModelService(db).ensure_legacy_model(db_project.id, current_user.id)
    db.commit()
    db.refresh(db_project)
    
    return db_project

@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    return project

@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int,
    project_update: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Check name uniqueness if name is being updated
    if project_update.name and project_update.name != project.name:
        existing_project = db.query(Project).filter(
            Project.workspace_id == current_user.workspace_id,
            Project.name == project_update.name,
            Project.is_active == True,
            Project.id != project_id
        ).first()
        
        if existing_project:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project name already exists in workspace"
            )
    
    update_data = project_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(project, field, value)
    
    db.commit()
    db.refresh(project)
    
    return project

@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Soft delete
    project.is_active = False
    db.commit()

    return {"message": "Project deleted successfully"}


# ============================================================================
# i18n settings — default locale, supported locales, default timezone
# ============================================================================

class ProjectI18nSettings(BaseModel):
    default_locale: str
    supported_locales: Optional[List[str]] = None  # null ⇒ [default_locale]
    default_timezone: str


def _owned_project(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == user.workspace_id,
    ).first()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.get("/{project_id}/i18n-settings", response_model=ProjectI18nSettings)
def get_i18n_settings(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    p = _owned_project(db, project_id, current_user)
    return ProjectI18nSettings(
        default_locale=p.default_locale,
        supported_locales=p.supported_locales or [p.default_locale],
        default_timezone=p.default_timezone,
    )


@router.put("/{project_id}/i18n-settings", response_model=ProjectI18nSettings)
def update_i18n_settings(
    project_id: int,
    data: ProjectI18nSettings,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set the project's default locale, supported locales, and default timezone.
    The default locale is always kept inside supported_locales."""
    p = _owned_project(db, project_id, current_user)
    supported = data.supported_locales or [data.default_locale]
    if data.default_locale not in supported:
        supported = [data.default_locale, *supported]
    # de-dupe, preserve order
    seen, deduped = set(), []
    for loc in supported:
        if loc not in seen:
            seen.add(loc)
            deduped.append(loc)
    p.default_locale = data.default_locale
    p.supported_locales = deduped
    p.default_timezone = data.default_timezone
    db.commit()
    db.refresh(p)
    return ProjectI18nSettings(
        default_locale=p.default_locale,
        supported_locales=p.supported_locales,
        default_timezone=p.default_timezone,
    )


# ============================================================================
# First-class project markets
# ============================================================================

class ProjectMarketCreate(BaseModel):
    key: str
    country_code: str
    region_codes: List[str] = Field(default_factory=list)
    timezone: str
    locales: List[str]
    calendar_tags: List[str] = Field(default_factory=list)
    make_default: bool = False


class ProjectMarketUpdate(BaseModel):
    country_code: Optional[str] = None
    region_codes: Optional[List[str]] = None
    timezone: Optional[str] = None
    locales: Optional[List[str]] = None
    calendar_tags: Optional[List[str]] = None


class ProjectMarketStatusUpdate(BaseModel):
    status: str


def _dump(model: BaseModel, *, exclude_unset: bool = False) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)


@router.get("/{project_id}/markets")
def list_project_markets(
    project_id: int,
    include_archived: bool = False,
    after_key: Optional[str] = None,
    limit: int = 50,
    _auth=Depends(require_project_role("viewer")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _owned_project(db, project_id, current_user)
    from app.services.project_market_service import ProjectMarketService
    return ProjectMarketService(db).list_page(
        project_id,
        include_archived=include_archived,
        after_key=after_key,
        limit=limit,
    )


@router.post("/{project_id}/markets", status_code=status.HTTP_201_CREATED)
def create_project_market(
    project_id: int,
    data: ProjectMarketCreate,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _owned_project(db, project_id, current_user)
    from app.services.project_market_service import ProjectMarketError, ProjectMarketService, market_payload
    values = _dump(data)
    make_default = bool(values.pop("make_default", False))
    try:
        row = ProjectMarketService(db).create(project_id, values, make_default)
        db.commit()
        db.refresh(row)
        return market_payload(row)
    except ProjectMarketError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.patch("/{project_id}/markets/{market_key}")
def update_project_market(
    project_id: int,
    market_key: str,
    data: ProjectMarketUpdate,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _owned_project(db, project_id, current_user)
    from app.services.project_market_service import ProjectMarketError, ProjectMarketService, market_payload
    try:
        row = ProjectMarketService(db).update(
            project_id, market_key, _dump(data, exclude_unset=True)
        )
        db.commit()
        db.refresh(row)
        return market_payload(row)
    except ProjectMarketError as exc:
        db.rollback()
        code = status.HTTP_404_NOT_FOUND if str(exc) == "Market not found" else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.post("/{project_id}/markets/{market_key}/default")
def set_default_project_market(
    project_id: int,
    market_key: str,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _owned_project(db, project_id, current_user)
    from app.services.project_market_service import ProjectMarketError, ProjectMarketService, market_payload
    try:
        row = ProjectMarketService(db).set_default(project_id, market_key)
        db.commit()
        db.refresh(row)
        return market_payload(row)
    except ProjectMarketError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.put("/{project_id}/markets/{market_key}/status")
def set_project_market_status(
    project_id: int,
    market_key: str,
    data: ProjectMarketStatusUpdate,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _owned_project(db, project_id, current_user)
    from app.services.project_market_service import ProjectMarketError, ProjectMarketService, market_payload
    try:
        row = ProjectMarketService(db).set_status(project_id, market_key, data.status)
        db.commit()
        db.refresh(row)
        return market_payload(row)
    except ProjectMarketError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
