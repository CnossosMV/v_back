"""
Personalization Router

Manage per-project personalization config (which traits/scores to expose
via the public visitor-data SDK endpoint).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func

from app.database import get_db
from app.models import (
    User, Project, ProjectPersonalizationConfig,
    ScoreDefinition,
)
from app.models.messaging import MessagingUser
from app.schemas.personalization import (
    PersonalizationConfigCreate,
    PersonalizationConfigUpdate,
    PersonalizationConfigResponse,
    VisitorDataTestRequest,
    VisitorDataResponse,
    AvailableFieldsResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(tags=["Personalization"])


# ============================================================================
# Helpers
# ============================================================================

def _get_project(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == user.workspace_id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# ============================================================================
# CRUD
# ============================================================================

@router.get("/projects/{project_id}/personalization", response_model=PersonalizationConfigResponse)
def get_config(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    config = db.query(ProjectPersonalizationConfig).filter(
        ProjectPersonalizationConfig.project_id == project_id
    ).first()

    if not config:
        # Return a default (unsaved) config
        config = ProjectPersonalizationConfig(
            id=0,
            project_id=project_id,
            enabled=False,
            exposed_traits=[],
            expose_scores=False,
            expose_name=False,
            expose_email=False,
            cache_ttl_seconds=300,
            require_analytics_consent=True,
            auto_track_spa_pages=False,
        )
        from datetime import datetime
        config.created_at = datetime.utcnow()
        config.updated_at = datetime.utcnow()

    return PersonalizationConfigResponse.model_validate(config)


@router.put("/projects/{project_id}/personalization", response_model=PersonalizationConfigResponse)
def upsert_config(
    project_id: int,
    data: PersonalizationConfigCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    config = db.query(ProjectPersonalizationConfig).filter(
        ProjectPersonalizationConfig.project_id == project_id
    ).first()

    if config:
        for key, val in data.model_dump().items():
            setattr(config, key, val)
    else:
        config = ProjectPersonalizationConfig(
            project_id=project_id,
            **data.model_dump()
        )
        db.add(config)

    db.commit()
    db.refresh(config)
    return PersonalizationConfigResponse.model_validate(config)


@router.patch("/projects/{project_id}/personalization", response_model=PersonalizationConfigResponse)
def patch_config(
    project_id: int,
    data: PersonalizationConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    config = db.query(ProjectPersonalizationConfig).filter(
        ProjectPersonalizationConfig.project_id == project_id
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Config not found. Use PUT to create first.")

    for key, val in data.model_dump(exclude_none=True).items():
        setattr(config, key, val)

    db.commit()
    db.refresh(config)
    return PersonalizationConfigResponse.model_validate(config)


# ============================================================================
# Test endpoint
# ============================================================================

@router.post("/projects/{project_id}/personalization/test", response_model=VisitorDataResponse)
def test_visitor_data(
    project_id: int,
    data: VisitorDataTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Test the visitor-data response for a given anonymous_id."""
    _get_project(db, project_id, current_user)

    from app.services.messaging.visitor_data_service import VisitorDataService
    service = VisitorDataService(db)
    result = service.resolve_visitor(project_id, data.anonymous_id)

    if result is None:
        return VisitorDataResponse(anonymous=True, traits={}, scores=[])

    return VisitorDataResponse(**result)


# ============================================================================
# Available fields
# ============================================================================

@router.get("/projects/{project_id}/personalization/available-fields", response_model=AvailableFieldsResponse)
def get_available_fields(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List available trait keys and active score definitions."""
    _get_project(db, project_id, current_user)

    # Collect distinct property keys from users
    users = db.query(MessagingUser.properties).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.properties.isnot(None)
    ).limit(500).all()

    trait_keys = set()
    for (props,) in users:
        if isinstance(props, dict):
            trait_keys.update(props.keys())

    # Remove standard fields that have their own toggles
    trait_keys.discard("email")
    trait_keys.discard("name")
    trait_keys.discard("phone")

    # Get active score definitions
    scores = db.query(ScoreDefinition).filter(
        ScoreDefinition.project_id == project_id,
        ScoreDefinition.status == "active"
    ).all()

    score_defs = [{"slug": s.slug, "name": s.name} for s in scores]

    return AvailableFieldsResponse(
        trait_keys=sorted(trait_keys),
        score_definitions=score_defs,
    )
