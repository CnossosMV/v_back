"""
Send Layer Router — API endpoints for send config, channel capabilities,
send logs, and manual sends.
"""
import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.routers.auth import get_current_user
from app.models import (
    Project, SendLog, ProjectSendConfig, ChannelCapability, DeliveryStatusEvent,
    Funnel, FunnelEnrollment,
)
from app.models.messaging import MessagingTemplate
from app.schemas.send_layer import (
    SendRequest,
    SendLogResponse,
    SendLogDetailResponse,
    SendLogListResponse,
    DecisionTimelineItem,
    DecisionTimelineResponse,
    DeferredSendStatsResponse,
    ProjectSendConfigResponse,
    ProjectSendConfigUpdate,
    ChannelCapabilityResponse,
    ChannelRegistryResponse,
    SendLogStatsResponse,
    StatusCount,
    ChannelCount,
    SourceTypeCount,
    TimeBucket,
    PostMessageEventsListResponse,
    PostMessageEventResponse,
    DeliveryFeedbackResponse,
    SendLogClickResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/send",
    tags=["send-layer"],
    dependencies=[Depends(require_project_role("viewer"))],
)

channels_router = APIRouter(
    prefix="/channels",
    tags=["send-layer"],
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _get_project(db: Session, project_id: int, user_info: dict) -> Project:
    workspace_id = (
        user_info.workspace_id
        if hasattr(user_info, "workspace_id")
        else user_info.get("workspace_id")
    )
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True,  # noqa: E712
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# ── Project Send Config ──────────────────────────────────────────────────


@router.get("/config", response_model=ProjectSendConfigResponse)
def get_send_config(
    project_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Get project send configuration."""
    _get_project(db, project_id, user_info)
    config = db.query(ProjectSendConfig).filter(
        ProjectSendConfig.project_id == project_id,
    ).first()
    if not config:
        # Return defaults
        config = ProjectSendConfig(
            id=0,
            project_id=project_id,
            default_strategy="try_fallback",
            default_fallback_order=["whatsapp", "email", "sms"],
            rate_limit_messages=10,
            rate_limit_window_minutes=5,
            quiet_hours_enabled=False,
            quiet_hours_timezone="UTC",
            quiet_hours_action="delay",
            use_send_layer=True,
        )
    return config


@router.put("/config", response_model=ProjectSendConfigResponse)
def upsert_send_config(
    project_id: int,
    payload: ProjectSendConfigUpdate,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
    _auth=Depends(require_project_role("admin")),
):
    """Create or update project send configuration."""
    _get_project(db, project_id, user_info)
    config = db.query(ProjectSendConfig).filter(
        ProjectSendConfig.project_id == project_id,
    ).first()

    update_data = payload.model_dump(exclude_unset=True)

    if config:
        for key, val in update_data.items():
            setattr(config, key, val)
    else:
        config = ProjectSendConfig(project_id=project_id, **update_data)
        db.add(config)

    db.commit()
    db.refresh(config)
    return config


# ── Channel Registry ─────────────────────────────────────────────────────


@router.get("/channel-registry", response_model=ChannelRegistryResponse)
def get_channel_registry(
    project_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Get all channels with capabilities and per-project availability."""
    _get_project(db, project_id, user_info)

    from app.services.channels.channel_registry_service import ChannelRegistryService

    svc = ChannelRegistryService(db)
    return {"channels": svc.get_registry(project_id)}


# ── Channel Capabilities ─────────────────────────────────────────────────


@channels_router.get("/capabilities", response_model=list[ChannelCapabilityResponse])
def list_channel_capabilities(
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """List all channel capabilities."""
    return db.query(ChannelCapability).order_by(ChannelCapability.channel).all()


# ── Send Logs ────────────────────────────────────────────────────────────


def _build_log_query(db: Session, project_id: int, **filters):
    """Build a filtered SendLog query. Shared by list, stats, and export."""
    query = db.query(SendLog).filter(SendLog.project_id == project_id)

    if filters.get("deferred_only"):
        query = query.filter(SendLog.render_context.isnot(None))
    if filters.get("status"):
        query = query.filter(SendLog.status == filters["status"])
    if filters.get("channel"):
        query = query.filter(SendLog.channel == filters["channel"])
    if filters.get("source_type"):
        query = query.filter(SendLog.source_type == filters["source_type"])
    if filters.get("recipient"):
        query = query.filter(SendLog.recipient.ilike(f"%{filters['recipient']}%"))
    if filters.get("date_from"):
        query = query.filter(SendLog.queued_at >= filters["date_from"])
    if filters.get("date_to"):
        query = query.filter(SendLog.queued_at <= filters["date_to"])
    if filters.get("user_id") is not None:
        query = query.filter(SendLog.user_id == filters["user_id"])
    if filters.get("template_id") is not None:
        query = query.filter(SendLog.template_id == filters["template_id"])
    if filters.get("source_id") is not None:
        query = query.filter(SendLog.source_id == filters["source_id"])
    if filters.get("instance_id") is not None:
        query = query.filter(SendLog.instance_id == filters["instance_id"])
    if filters.get("has_opened") is not None:
        if filters["has_opened"]:
            query = query.filter(SendLog.open_count > 0)
        else:
            query = query.filter(SendLog.open_count == 0)

    return query


def _event_action_source_id_for_log(db: Session, log: SendLog) -> Optional[int]:
    """Resolve hidden EventAction funnel logs to their public EventAction id."""
    if log.source_type != "funnel":
        return None

    if log.deferred_source_enrollment_id:
        enrollment = (
            db.query(FunnelEnrollment)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .filter(
                FunnelEnrollment.id == log.deferred_source_enrollment_id,
                Funnel.source == "event_action",
                Funnel.event_action_id.isnot(None),
            )
            .first()
        )
        if enrollment and enrollment.funnel:
            return enrollment.funnel.event_action_id

    if log.source_id:
        enrollment = (
            db.query(FunnelEnrollment)
            .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
            .filter(
                FunnelEnrollment.id == log.source_id,
                Funnel.source == "event_action",
                Funnel.event_action_id.isnot(None),
            )
            .first()
        )
        if enrollment and enrollment.funnel:
            return enrollment.funnel.event_action_id

        funnel = db.query(Funnel).filter(
            Funnel.id == log.source_id,
            Funnel.source == "event_action",
            Funnel.event_action_id.isnot(None),
        ).first()
        if funnel:
            return funnel.event_action_id

    return None


def _template_names_for_logs(db: Session, logs: list) -> dict:
    """Batch-fetch template names for a page of send logs."""
    template_ids = {log.template_id for log in logs if log.template_id}
    if not template_ids:
        return {}
    rows = (
        db.query(MessagingTemplate.id, MessagingTemplate.name)
        .filter(MessagingTemplate.id.in_(template_ids))
        .all()
    )
    return {row.id: row.name for row in rows}


def _send_log_response(
    db: Session, log: SendLog, template_names: Optional[dict] = None,
) -> SendLogResponse:
    response = SendLogResponse.model_validate(log)
    if log.template_id and template_names:
        response.template_name = template_names.get(log.template_id)
    event_action_id = _event_action_source_id_for_log(db, log)
    if event_action_id:
        response.source_type = "event_action"
        response.source_id = event_action_id
    return response


def _send_log_detail_response(db: Session, log: SendLog) -> SendLogDetailResponse:
    response = SendLogDetailResponse.model_validate(log)
    if log.template_id:
        response.template_name = _template_names_for_logs(db, [log]).get(log.template_id)
    event_action_id = _event_action_source_id_for_log(db, log)
    if event_action_id:
        response.source_type = "event_action"
        response.source_id = event_action_id
    return response


@router.get("/logs", response_model=SendLogListResponse)
def list_send_logs(
    project_id: int,
    status: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    deferred_only: bool = Query(False),
    recipient: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    user_id: Optional[int] = Query(None),
    template_id: Optional[int] = Query(None),
    source_id: Optional[int] = Query(None),
    instance_id: Optional[int] = Query(None),
    has_opened: Optional[bool] = Query(None),
    sort_by: Optional[str] = Query("queued_at"),
    sort_order: Optional[str] = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Paginated list of send logs with filters."""
    _get_project(db, project_id, user_info)

    query = _build_log_query(
        db, project_id, status=status, channel=channel, source_type=source_type,
        deferred_only=deferred_only, recipient=recipient, date_from=date_from,
        date_to=date_to, user_id=user_id, template_id=template_id,
        source_id=source_id, instance_id=instance_id, has_opened=has_opened,
    )

    total = query.count()

    # Sort
    allowed_sort = {"queued_at": SendLog.queued_at, "sent_at": SendLog.sent_at, "status": SendLog.status, "channel": SendLog.channel}
    sort_col = allowed_sort.get(sort_by, SendLog.queued_at)
    order = sort_col.desc() if sort_order == "desc" else sort_col.asc()

    items = query.order_by(order).offset((page - 1) * page_size).limit(page_size).all()
    template_names = _template_names_for_logs(db, items)

    return SendLogListResponse(
        items=[_send_log_response(db, item, template_names) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/logs/deferred/stats", response_model=DeferredSendStatsResponse)
def get_deferred_send_stats(
    project_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Get counts of deferred send logs by resolution status."""
    from sqlalchemy import func, case
    _get_project(db, project_id, user_info)

    # Count send_logs that were originally deferred (have render_context)
    # or currently have deferred/expired/skipped status
    results = db.query(
        func.count(case((SendLog.status == "deferred", 1))).label("deferred"),
        func.count(case((SendLog.status == "expired", 1))).label("expired"),
        func.count(case((SendLog.status == "skipped", 1))).label("skipped"),
        func.count(case(
            (SendLog.status == "sent", 1),
        )).label("sent"),
        func.count(SendLog.id).label("total"),
    ).filter(
        SendLog.project_id == project_id,
        SendLog.render_context.isnot(None),
    ).first()

    return DeferredSendStatsResponse(
        deferred=results.deferred or 0,
        expired=results.expired or 0,
        skipped=results.skipped or 0,
        sent=results.sent or 0,
        total=results.total or 0,
    )


@router.get("/logs/stats", response_model=SendLogStatsResponse)
def get_send_log_stats(
    project_id: int,
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    channel: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Aggregate stats for send logs."""
    _get_project(db, project_id, user_info)

    query = _build_log_query(
        db, project_id, date_from=date_from, date_to=date_to,
        channel=channel, source_type=source_type,
    )

    total = query.count()
    if total == 0:
        return SendLogStatsResponse()

    # By status
    by_status_rows = (
        query.with_entities(SendLog.status, func.count(SendLog.id))
        .group_by(SendLog.status).all()
    )
    by_status = [StatusCount(status=s, count=c) for s, c in by_status_rows]

    # By channel
    by_channel_rows = (
        query.with_entities(SendLog.channel, func.count(SendLog.id))
        .group_by(SendLog.channel).all()
    )
    by_channel = [ChannelCount(channel=ch, count=c) for ch, c in by_channel_rows]

    # By source type
    by_source_rows = (
        query.with_entities(SendLog.source_type, func.count(SendLog.id))
        .group_by(SendLog.source_type).all()
    )
    by_source_type = [SourceTypeCount(source_type=st, count=c) for st, c in by_source_rows]

    # Rates
    status_map = {s.status: s.count for s in by_status}
    delivered = status_map.get("delivered", 0) + status_map.get("read", 0)
    sent_total = delivered + status_map.get("sent", 0) + status_map.get("failed", 0)
    failed = status_map.get("failed", 0)

    delivery_rate = round((delivered / sent_total * 100) if sent_total > 0 else 0, 1)
    failure_rate = round((failed / total * 100) if total > 0 else 0, 1)

    # Email open rate
    email_total = query.filter(SendLog.channel == "email").count()
    email_opened = query.filter(SendLog.channel == "email", SendLog.open_count > 0).count()
    open_rate = round((email_opened / email_total * 100) if email_total > 0 else 0, 1)

    # Over time — determine bucket size
    effective_from = date_from
    effective_to = date_to or datetime.utcnow()
    if not effective_from:
        first_log = query.order_by(SendLog.queued_at.asc()).first()
        effective_from = first_log.queued_at if first_log else effective_to - timedelta(days=7)

    range_days = (effective_to - effective_from).days
    trunc_unit = "day" if range_days > 7 else "hour"

    time_rows = (
        query.with_entities(
            func.date_trunc(trunc_unit, SendLog.queued_at).label("period"),
            func.count(SendLog.id).label("count"),
        )
        .group_by(text("1"))
        .order_by(text("1"))
        .all()
    )
    over_time = [
        TimeBucket(period=row.period.isoformat() if row.period else "", count=row.count)
        for row in time_rows
    ]

    return SendLogStatsResponse(
        total=total,
        by_status=by_status,
        by_channel=by_channel,
        by_source_type=by_source_type,
        delivery_rate=delivery_rate,
        open_rate=open_rate,
        failure_rate=failure_rate,
        over_time=over_time,
    )


@router.get("/logs/export")
def export_send_logs(
    project_id: int,
    status: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    recipient: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    user_id: Optional[int] = Query(None),
    template_id: Optional[int] = Query(None),
    source_id: Optional[int] = Query(None),
    instance_id: Optional[int] = Query(None),
    has_opened: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Export send logs as CSV (max 10,000 rows)."""
    _get_project(db, project_id, user_info)

    query = _build_log_query(
        db, project_id, status=status, channel=channel, source_type=source_type,
        recipient=recipient, date_from=date_from, date_to=date_to,
        user_id=user_id, template_id=template_id, source_id=source_id,
        instance_id=instance_id, has_opened=has_opened,
    )

    query = query.order_by(SendLog.queued_at.desc()).limit(10000)

    headers = [
        "id", "channel", "recipient", "status", "source_type", "source_id",
        "content_summary", "queued_at", "sent_at", "delivered_at", "read_at",
        "failed_at", "opened_at", "open_count", "error_message", "instance_id",
        "template_id", "template_name", "provider_message_id",
    ]

    def _fmt(val):
        if val is None:
            return ""
        if isinstance(val, datetime):
            return val.isoformat()
        return str(val)

    template_name_cache: dict = {}

    def _template_name(template_id):
        if not template_id:
            return None
        if template_id not in template_name_cache:
            row = (
                db.query(MessagingTemplate.name)
                .filter(MessagingTemplate.id == template_id)
                .first()
            )
            template_name_cache[template_id] = row.name if row else None
        return template_name_cache[template_id]

    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for log in query.yield_per(500):
            writer.writerow([
                _fmt(_template_name(log.template_id)) if h == "template_name"
                else _fmt(getattr(log, h, None))
                for h in headers
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=send_logs_{project_id}.csv"},
    )


@router.get("/logs/{log_id}", response_model=SendLogDetailResponse)
def get_send_log(
    project_id: int,
    log_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Get a single send log with delivery events."""
    _get_project(db, project_id, user_info)

    log = db.query(SendLog).filter(
        SendLog.id == log_id,
        SendLog.project_id == project_id,
    ).first()
    if not log:
        raise HTTPException(status_code=404, detail="Send log not found")

    # Enrich with funnel_id for deferred sends so the UI can build retry links
    response = _send_log_detail_response(db, log)
    if log.deferred_source_enrollment_id:
        from app.models import FunnelEnrollment
        enrollment = db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == log.deferred_source_enrollment_id,
        ).first()
        if enrollment:
            response.deferred_funnel_id = enrollment.funnel_id
    return response


@router.get("/logs/{log_id}/feedback", response_model=list[DeliveryFeedbackResponse])
def get_send_log_feedback(
    project_id: int,
    log_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Get delivery feedback records for a specific send log."""
    _get_project(db, project_id, user_info)
    from app.services.channels.delivery_feedback_service import DeliveryFeedbackService
    return DeliveryFeedbackService(db).get_feedback_for_send_log(log_id)


@router.get("/logs/{log_id}/clicks", response_model=list[SendLogClickResponse])
def get_send_log_clicks(
    project_id: int,
    log_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Get per-link click data for a specific send log."""
    _get_project(db, project_id, user_info)
    from app.models import SendLogClick
    return db.query(SendLogClick).filter(
        SendLogClick.send_log_id == log_id,
    ).order_by(SendLogClick.link_index).all()


@router.get("/logs/{log_id}/post-events", response_model=PostMessageEventsListResponse)
def get_post_message_events(
    project_id: int,
    log_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Get events for the same user that occurred after this message was sent.

    Resolves the user from send_log.user_id or by matching the recipient
    against messaging_users email/phone. Returns events ordered by created_at.
    """
    _get_project(db, project_id, user_info)

    log = db.query(SendLog).filter(
        SendLog.id == log_id,
        SendLog.project_id == project_id,
    ).first()
    if not log:
        raise HTTPException(status_code=404, detail="Send log not found")

    from app.models.messaging import MessagingEvent, MessagingUser

    # Resolve user_id
    resolved_user_id = log.user_id
    if not resolved_user_id and log.recipient:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
        ).filter(
            (MessagingUser.email == log.recipient)
            | (MessagingUser.phone == log.recipient)
            | (MessagingUser.phone_e164 == log.recipient)
            | (MessagingUser.external_id == log.recipient)
        ).first()
        if user:
            resolved_user_id = user.id

    if not resolved_user_id:
        return PostMessageEventsListResponse(
            items=[], total=0, send_log_id=log.id, user_id=None, since=None,
        )

    # Events since message was sent (or queued if not yet sent)
    since = log.sent_at or log.queued_at

    query = db.query(MessagingEvent).filter(
        MessagingEvent.project_id == project_id,
        MessagingEvent.user_id == resolved_user_id,
        MessagingEvent.created_at >= since,
    )

    if event_name:
        query = query.filter(MessagingEvent.event_name == event_name)

    total = query.count()
    items = (
        query.order_by(MessagingEvent.created_at.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return PostMessageEventsListResponse(
        items=[PostMessageEventResponse.model_validate(e) for e in items],
        total=total,
        send_log_id=log.id,
        user_id=resolved_user_id,
        since=since,
    )


# ── Manual Send ──────────────────────────────────────────────────────────


@router.post("/")
async def manual_send(
    project_id: int,
    payload: SendRequest,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
    auth=Depends(require_project_role("editor")),
):
    """Manually send a message through the send pipeline."""
    _get_project(db, project_id, user_info)
    if payload.skip_policy and auth.get("role") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Only project admins can bypass send policy")

    from app.services.channels.base import OutboundContent
    from app.services.channels.send_service import SendService

    content = OutboundContent(
        content_type=payload.content.content_type,
        text=payload.content.text,
        html=payload.content.html,
        subject=payload.content.subject,
        media_url=payload.content.media_url,
        media_type=payload.content.media_type,
        media_caption=payload.content.media_caption,
        template_name=payload.content.template_name,
        template_language=payload.content.template_language,
        template_components=payload.content.template_components,
        buttons=payload.content.buttons,
        metadata=payload.content.metadata or {},
    )

    svc = SendService(db)
    decision = await svc.send(
        project_id=project_id,
        user_id=payload.user_id,
        recipient=payload.recipient,
        content=content,
        channel=payload.channel,
        fallback_order=payload.fallback_order,
        on_channel_unavailable=payload.on_channel_unavailable,
        source_type=payload.source_type,
        source_id=payload.source_id,
        instance_config=payload.instance_config,
        scheduled_at=payload.scheduled_at,
        skip_policy=payload.skip_policy,
    )

    db.commit()

    return {
        "success": decision.success,
        "send_log_id": decision.send_log_id,
        "channel_used": decision.channel_used,
        "status": decision.status,
        "error": decision.error,
        "decision_trace": decision.decision_trace,
    }


RETRYABLE_STATUSES = {"failed", "exhausted", "expired", "deferred"}
POLICY_BLOCK_ERROR_MARKERS = (
    "blocked by missing consent",
    "blocked by opt-out",
    "blocked by contact verification policy",
)


def _is_policy_blocked(log: SendLog) -> bool:
    if log.status == "blocked":
        return True
    error = (log.error_message or "").lower()
    return any(marker in error for marker in POLICY_BLOCK_ERROR_MARKERS)


def _find_root_deferred(db, log) -> Optional[SendLog]:
    """Walk the retry chain back to find the original log with render_context."""
    current = log
    visited = {log.id}
    while True:
        parent_id = current.retry_of_id or (
            current.source_id if current.source_type == "retry" else None
        )
        if not parent_id or parent_id in visited:
            break
        visited.add(parent_id)
        parent = db.query(SendLog).filter(SendLog.id == parent_id).first()
        if not parent:
            break
        current = parent
        if current.render_context or current.deferred_source_enrollment_id:
            break
    return current if current.id != log.id else None


@router.post("/logs/{log_id}/retry")
async def retry_send_log(
    project_id: int,
    log_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
    _auth=Depends(require_project_role("editor")),
):
    """Retry a failed/exhausted/expired send log."""
    _get_project(db, project_id, user_info)

    # Lock the row to prevent concurrent retries
    original = db.query(SendLog).filter(
        SendLog.id == log_id,
        SendLog.project_id == project_id,
    ).with_for_update().first()

    if not original:
        raise HTTPException(status_code=404, detail="Send log not found")

    if _is_policy_blocked(original):
        raise HTTPException(
            status_code=409,
            detail=(
                "Policy-blocked sends cannot be retried as transport failures. "
                "Correct the originating intent or permission, then re-evaluate "
                "the original business event."
            ),
        )

    if original.status not in RETRYABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry send with status '{original.status}'. "
                   f"Retryable statuses: {', '.join(sorted(RETRYABLE_STATUSES))}",
        )

    # Clear auto opt-outs caused by temporary/reclassified errors so retry can proceed.
    # Walk the full retry chain since the feedback is on the root log, not the current one.
    if original.user_id and original.channel:
        from app.models.messaging import MessagingUser
        user = db.query(MessagingUser).filter(
            MessagingUser.id == original.user_id,
        ).first()
        if user:
            opted_out = list(user.opted_out_channels or [])
            if original.channel in opted_out:
                from app.models import DeliveryFeedback

                # Collect all log IDs in the retry chain
                chain_ids = {log_id}
                _cur = original
                _visited = {original.id}
                while True:
                    _pid = _cur.retry_of_id or (
                        _cur.source_id if _cur.source_type == "retry" else None
                    )
                    if not _pid or _pid in _visited:
                        break
                    _visited.add(_pid)
                    chain_ids.add(_pid)
                    _cur = db.query(SendLog).filter(SendLog.id == _pid).first()
                    if not _cur:
                        break

                # Check if ANY log in the chain had a recoverable error
                recoverable = db.query(DeliveryFeedback).filter(
                    DeliveryFeedback.send_log_id.in_(chain_ids),
                    DeliveryFeedback.feedback_type.in_([
                        "temporarily_unreachable", "policy_violation",
                    ]),
                ).first()

                # Also check for errors that were reclassified (131026, 470 used to
                # trigger immediate opt-out but are now temporary/policy)
                if not recoverable:
                    recoverable = db.query(DeliveryFeedback).filter(
                        DeliveryFeedback.send_log_id.in_(chain_ids),
                        DeliveryFeedback.provider_code.in_(["131026", "470"]),
                    ).first()

                if recoverable:
                    opted_out.remove(original.channel)
                    user.opted_out_channels = opted_out
                    logger.info(
                        "Cleared recoverable opt-out for user %d channel %s before retry "
                        "(feedback %d, code %s)",
                        original.user_id, original.channel,
                        recoverable.id, recoverable.provider_code,
                    )

    # Trace the retry chain back to the root log with render_context.
    # Retries lose render_context, instance_id, and enrollment link —
    # we need the original deferred log to reconstruct the real content.
    source_log = original
    if not original.render_context and not original.content_payload:
        root = _find_root_deferred(db, original)
        if root:
            source_log = root
            logger.info(
                "Retry %d: traced back to root log %d with render_context",
                original.id, root.id,
            )

    # Reconstruct content from stored payload or render_context
    from app.services.channels.base import OutboundContent
    from app.services.channels.send_service import SendService

    if original.content_payload:
        content = OutboundContent.from_dict(original.content_payload)
    elif source_log.render_context:
        from app.services.channels.scheduled_send_worker import ScheduledSendWorker
        rendered = ScheduledSendWorker()._re_render_content(db, source_log)
        content = rendered or OutboundContent(
            content_type=original.content_type or "text",
            text=original.content_summary,
        )
    else:
        content = OutboundContent(
            content_type=original.content_type or "text",
            text=original.content_summary,
        )

    # Build instance_config — check original, then root, then render_context
    # instance_id can be at render_context top level (new logs) or nested
    # in render_context.config (old logs created before the fix).
    instance_config = None
    for log_to_check in [original, source_log]:
        if instance_config:
            break
        if log_to_check.instance_id:
            instance_config = {"instance_id": log_to_check.instance_id}
        elif log_to_check.render_context:
            iid = log_to_check.render_context.get("instance_id")
            if not iid:
                cfg = log_to_check.render_context.get("config")
                if isinstance(cfg, dict):
                    iid = cfg.get("instance_id")
            if iid:
                instance_config = {"instance_id": iid}

    # Restrict fallback to the intended channel
    effective_fallback = original.fallback_order
    if effective_fallback is None:
        intended_channel = original.channel or original.preferred_channel
        if intended_channel:
            effective_fallback = [intended_channel]

    # Resolve enrollment ID from original or root for funnel resumption
    enrollment_id = (
        original.deferred_source_enrollment_id
        or source_log.deferred_source_enrollment_id
    )

    svc = SendService(db)
    decision = await svc.send(
        project_id=project_id,
        user_id=original.user_id,
        recipient=original.recipient,
        content=content,
        channel=original.channel or original.preferred_channel,
        fallback_order=effective_fallback,
        source_type="retry",
        source_id=original.id,
        instance_config=instance_config,
        template_id=original.template_id or source_log.template_id,
        # Context propagation — born with the log, not patched after
        render_context=source_log.render_context,
        deferred_source_enrollment_id=enrollment_id,
        retry_of_id=original.id,
    )

    # If the original was a deferred funnel send and the retry succeeded,
    # resume the funnel enrollment so it advances past the stuck step.
    enrollment_resumed = False
    if decision.success and enrollment_id:
        try:
            from app.models import FunnelEnrollment, FunnelStep
            enrollment = db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == enrollment_id,
                FunnelEnrollment.status == "active",
            ).first()
            if enrollment and enrollment.current_step:
                from app.services.funnel_engine import FunnelEngine
                engine = FunnelEngine(db)
                engine._log(enrollment, enrollment.current_step, "deferred_send_resolved", {
                    "send_log_id": original.id,
                    "retry_log_id": decision.send_log_id,
                    "resolution": "sent_via_retry",
                })
                engine._advance_to_next(enrollment, enrollment.current_step)
                enrollment_resumed = True
        except Exception as e:
            logger.warning("Error resuming enrollment %s after retry: %s",
                           original.deferred_source_enrollment_id, e)

    db.commit()

    return {
        "success": decision.success,
        "send_log_id": decision.send_log_id,
        "channel_used": decision.channel_used,
        "status": decision.status,
        "error": decision.error,
        "decision_trace": decision.decision_trace,
        "original_log_id": original.id,
        "enrollment_resumed": enrollment_resumed,
    }


@router.post("/contacts/{user_id}/clear-opt-out")
def clear_contact_opt_out(
    project_id: int,
    user_id: int,
    channel: str = Query(..., description="Channel to clear opt-out for (whatsapp, email, sms)"),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
    _auth=Depends(require_project_role("admin")),
):
    """Clear an auto-opt-out for a contact on a specific channel."""
    _get_project(db, project_id, user_info)

    from app.models.messaging import MessagingUser
    contact = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    opted_out = list(contact.opted_out_channels or [])
    if channel not in opted_out:
        return {"cleared": False, "reason": f"Contact is not opted out of {channel}"}

    opted_out.remove(channel)
    contact.opted_out_channels = opted_out
    db.commit()

    logger.info(
        "Manually cleared opt-out for user %d channel %s (project %d, by %s)",
        user_id,
        channel,
        project_id,
        getattr(user_info, "id", None) or (
            user_info.get("sub", "unknown") if isinstance(user_info, dict) else "unknown"
        ),
    )

    return {"cleared": True, "channel": channel, "remaining_opt_outs": opted_out}


@router.get("/contacts/{user_id}/decisions", response_model=DecisionTimelineResponse)
def get_contact_decisions(
    project_id: int,
    user_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Decision view: the recent outbound decisions for a contact — what was
    selected/sent/held/superseded/expired and the reasoning trace behind each.
    Powers the per-contact 'why did they get this and not that' surface."""
    _get_project(db, project_id, user_info)

    from app.models.messaging import MessagingUser
    contact = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    logs = db.query(SendLog).filter(
        SendLog.project_id == project_id,
        SendLog.user_id == user_id,
    ).order_by(SendLog.queued_at.desc()).limit(limit).all()

    items = [DecisionTimelineItem.model_validate(l) for l in logs]
    recipient = contact.email or contact.phone or contact.phone_e164 or contact.external_id
    return DecisionTimelineResponse(
        items=items, total=len(items), contact_id=user_id, recipient=recipient,
    )


@router.get("/contacts/{user_id}/budget")
def get_contact_attention_budget(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Guardian attention-budget snapshot for a contact (cap/used/remaining,
    per period + per channel). Powers the Decision view attention view."""
    _get_project(db, project_id, user_info)
    from app.services.guardian.guardian_service import GuardianService
    return {
        "user_id": user_id,
        "budget": GuardianService(db).budget_snapshot(project_id, user_id),
    }
