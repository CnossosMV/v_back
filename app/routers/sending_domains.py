"""
Sending Domains — admin endpoints for the outbound pace governor.

List and tune per-domain send pace, warm-up, and reputation status. Domains are
visible to a project when they are owned by it (project_id match) or shared
(project_id IS NULL). All actions require the project 'admin' role.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.models import SendingDomain, SendingRateState
from app.services.channels.send_pace_service import SendPaceService, pace_mode

router = APIRouter(
    prefix="/projects/{project_id}/sending-domains",
    tags=["sending-domains"],
)


class SendingDomainUpdate(BaseModel):
    max_per_minute: Optional[int] = None
    max_per_day: Optional[int] = None
    throttle_factor: Optional[float] = None
    warmup_enabled: Optional[bool] = None
    status: Optional[str] = None  # active | throttled | paused (manual override)
    auto_throttle_bounce_pct: Optional[float] = None
    auto_pause_bounce_pct: Optional[float] = None
    auto_pause_complaint_pct: Optional[float] = None


def _serialize(db: Session, row: SendingDomain, project_id: int) -> dict:
    policy = SendPaceService(db).resolve_policy(row.domain, row.project_id or project_id)
    state = db.query(SendingRateState).filter(SendingRateState.scope_key == row.domain).first()
    today = datetime.utcnow().date()
    sent_today = state.day_count if (state and state.day == today) else 0
    return {
        "id": row.id,
        "domain": row.domain,
        "project_id": row.project_id,
        "shared": row.project_id is None,
        "status": row.status,
        "policy": {
            "effective_per_minute": policy.max_per_minute,
            "effective_per_day": policy.max_per_day,
            "warmup_active": policy.warmup_active,
            "warmup_day": policy.warmup_day,
        },
        "config": {
            "max_per_minute": row.max_per_minute,
            "max_per_day": row.max_per_day,
            "throttle_factor": row.throttle_factor,
            "warmup_enabled": row.warmup_enabled,
            "warmup_started_at": row.warmup_started_at.isoformat() if row.warmup_started_at else None,
            "auto_throttle_bounce_pct": row.auto_throttle_bounce_pct,
            "auto_pause_bounce_pct": row.auto_pause_bounce_pct,
            "auto_pause_complaint_pct": row.auto_pause_complaint_pct,
        },
        "reputation": {
            "bounce_rate_24h": row.bounce_rate_24h,
            "complaint_rate_24h": row.complaint_rate_24h,
            "updated_at": row.reputation_updated_at.isoformat() if row.reputation_updated_at else None,
        },
        "today": {
            "sent": sent_today,
            "remaining": (policy.max_per_day - sent_today) if policy.max_per_day is not None else None,
            "next_slot_at": state.next_slot_at.isoformat() if (state and state.next_slot_at) else None,
        },
    }


@router.get("")
async def list_sending_domains(
    project_id: int,
    membership=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    """List sending domains visible to this project (owned + shared)."""
    rows = (
        db.query(SendingDomain)
        .filter(or_(SendingDomain.project_id == project_id, SendingDomain.project_id.is_(None)))
        .order_by(SendingDomain.domain.asc())
        .all()
    )
    return {"pace_mode": pace_mode(), "domains": [_serialize(db, r, project_id) for r in rows]}


@router.patch("/{domain_id}")
async def update_sending_domain(
    project_id: int,
    domain_id: int,
    payload: SendingDomainUpdate,
    membership=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    """Tune a domain's pace policy, warm-up, or status. A shared domain
    (project_id IS NULL) can only be edited by a project it is not owned by if
    you are an admin of that project — kept permissive since domains are tenant
    infrastructure; tighten later if needed."""
    row = db.query(SendingDomain).filter(SendingDomain.id == domain_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Sending domain not found")
    if row.project_id is not None and row.project_id != project_id:
        raise HTTPException(status_code=403, detail="Domain belongs to another project")

    if payload.status is not None:
        if payload.status not in ("active", "throttled", "paused"):
            raise HTTPException(status_code=400, detail="Invalid status")
        row.status = payload.status
    for field in (
        "max_per_minute", "max_per_day", "throttle_factor", "warmup_enabled",
        "auto_throttle_bounce_pct", "auto_pause_bounce_pct", "auto_pause_complaint_pct",
    ):
        val = getattr(payload, field)
        if val is not None:
            setattr(row, field, val)
    db.commit()
    db.refresh(row)
    return _serialize(db, row, project_id)


@router.post("/{domain_id}/pause")
async def pause_sending_domain(
    project_id: int,
    domain_id: int,
    membership=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    row = db.query(SendingDomain).filter(SendingDomain.id == domain_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Sending domain not found")
    if row.project_id is not None and row.project_id != project_id:
        raise HTTPException(status_code=403, detail="Domain belongs to another project")
    row.status = "paused"
    db.commit()
    return {"id": row.id, "domain": row.domain, "status": row.status}


@router.post("/{domain_id}/resume")
async def resume_sending_domain(
    project_id: int,
    domain_id: int,
    membership=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    row = db.query(SendingDomain).filter(SendingDomain.id == domain_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Sending domain not found")
    if row.project_id is not None and row.project_id != project_id:
        raise HTTPException(status_code=403, detail="Domain belongs to another project")
    row.status = "active"
    db.commit()
    return {"id": row.id, "domain": row.domain, "status": row.status}
