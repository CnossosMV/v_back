"""
Messaging Events Router
View recorded events from SDK and backend API
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from sqlalchemy import text, or_, func

from app.database import get_db
from app.models import Project, User
from app.models.messaging import MessagingEvent, MessagingEventSchema
from app.schemas.messaging import (
    MessagingEventResponse,
    MessagingEventGroup,
    MessagingEventGroupList,
)
from app.routers.auth import get_current_user

router = APIRouter(prefix="/projects/{project_id}/messaging/events", tags=["messaging-events"])


def get_project_or_404(db: Session, project_id: int, workspace_id: int) -> Project:
    """Get project and verify workspace access"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/", response_model=List[MessagingEventResponse])
def list_events(
    project_id: int,
    event_name: Optional[str] = Query(None, description="Filter by event name"),
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    processed: Optional[bool] = Query(None, description="Filter by processed status"),
    source: Optional[str] = Query(None, description="Filter by source (frontend/backend)"),
    external_event_id: Optional[str] = Query(None, description="Filter by client-generated event ID"),
    has_warnings: Optional[bool] = Query(None, description="Filter by processing warnings"),
    start_date: Optional[datetime] = Query(None, description="Filter events after this date"),
    end_date: Optional[datetime] = Query(None, description="Filter events before this date"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List messaging events with optional filters"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(MessagingEvent).filter(
        MessagingEvent.project_id == project_id
    )

    # Apply filters
    if event_name:
        query = query.filter(MessagingEvent.event_name == event_name)

    if user_id:
        query = query.filter(MessagingEvent.user_id == user_id)

    if processed is not None:
        query = query.filter(MessagingEvent.processed == processed)

    if source:
        query = query.filter(MessagingEvent.source == source)

    if external_event_id:
        query = query.filter(MessagingEvent.external_event_id == external_event_id)

    if has_warnings is True:
        query = query.filter(
            MessagingEvent.processing_notes.isnot(None),
            text("processing_notes->>'user_resolved' = 'false'")
        )
    elif has_warnings is False:
        query = query.filter(
            or_(
                MessagingEvent.processing_notes.is_(None),
                text("processing_notes->>'user_resolved' = 'true'")
            )
        )

    if start_date:
        query = query.filter(MessagingEvent.created_at >= start_date)

    if end_date:
        query = query.filter(MessagingEvent.created_at <= end_date)

    # Order by most recent
    query = query.order_by(MessagingEvent.created_at.desc())

    # Paginate
    events = query.offset(skip).limit(limit).all()

    return events


def _apply_event_filters(
    query,
    *,
    project_id: int,
    event_name: Optional[str],
    user_id: Optional[int],
    processed: Optional[bool],
    source: Optional[str],
    has_warnings: Optional[bool],
    start_date: Optional[datetime],
    end_date: Optional[datetime],
):
    query = query.filter(MessagingEvent.project_id == project_id)
    if event_name:
        query = query.filter(MessagingEvent.event_name == event_name)
    if user_id:
        query = query.filter(MessagingEvent.user_id == user_id)
    if processed is not None:
        query = query.filter(MessagingEvent.processed == processed)
    if source:
        query = query.filter(MessagingEvent.source == source)
    if has_warnings is True:
        query = query.filter(
            MessagingEvent.processing_notes.isnot(None),
            text("processing_notes->>'user_resolved' = 'false'")
        )
    elif has_warnings is False:
        query = query.filter(
            or_(
                MessagingEvent.processing_notes.is_(None),
                text("processing_notes->>'user_resolved' = 'true'")
            )
        )
    if start_date:
        query = query.filter(MessagingEvent.created_at >= start_date)
    if end_date:
        query = query.filter(MessagingEvent.created_at <= end_date)
    return query


@router.get("/grouped/list", response_model=MessagingEventGroupList)
def list_events_grouped(
    project_id: int,
    event_name: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    processed: Optional[bool] = Query(None),
    source: Optional[str] = Query(None),
    has_warnings: Optional[bool] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    window_seconds: int = Query(300, ge=60, le=3600),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List events grouped by (user_id, anonymous_id, event_name, source) within a fixed
    time bucket of `window_seconds`. Each group contains the underlying event rows so
    the UI can expand without an extra round-trip.
    """
    get_project_or_404(db, project_id, current_user.workspace_id)

    bucket_expr = func.to_timestamp(
        func.floor(func.extract("epoch", MessagingEvent.created_at) / window_seconds) * window_seconds
    )

    base = _apply_event_filters(
        db.query(
            MessagingEvent.user_id.label("user_id"),
            MessagingEvent.anonymous_id.label("anonymous_id"),
            MessagingEvent.event_name.label("event_name"),
            MessagingEvent.source.label("source"),
            bucket_expr.label("bucket_start"),
            func.array_agg(MessagingEvent.id).label("event_ids"),
            func.count().label("count"),
            func.min(MessagingEvent.created_at).label("first_at"),
            func.max(MessagingEvent.created_at).label("last_at"),
            func.bool_or(MessagingEvent.processed.is_(False)).label("any_unprocessed"),
        ),
        project_id=project_id,
        event_name=event_name,
        user_id=user_id,
        processed=processed,
        source=source,
        has_warnings=has_warnings,
        start_date=start_date,
        end_date=end_date,
    ).group_by(
        MessagingEvent.user_id,
        MessagingEvent.anonymous_id,
        MessagingEvent.event_name,
        MessagingEvent.source,
        bucket_expr,
    )

    # Total groups count (separate query — wraps the GROUP BY in a subquery)
    total_groups = db.query(func.count()).select_from(base.subquery()).scalar() or 0

    rows = base.order_by(func.max(MessagingEvent.created_at).desc()).offset(skip).limit(limit).all()

    if not rows:
        return MessagingEventGroupList(groups=[], total_groups=int(total_groups))

    # Fetch all underlying events in one query, then bucket per group
    all_event_ids: List[int] = []
    for r in rows:
        all_event_ids.extend(r.event_ids)

    event_rows = (
        db.query(MessagingEvent)
        .filter(MessagingEvent.id.in_(all_event_ids))
        .all()
    )
    events_by_id = {e.id: e for e in event_rows}

    groups: List[MessagingEventGroup] = []
    for r in rows:
        evs = [events_by_id[i] for i in r.event_ids if i in events_by_id]
        evs.sort(key=lambda e: e.created_at, reverse=True)
        any_warnings = any(
            e.processing_notes is not None
            and e.processing_notes.get("user_resolved") is False
            for e in evs
        )
        groups.append(MessagingEventGroup(
            user_id=r.user_id,
            anonymous_id=r.anonymous_id,
            event_name=r.event_name,
            source=r.source,
            bucket_start=r.bucket_start,
            first_at=r.first_at,
            last_at=r.last_at,
            count=r.count,
            any_unprocessed=bool(r.any_unprocessed),
            any_warnings=any_warnings,
            events=[MessagingEventResponse.model_validate(e) for e in evs],
        ))

    return MessagingEventGroupList(groups=groups, total_groups=int(total_groups))


@router.get("/{event_id}", response_model=MessagingEventResponse)
def get_event(
    project_id: int,
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific messaging event"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    event = db.query(MessagingEvent).filter(
        MessagingEvent.id == event_id,
        MessagingEvent.project_id == project_id
    ).first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    return event


@router.post("/{event_id}/replay")
async def replay_event(
    project_id: int,
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Replay an existing event through the event action engine.
    Re-processes the event as if it was just received, triggering matching actions.
    """
    from app.services.funnel_engine import FunnelEngine
    from app.models import Funnel

    get_project_or_404(db, project_id, current_user.workspace_id)

    event = db.query(MessagingEvent).filter(
        MessagingEvent.id == event_id,
        MessagingEvent.project_id == project_id
    ).first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    try:
        engine = FunnelEngine(db)
        enrolled = engine.check_event_triggers(db, event)

        results = []
        for enrollment in enrolled:
            funnel = db.query(Funnel).filter(Funnel.id == enrollment.funnel_id).first()
            results.append({
                "event_action_id": funnel.event_action_id if funnel else None,
                "funnel_id": enrollment.funnel_id,
                "enrollment_id": enrollment.id,
                "status": enrollment.status,
            })

        return {
            "success": True,
            "event_id": event.id,
            "event_name": event.event_name,
            "executions_count": len(enrolled),
            "executions": results,
            "message": f"Replayed event, triggered {len(enrolled)} enrollment(s)"
        }
    except Exception as e:
        return {
            "success": False,
            "event_id": event.id,
            "event_name": event.event_name,
            "executions_count": 0,
            "executions": [],
            "message": f"Replay failed: {str(e)}"
        }


@router.get("/names/unique")
def get_unique_event_names(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get list of unique event names in the project (hybrid: schemas + raw events)"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    # Fast path: event names from schema table (small, indexed)
    schema_names = db.query(MessagingEventSchema.event_name).filter(
        MessagingEventSchema.project_id == project_id,
        MessagingEventSchema.is_active == True
    ).all()
    names = {name[0] for name in schema_names}

    # Fallback: also query raw events for any not yet in schemas
    raw_names = db.query(MessagingEvent.event_name).filter(
        MessagingEvent.project_id == project_id
    ).distinct().all()
    names.update(name[0] for name in raw_names)

    return {"event_names": sorted(names)}


@router.get("/count/total")
def count_events(
    project_id: int,
    event_name: Optional[str] = Query(None),
    processed: Optional[bool] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get total count of events with optional filters"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(MessagingEvent).filter(
        MessagingEvent.project_id == project_id
    )

    if event_name:
        query = query.filter(MessagingEvent.event_name == event_name)

    if processed is not None:
        query = query.filter(MessagingEvent.processed == processed)

    if start_date:
        query = query.filter(MessagingEvent.created_at >= start_date)

    if end_date:
        query = query.filter(MessagingEvent.created_at <= end_date)

    return {"count": query.count()}


@router.get("/stats/by-name")
def get_event_stats_by_name(
    project_id: int,
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get event counts grouped by event name"""
    from sqlalchemy import func

    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(
        MessagingEvent.event_name,
        func.count(MessagingEvent.id).label('count')
    ).filter(
        MessagingEvent.project_id == project_id
    )

    if start_date:
        query = query.filter(MessagingEvent.created_at >= start_date)

    if end_date:
        query = query.filter(MessagingEvent.created_at <= end_date)

    results = query.group_by(MessagingEvent.event_name).order_by(
        func.count(MessagingEvent.id).desc()
    ).all()

    return {
        "stats": [
            {"event_name": name, "count": count}
            for name, count in results
        ]
    }
