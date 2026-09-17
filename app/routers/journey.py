"""
Event Graph Router — backfill triggers, goal-event config, and graph API endpoints.
"""
import asyncio
import logging
from datetime import date, datetime
from typing import Optional, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.models import User, Project
from app.models.journey import JourneyBackfillJob
from app.schemas.journey import (
    BackfillJobResponse, GoalEventUpdate,
    JourneyGraphResponse, UserJourneyResponse, NodeDetailResponse,
    DataHealthResponse,
)
from app.routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Event Graph"])


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


def _run_backfill_in_thread(func, project_id: int, job_id: int):
    """Run a blocking backfill function in a background thread."""
    def _work():
        db = SessionLocal()
        try:
            func(db, project_id, job_id)
        except Exception as e:
            logger.error("Backfill job %d failed: %s", job_id, e)
        finally:
            db.close()
    import threading
    t = threading.Thread(target=_work, daemon=True)
    t.start()


# ============================================================================
# Backfill endpoints
# ============================================================================

@router.post("/projects/{project_id}/journey/backfill/sessions", response_model=BackfillJobResponse)
async def backfill_sessions(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Trigger session_id backfill for historical events (last 90 days)."""
    _get_project(db, project_id, user)

    job = JourneyBackfillJob(
        project_id=project_id,
        job_type="sessions",
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    from app.services.journey.session_backfill import backfill_sessions as _backfill
    _run_backfill_in_thread(_backfill, project_id, job.id)

    return job


@router.post("/projects/{project_id}/journey/backfill/interventions", response_model=BackfillJobResponse)
async def backfill_interventions(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Trigger SendLog historical bridge (last 90 days)."""
    _get_project(db, project_id, user)

    job = JourneyBackfillJob(
        project_id=project_id,
        job_type="interventions",
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    from app.services.journey.intervention_backfill import backfill_interventions as _backfill
    _run_backfill_in_thread(_backfill, project_id, job.id)

    return job


@router.post("/projects/{project_id}/journey/refresh")
async def trigger_refresh(
    project_id: int,
    full: bool = Query(False, description="Full 90-day recompute instead of incremental"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Trigger manual materialization of the event graph."""
    _get_project(db, project_id, user)

    from app.services.journey.materializer import JourneyMaterializerWorker
    worker = JourneyMaterializerWorker()
    try:
        worker._do_materialize(db)
        return {"status": "completed", "message": "Materialization finished"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Materialization failed: {str(e)}")


@router.get("/projects/{project_id}/journey/backfill/jobs", response_model=List[BackfillJobResponse])
async def list_backfill_jobs(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List backfill job status for this project."""
    _get_project(db, project_id, user)

    jobs = db.query(JourneyBackfillJob).filter(
        JourneyBackfillJob.project_id == project_id,
    ).order_by(JourneyBackfillJob.id.desc()).limit(20).all()

    return jobs


# ============================================================================
# Goal event configuration
# ============================================================================

@router.put("/projects/{project_id}/journey/goal-event")
async def update_goal_event(
    project_id: int,
    body: GoalEventUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Set or clear the goal event for the event graph."""
    project = _get_project(db, project_id, user)
    project.goal_event = body.goal_event
    db.commit()
    return {"goal_event": project.goal_event}


@router.get("/projects/{project_id}/journey/goal-event")
async def get_goal_event(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get the current goal event (with auto-detection fallback)."""
    project = _get_project(db, project_id, user)

    from app.services.journey.graph_service import resolve_goal_event
    resolved = resolve_goal_event(db, project_id)

    return {
        "goal_event": project.goal_event,
        "resolved_goal_event": resolved,
        "is_auto_detected": resolved is not None and project.goal_event is None,
    }


# ============================================================================
# Graph API endpoints
# ============================================================================

@router.get("/projects/{project_id}/journey/graph", response_model=JourneyGraphResponse)
async def get_aggregate_graph(
    project_id: int,
    date_from: date = Query(..., description="Period start (ISO date)"),
    date_to: date = Query(..., description="Period end (ISO date)"),
    conversion_event: Optional[str] = Query(None, description="Override goal event"),
    show_interventions: bool = Query(False, description="Include intervention edges"),
    min_volume: int = Query(1, ge=1, description="Min transitions to show edge"),
    max_nodes: int = Query(30, ge=5, le=200, description="Max nodes by frequency"),
    visitor_type: str = Query("all", regex="^(all|identified|anonymous)$",
                              description="Filter graph by visitor type"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get the aggregate event graph for a project."""
    _get_project(db, project_id, user)

    from app.services.journey.graph_service import JourneyGraphService
    svc = JourneyGraphService(db)
    return svc.get_aggregate_graph(
        project_id=project_id,
        date_from=date_from,
        date_to=date_to,
        conversion_event=conversion_event,
        show_interventions=show_interventions,
        min_volume=min_volume,
        max_nodes=max_nodes,
        visitor_type=visitor_type,
    )


@router.get("/projects/{project_id}/journey/graph/user/{user_id}", response_model=UserJourneyResponse)
async def get_user_journey(
    project_id: int,
    user_id: int,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get aggregate graph with individual user path overlay."""
    _get_project(db, project_id, user)

    from app.services.journey.graph_service import JourneyGraphService
    svc = JourneyGraphService(db)
    return svc.get_user_journey(
        project_id=project_id,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/projects/{project_id}/journey/graph/anonymous/{anonymous_id}", response_model=UserJourneyResponse)
async def get_anonymous_journey(
    project_id: int,
    anonymous_id: str,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Aggregate graph + path overlay for an unidentified anonymous visitor."""
    _get_project(db, project_id, user)

    from app.services.journey.graph_service import JourneyGraphService
    svc = JourneyGraphService(db)
    return svc.get_anonymous_journey(
        project_id=project_id,
        anonymous_id=anonymous_id,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/projects/{project_id}/journey/nodes/{event_name}", response_model=NodeDetailResponse)
async def get_node_detail(
    project_id: int,
    event_name: str,
    date_from: date = Query(...),
    date_to: date = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get detail panel data for a specific node."""
    _get_project(db, project_id, user)

    from app.services.journey.graph_service import JourneyGraphService
    svc = JourneyGraphService(db)
    return svc.get_node_detail(
        project_id=project_id,
        event_name=event_name,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/projects/{project_id}/journey/data-health", response_model=DataHealthResponse)
async def get_data_health(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Analyze event data quality and return health score + recommendations."""
    _get_project(db, project_id, user)

    from app.services.journey.graph_service import get_data_health as _get_health
    return _get_health(db, project_id)
