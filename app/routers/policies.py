"""
Policies Router

Manage project-level automation policies (contact caps, cooldowns, quiet hours).
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models import User, Project
from app.schemas.policies import (
    ProjectPolicyCreate, ProjectPolicyResponse,
    ContactLedgerEntry, ContactStatsResponse, ChannelStats,
    PolicyDecision,
)
from app.services.scoring.policy_service import PolicyService
from app.routers.auth import get_current_user

router = APIRouter(tags=["Policies"])


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
# Policy CRUD
# ============================================================================

@router.get("/projects/{project_id}/policies")
def get_policy(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    service = PolicyService(db)
    policy = service.get_project_policy(project_id)
    if not policy:
        return None
    return ProjectPolicyResponse.model_validate(policy)


@router.put("/projects/{project_id}/policies")
def upsert_policy(
    project_id: int,
    data: ProjectPolicyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    service = PolicyService(db)
    policy = service.upsert_project_policy(project_id, data.model_dump(exclude_none=True))
    return ProjectPolicyResponse.model_validate(policy)


# ============================================================================
# Ledger
# ============================================================================

@router.get("/projects/{project_id}/policies/ledger")
def get_ledger(
    project_id: int,
    user_id: Optional[int] = None,
    channel: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    service = PolicyService(db)
    entries = service.get_ledger(project_id, user_id, channel, limit, offset)
    return [ContactLedgerEntry.model_validate(e) for e in entries]


# ============================================================================
# Stats
# ============================================================================

@router.get("/projects/{project_id}/policies/stats")
def get_stats(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    service = PolicyService(db)
    stats = service.get_contact_stats(project_id)

    return ContactStatsResponse(
        channels=[ChannelStats(**ch) for ch in stats["channels"]],
        total_today=stats["total_today"],
        total_this_week=stats["total_this_week"],
        total_this_month=stats["total_this_month"],
    )


# ============================================================================
# Dry-run check
# ============================================================================

@router.post("/projects/{project_id}/policies/check")
def dry_run_check(
    project_id: int,
    user_id: int = Query(...),
    channel: str = Query(...),
    source: str = Query(default="event_action"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    service = PolicyService(db)
    decision = service.check_can_contact(project_id, user_id, channel, source)
    return decision


# ============================================================================
# i18n — per-locale Guardian overrides (sparse; unset fields inherit the base)
# ============================================================================

from pydantic import BaseModel
from typing import List, Dict, Any
from app.models import LocalePolicyOverride


class LocalePolicyOverrideIn(BaseModel):
    locale: str
    quiet_hours: Optional[Dict[str, Any]] = None
    timezone: Optional[str] = None
    contact_caps: Optional[Dict[str, Any]] = None
    channel_cooldowns: Optional[Dict[str, Any]] = None


class LocalePolicyOverrideOut(LocalePolicyOverrideIn):
    id: int
    project_id: int

    class Config:
        from_attributes = True


@router.get("/projects/{project_id}/policies/locale-overrides", response_model=List[LocalePolicyOverrideOut])
def list_locale_overrides(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    return db.query(LocalePolicyOverride).filter(
        LocalePolicyOverride.project_id == project_id
    ).all()


@router.put("/projects/{project_id}/policies/locale-overrides", response_model=LocalePolicyOverrideOut)
def upsert_locale_override(
    project_id: int,
    data: LocalePolicyOverrideIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upsert the override for one locale. Only set the fields that differ from base."""
    _get_project(db, project_id, current_user)
    row = db.query(LocalePolicyOverride).filter(
        LocalePolicyOverride.project_id == project_id,
        LocalePolicyOverride.locale == data.locale,
    ).first()
    if row:
        row.quiet_hours = data.quiet_hours
        row.timezone = data.timezone
        row.contact_caps = data.contact_caps
        row.channel_cooldowns = data.channel_cooldowns
    else:
        row = LocalePolicyOverride(project_id=project_id, **data.model_dump())
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/projects/{project_id}/policies/locale-overrides/{locale}", status_code=204)
def delete_locale_override(
    project_id: int,
    locale: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    row = db.query(LocalePolicyOverride).filter(
        LocalePolicyOverride.project_id == project_id,
        LocalePolicyOverride.locale == locale,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Override not found")
    db.delete(row)
    db.commit()
