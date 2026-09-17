"""
Channel Health — admin-only dashboard endpoints.

Provides per-project and per-instance delivery health metrics
aggregated from send_logs.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.services.channels.channel_health_service import ChannelHealthService

router = APIRouter(
    prefix="/projects/{project_id}/channel-health",
    tags=["channel-health"],
)


@router.get("")
async def get_project_health(
    project_id: int,
    hours: int = Query(24, ge=1, le=168),
    membership=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    """Project-wide channel health summary."""
    svc = ChannelHealthService(db)
    return svc.get_project_health(project_id, hours)


@router.get("/instances")
async def get_instances_summary(
    project_id: int,
    hours: int = Query(24, ge=1, le=168),
    membership=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    """List all instances with summary metrics."""
    svc = ChannelHealthService(db)
    return svc.get_instances_summary(project_id, hours)


@router.get("/instances/{instance_id}")
async def get_instance_health(
    project_id: int,
    instance_id: int,
    hours: int = Query(24, ge=1, le=168),
    membership=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    """Detailed health metrics for a specific channel instance."""
    svc = ChannelHealthService(db)
    return svc.get_instance_health(project_id, instance_id, hours)
