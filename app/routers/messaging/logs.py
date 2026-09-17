"""
Messaging Logs Router
View message delivery logs with status tracking
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from app.database import get_db
from app.models import Project, User
from app.models.messaging import MessagingLog, MessageStatus
from app.schemas.messaging import MessagingLogResponse, MessagingLogDetailResponse, MessagingOverviewStats
from app.routers.auth import get_current_user

router = APIRouter(prefix="/projects/{project_id}/messaging/logs", tags=["messaging-logs"])


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


@router.get("/", response_model=List[MessagingLogResponse])
def list_logs(
    project_id: int,
    status: Optional[str] = Query(None, description="Filter by status"),
    template_id: Optional[int] = Query(None, description="Filter by template ID"),
    channel_id: Optional[int] = Query(None, description="Filter by channel ID"),
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    recipient: Optional[str] = Query(None, description="Filter by recipient"),
    start_date: Optional[datetime] = Query(None, description="Filter logs after this date"),
    end_date: Optional[datetime] = Query(None, description="Filter logs before this date"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List messaging logs with optional filters"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(MessagingLog).filter(
        MessagingLog.project_id == project_id
    )

    # Apply filters
    if status:
        try:
            status_enum = MessageStatus(status)
            query = query.filter(MessagingLog.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if template_id:
        query = query.filter(MessagingLog.template_id == template_id)

    if channel_id:
        query = query.filter(MessagingLog.channel_id == channel_id)

    if user_id:
        query = query.filter(MessagingLog.user_id == user_id)

    if recipient:
        query = query.filter(MessagingLog.recipient.ilike(f"%{recipient}%"))

    if start_date:
        query = query.filter(MessagingLog.created_at >= start_date)

    if end_date:
        query = query.filter(MessagingLog.created_at <= end_date)

    # Order by most recent
    query = query.order_by(MessagingLog.created_at.desc())

    # Paginate
    logs = query.offset(skip).limit(limit).all()

    return logs


@router.get("/{log_id}", response_model=MessagingLogDetailResponse)
def get_log(
    project_id: int,
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific messaging log with related objects"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    log = db.query(MessagingLog).filter(
        MessagingLog.id == log_id,
        MessagingLog.project_id == project_id
    ).first()

    if not log:
        raise HTTPException(status_code=404, detail="Log not found")

    return log


@router.get("/count/total")
def count_logs(
    project_id: int,
    status: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get total count of logs with optional filters"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(MessagingLog).filter(
        MessagingLog.project_id == project_id
    )

    if status:
        try:
            status_enum = MessageStatus(status)
            query = query.filter(MessagingLog.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if start_date:
        query = query.filter(MessagingLog.created_at >= start_date)

    if end_date:
        query = query.filter(MessagingLog.created_at <= end_date)

    return {"count": query.count()}


@router.get("/stats/by-status")
def get_stats_by_status(
    project_id: int,
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get log counts grouped by status"""
    from sqlalchemy import func

    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(
        MessagingLog.status,
        func.count(MessagingLog.id).label('count')
    ).filter(
        MessagingLog.project_id == project_id
    )

    if start_date:
        query = query.filter(MessagingLog.created_at >= start_date)

    if end_date:
        query = query.filter(MessagingLog.created_at <= end_date)

    results = query.group_by(MessagingLog.status).all()

    return {
        "stats": [
            {"status": status.value if status else "unknown", "count": count}
            for status, count in results
        ]
    }


@router.get("/overview/stats", response_model=MessagingOverviewStats)
def get_overview_stats(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get overview statistics for the messaging dashboard"""
    from sqlalchemy import func
    from datetime import datetime, timedelta
    from app.models.messaging import MessagingUser, MessagingEvent, MessagingTemplate, MessagingChannel

    get_project_or_404(db, project_id, current_user.workspace_id)

    # Today's date for filtering
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    # Total users
    total_users = db.query(func.count(MessagingUser.id)).filter(
        MessagingUser.project_id == project_id
    ).scalar() or 0

    # Events today
    total_events_today = db.query(func.count(MessagingEvent.id)).filter(
        MessagingEvent.project_id == project_id,
        MessagingEvent.created_at >= today_start
    ).scalar() or 0

    # Message counts by status
    sent_count = db.query(func.count(MessagingLog.id)).filter(
        MessagingLog.project_id == project_id,
        MessagingLog.status.in_([MessageStatus.sent, MessageStatus.delivered])
    ).scalar() or 0

    delivered_count = db.query(func.count(MessagingLog.id)).filter(
        MessagingLog.project_id == project_id,
        MessagingLog.status == MessageStatus.delivered
    ).scalar() or 0

    failed_count = db.query(func.count(MessagingLog.id)).filter(
        MessagingLog.project_id == project_id,
        MessagingLog.status.in_([MessageStatus.failed, MessageStatus.bounced])
    ).scalar() or 0

    # Delivery rate
    total_attempted = sent_count + failed_count
    delivery_rate = (delivered_count / total_attempted * 100) if total_attempted > 0 else 0.0

    # Active templates
    active_templates = db.query(func.count(MessagingTemplate.id)).filter(
        MessagingTemplate.project_id == project_id,
        MessagingTemplate.is_active == True
    ).scalar() or 0

    # Active channels
    active_channels = db.query(func.count(MessagingChannel.id)).filter(
        MessagingChannel.project_id == project_id,
        MessagingChannel.is_active == True
    ).scalar() or 0

    return MessagingOverviewStats(
        total_users=total_users,
        total_events_today=total_events_today,
        total_messages_sent=sent_count,
        total_messages_delivered=delivered_count,
        total_messages_failed=failed_count,
        delivery_rate=round(delivery_rate, 2),
        active_templates=active_templates,
        active_channels=active_channels
    )
