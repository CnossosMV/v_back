"""
Message Effectiveness Scoring (MES) Router

Per-message scores, aggregations by template/channel/source/funnel-step, and configuration.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc, and_, or_
from datetime import datetime, timedelta
from typing import Dict, Optional

from app.database import get_db
from app.models import (
    User, Project, SendLog,
    MessageEffectivenessScore, MESConfig,
)
from app.schemas.mes import (
    MESScoreResponse, MESAggregateItem, MESStatsResponse,
    MESFunnelStepItem, MESConfigResponse, MESConfigUpdate,
    TemplateEffectivenessItem,
    BatchScoreRequest,
)
from app.routers.auth import get_current_user
from app.services.scoring.mes_engine import MESEngine

router = APIRouter(tags=["Message Effectiveness"])

TEST_SOURCE_TYPES = (
    "template_test",
    "meta_test",
    "email_test",
    "whatsapp_test",
    "test_send",
)


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


def _grade_dist(db, base_query):
    """Compute grade distribution from a base query of MES records."""
    rows = (
        base_query
        .with_entities(
            MessageEffectivenessScore.score_grade,
            sqlfunc.count(MessageEffectivenessScore.id),
        )
        .group_by(MessageEffectivenessScore.score_grade)
        .all()
    )
    return {row[0]: row[1] for row in rows}


def _settled_filter():
    """Filter to only scores where the attribution window has closed.

    This prevents in-flight messages (window still open, reengagement=0 just
    because the user hasn't had time to come back) from dragging down averages.
    Uses the engine-maintained `settled` flag; the created_at heuristic remains
    as a transitional fallback for rows computed before the flag existed.
    """
    now = datetime.utcnow()
    return and_(
        MessageEffectivenessScore.computed_at.isnot(None),
        MessageEffectivenessScore.score_grade != "pending",
        or_(
            MessageEffectivenessScore.settled == True,
            MessageEffectivenessScore.created_at < now - timedelta(hours=24),
        ),
    )


def _computed_mes_filter():
    return and_(
        MessageEffectivenessScore.computed_at.isnot(None),
        MessageEffectivenessScore.score_grade != "pending",
    )


# ============================================================================
# Per-message score
# ============================================================================

@router.get(
    "/projects/{project_id}/mes/send-logs/{log_id}/score",
    response_model=MESScoreResponse,
)
def get_message_score(
    project_id: int,
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    mes = db.query(MessageEffectivenessScore).filter(
        MessageEffectivenessScore.project_id == project_id,
        MessageEffectivenessScore.send_log_id == log_id,
    ).first()

    if not mes:
        # Compute on-the-fly — creates the MES record so the evolution pipeline can track it
        send_log = db.query(SendLog).filter(
            SendLog.id == log_id,
            SendLog.project_id == project_id,
        ).first()
        if not send_log:
            raise HTTPException(status_code=404, detail="Send log not found")
        engine = MESEngine(db)
        mes = engine.compute_score(log_id)
        if not mes:
            raise HTTPException(status_code=404, detail="Send log not found")
        db.commit()
    elif mes.stale:
        # Recompute stale record before returning
        engine = MESEngine(db)
        mes = engine.compute_score(log_id)
        db.commit()

    return mes


# ============================================================================
# Batch scores
# ============================================================================

@router.post(
    "/projects/{project_id}/mes/send-logs/batch-scores",
    response_model=Dict[int, MESScoreResponse],
)
def get_batch_scores(
    project_id: int,
    body: BatchScoreRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch MES scores for multiple send_logs in one request.

    Creates and computes scores on-the-fly for send_logs that don't have MES
    records yet, and recomputes stale records before returning.
    """
    _get_project(db, project_id, current_user)

    # 1. Fetch all existing MES records for requested IDs
    existing = (
        db.query(MessageEffectivenessScore)
        .filter(
            MessageEffectivenessScore.project_id == project_id,
            MessageEffectivenessScore.send_log_id.in_(body.send_log_ids),
        )
        .all()
    )
    result = {}
    stale_ids = []
    for mes in existing:
        if mes.stale:
            stale_ids.append(mes.send_log_id)
        else:
            result[mes.send_log_id] = mes

    # 2. Compute on-the-fly for missing IDs + recompute stale
    missing_ids = set(body.send_log_ids) - set(result.keys())
    compute_ids = missing_ids | set(stale_ids)

    if compute_ids:
        engine = MESEngine(db)
        for log_id in compute_ids:
            try:
                mes = engine.compute_score(log_id)
                if mes and mes.project_id == project_id:
                    result[mes.send_log_id] = mes
            except Exception:
                pass
        db.commit()

    return result


# ============================================================================
# Aggregate stats
# ============================================================================

@router.get(
    "/projects/{project_id}/mes/stats",
    response_model=MESStatsResponse,
)
def get_mes_stats(
    project_id: int,
    channel: Optional[str] = None,
    source_type: Optional[str] = None,
    template_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    q = db.query(MessageEffectivenessScore).filter(
        MessageEffectivenessScore.project_id == project_id,
        MessageEffectivenessScore.computed_at.isnot(None),
    )
    if channel:
        q = q.filter(MessageEffectivenessScore.channel == channel)
    if source_type:
        q = q.filter(MessageEffectivenessScore.source_type == source_type)
    if template_id:
        q = q.filter(MessageEffectivenessScore.template_id == template_id)
    if date_from:
        q = q.filter(MessageEffectivenessScore.created_at >= date_from)
    if date_to:
        q = q.filter(MessageEffectivenessScore.created_at <= date_to)

    agg = q.with_entities(
        sqlfunc.count(MessageEffectivenessScore.id),
        sqlfunc.avg(MessageEffectivenessScore.reach_score),
        sqlfunc.avg(MessageEffectivenessScore.reengagement_score),
        sqlfunc.avg(MessageEffectivenessScore.combined_score),
    ).first()

    total_pending = db.query(sqlfunc.count(MessageEffectivenessScore.id)).filter(
        MessageEffectivenessScore.project_id == project_id,
        MessageEffectivenessScore.score_grade == "pending",
    ).scalar() or 0

    return MESStatsResponse(
        total_scored=agg[0] or 0,
        total_pending=total_pending,
        avg_reach=round(agg[1] or 0, 2),
        avg_reengagement=round(agg[2] or 0, 2),
        avg_combined=round(agg[3] or 0, 2),
        grade_distribution=_grade_dist(db, q),
    )


# ============================================================================
# By template
# ============================================================================

@router.get(
    "/projects/{project_id}/mes/templates/effectiveness",
    response_model=list[TemplateEffectivenessItem],
)
def get_template_effectiveness(
    project_id: int,
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    channel: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    min_sample_size: int = Query(1, ge=1),
    sort_by: str = Query("avg_combined"),
    sort_order: str = Query("desc"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Aggregate production-only template effectiveness metrics.

    Test sends are always excluded so this endpoint reflects real customer
    messaging performance.
    """
    _get_project(db, project_id, current_user)

    from app.models.messaging import MessagingTemplate

    def apply_send_filters(query):
        query = query.filter(
            SendLog.project_id == project_id,
            SendLog.template_id.isnot(None),
            SendLog.source_type.notin_(TEST_SOURCE_TYPES),
        )
        if date_from:
            query = query.filter(SendLog.queued_at >= date_from)
        if date_to:
            query = query.filter(SendLog.queued_at <= date_to)
        if channel:
            query = query.filter(or_(SendLog.channel == channel, MessagingTemplate.channel_type == channel))
        if source_type:
            query = query.filter(SendLog.source_type == source_type)
        return query

    computed_mes = _computed_mes_filter()
    total_sends = sqlfunc.count(SendLog.id)
    delivered_count = sqlfunc.count(SendLog.id).filter(SendLog.status.in_(["delivered", "read"]))
    read_count = sqlfunc.count(SendLog.id).filter(or_(SendLog.status == "read", SendLog.read_at.isnot(None)))
    failed_count = sqlfunc.count(SendLog.id).filter(SendLog.status == "failed")
    unique_opens = sqlfunc.count(SendLog.id).filter(or_(SendLog.opened_at.isnot(None), SendLog.open_count > 0))
    unique_clicks = sqlfunc.count(SendLog.id).filter(or_(SendLog.first_click_at.isnot(None), SendLog.click_count > 0))
    replied_count = sqlfunc.count(SendLog.id).filter(SendLog.replied_at.isnot(None))
    mes_sample_size = sqlfunc.count(MessageEffectivenessScore.id).filter(computed_mes)

    rows = (
        apply_send_filters(
            db.query(
                MessagingTemplate.id,
                MessagingTemplate.name,
                MessagingTemplate.slug,
                MessagingTemplate.locale,
                MessagingTemplate.channel_type,
                sqlfunc.max(sqlfunc.coalesce(SendLog.sent_at, SendLog.queued_at)).label("last_sent_at"),
                total_sends.label("total_sends"),
                delivered_count.label("delivered_count"),
                read_count.label("read_count"),
                failed_count.label("failed_count"),
                unique_opens.label("unique_opens"),
                unique_clicks.label("unique_clicks"),
                replied_count.label("replied_count"),
                sqlfunc.avg(MessageEffectivenessScore.reach_score).filter(computed_mes).label("avg_reach"),
                sqlfunc.avg(MessageEffectivenessScore.reengagement_score).filter(computed_mes).label("avg_reengagement"),
                sqlfunc.avg(MessageEffectivenessScore.combined_score).filter(computed_mes).label("avg_combined"),
                mes_sample_size.label("mes_sample_size"),
            )
            .select_from(SendLog)
            .join(MessagingTemplate, MessagingTemplate.id == SendLog.template_id)
            .outerjoin(MessageEffectivenessScore, MessageEffectivenessScore.send_log_id == SendLog.id)
        )
        .group_by(
            MessagingTemplate.id,
            MessagingTemplate.name,
            MessagingTemplate.slug,
            MessagingTemplate.locale,
            MessagingTemplate.channel_type,
        )
        .having(sqlfunc.count(SendLog.id) >= min_sample_size)
        .all()
    )

    result = []
    for row in rows:
        sends = int(row.total_sends or 0)
        delivered = int(row.delivered_count or 0)
        read = int(row.read_count or 0)
        failed = int(row.failed_count or 0)
        opens = int(row.unique_opens or 0)
        clicks = int(row.unique_clicks or 0)
        replied = int(row.replied_count or 0)

        grade_query = (
            db.query(
                MessageEffectivenessScore.score_grade,
                sqlfunc.count(MessageEffectivenessScore.id),
            )
            .join(SendLog, SendLog.id == MessageEffectivenessScore.send_log_id)
            .join(MessagingTemplate, MessagingTemplate.id == SendLog.template_id)
            .filter(
                MessageEffectivenessScore.project_id == project_id,
                MessageEffectivenessScore.template_id == row.id,
                computed_mes,
                SendLog.source_type.notin_(TEST_SOURCE_TYPES),
            )
        )
        if date_from:
            grade_query = grade_query.filter(SendLog.queued_at >= date_from)
        if date_to:
            grade_query = grade_query.filter(SendLog.queued_at <= date_to)
        if channel:
            grade_query = grade_query.filter(or_(SendLog.channel == channel, MessagingTemplate.channel_type == channel))
        if source_type:
            grade_query = grade_query.filter(SendLog.source_type == source_type)

        grade_distribution = {
            grade: count
            for grade, count in grade_query.group_by(MessageEffectivenessScore.score_grade).all()
        }

        channel_type = row.channel_type.value if hasattr(row.channel_type, "value") else row.channel_type
        result.append(TemplateEffectivenessItem(
            template_id=row.id,
            name=row.name,
            slug=row.slug,
            locale=row.locale,
            channel_type=channel_type,
            last_sent_at=row.last_sent_at,
            total_sends=sends,
            delivered_count=delivered,
            read_count=read,
            failed_count=failed,
            delivery_rate=round((delivered / sends * 100) if sends else 0, 2),
            failure_rate=round((failed / sends * 100) if sends else 0, 2),
            read_rate=round((read / delivered * 100) if delivered else 0, 2),
            unique_opens=opens,
            unique_clicks=clicks,
            open_rate=round((opens / delivered * 100) if delivered else 0, 2),
            click_rate=round((clicks / delivered * 100) if delivered else 0, 2),
            replied_count=replied,
            reply_rate=round((replied / delivered * 100) if delivered else 0, 2),
            avg_reach=round(float(row.avg_reach or 0), 2),
            avg_reengagement=round(float(row.avg_reengagement or 0), 2),
            avg_combined=round(float(row.avg_combined or 0), 2),
            mes_sample_size=int(row.mes_sample_size or 0),
            grade_distribution=grade_distribution,
        ))

    # Bayesian smoothing: shrink each template's avg_combined toward the
    # project-wide (sample-weighted) mean so a 3-send template can't outrank
    # a 500-send one on noise. Prior weight = 10 virtual sends.
    PRIOR_WEIGHT = 10
    total_weight = sum(item.mes_sample_size for item in result)
    if total_weight > 0:
        prior = sum(item.avg_combined * item.mes_sample_size for item in result) / total_weight
        for item in result:
            n = item.mes_sample_size
            if n > 0:
                item.avg_combined_smoothed = round(
                    (PRIOR_WEIGHT * prior + n * item.avg_combined) / (PRIOR_WEIGHT + n), 2,
                )

    allowed_sort = {
        "name": lambda item: item.name.lower(),
        "channel_type": lambda item: item.channel_type or "",
        "total_sends": lambda item: item.total_sends,
        "delivery_rate": lambda item: item.delivery_rate,
        "failure_rate": lambda item: item.failure_rate,
        "open_rate": lambda item: item.open_rate,
        "click_rate": lambda item: item.click_rate,
        "read_rate": lambda item: item.read_rate,
        "reply_rate": lambda item: item.reply_rate,
        "avg_reach": lambda item: item.avg_reach,
        "avg_reengagement": lambda item: item.avg_reengagement,
        "avg_combined": lambda item: item.avg_combined,
        "avg_combined_smoothed": lambda item: item.avg_combined_smoothed,
        "mes_sample_size": lambda item: item.mes_sample_size,
        "last_sent_at": lambda item: item.last_sent_at or datetime.min,
    }
    sort_key = allowed_sort.get(sort_by, allowed_sort["avg_combined"])
    result.sort(key=sort_key, reverse=sort_order != "asc")
    return result


@router.get(
    "/projects/{project_id}/mes/by-template",
    response_model=list[MESAggregateItem],
)
def get_mes_by_template(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    from app.models.messaging import MessagingTemplate

    rows = (
        db.query(
            MessageEffectivenessScore.template_id,
            MessagingTemplate.name,
            sqlfunc.avg(MessageEffectivenessScore.reach_score),
            sqlfunc.avg(MessageEffectivenessScore.reengagement_score),
            sqlfunc.avg(MessageEffectivenessScore.combined_score),
            sqlfunc.count(MessageEffectivenessScore.id),
        )
        .outerjoin(MessagingTemplate, MessagingTemplate.id == MessageEffectivenessScore.template_id)
        .filter(
            MessageEffectivenessScore.project_id == project_id,
            MessageEffectivenessScore.template_id.isnot(None),
            _settled_filter(),
        )
        .group_by(MessageEffectivenessScore.template_id, MessagingTemplate.name)
        .order_by(sqlfunc.avg(MessageEffectivenessScore.combined_score).desc())
        .all()
    )

    result = []
    for tid, tname, avg_r, avg_re, avg_c, cnt in rows:
        gd = _grade_dist(
            db,
            db.query(MessageEffectivenessScore).filter(
                MessageEffectivenessScore.project_id == project_id,
                MessageEffectivenessScore.template_id == tid,
                _settled_filter(),
            ),
        )
        result.append(MESAggregateItem(
            key=str(tid),
            label=tname,
            avg_reach=round(avg_r or 0, 2),
            avg_reengagement=round(avg_re or 0, 2),
            avg_combined=round(avg_c or 0, 2),
            sample_size=cnt,
            grade_distribution=gd,
        ))
    return result


# ============================================================================
# By channel
# ============================================================================

@router.get(
    "/projects/{project_id}/mes/by-channel",
    response_model=list[MESAggregateItem],
)
def get_mes_by_channel(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    rows = (
        db.query(
            MessageEffectivenessScore.channel,
            sqlfunc.avg(MessageEffectivenessScore.reach_score),
            sqlfunc.avg(MessageEffectivenessScore.reengagement_score),
            sqlfunc.avg(MessageEffectivenessScore.combined_score),
            sqlfunc.count(MessageEffectivenessScore.id),
        )
        .filter(
            MessageEffectivenessScore.project_id == project_id,
            _settled_filter(),
        )
        .group_by(MessageEffectivenessScore.channel)
        .order_by(sqlfunc.avg(MessageEffectivenessScore.combined_score).desc())
        .all()
    )

    result = []
    for ch, avg_r, avg_re, avg_c, cnt in rows:
        gd = _grade_dist(
            db,
            db.query(MessageEffectivenessScore).filter(
                MessageEffectivenessScore.project_id == project_id,
                MessageEffectivenessScore.channel == ch,
                _settled_filter(),
            ),
        )
        result.append(MESAggregateItem(
            key=ch,
            label=ch.title(),
            avg_reach=round(avg_r or 0, 2),
            avg_reengagement=round(avg_re or 0, 2),
            avg_combined=round(avg_c or 0, 2),
            sample_size=cnt,
            grade_distribution=gd,
        ))
    return result


# ============================================================================
# By source type
# ============================================================================

@router.get(
    "/projects/{project_id}/mes/by-source-type",
    response_model=list[MESAggregateItem],
)
def get_mes_by_source_type(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    rows = (
        db.query(
            MessageEffectivenessScore.source_type,
            sqlfunc.avg(MessageEffectivenessScore.reach_score),
            sqlfunc.avg(MessageEffectivenessScore.reengagement_score),
            sqlfunc.avg(MessageEffectivenessScore.combined_score),
            sqlfunc.count(MessageEffectivenessScore.id),
        )
        .filter(
            MessageEffectivenessScore.project_id == project_id,
            _settled_filter(),
        )
        .group_by(MessageEffectivenessScore.source_type)
        .order_by(sqlfunc.avg(MessageEffectivenessScore.combined_score).desc())
        .all()
    )

    result = []
    for st, avg_r, avg_re, avg_c, cnt in rows:
        gd = _grade_dist(
            db,
            db.query(MessageEffectivenessScore).filter(
                MessageEffectivenessScore.project_id == project_id,
                MessageEffectivenessScore.source_type == st,
                _settled_filter(),
            ),
        )
        result.append(MESAggregateItem(
            key=st,
            label=st.replace("_", " ").title(),
            avg_reach=round(avg_r or 0, 2),
            avg_reengagement=round(avg_re or 0, 2),
            avg_combined=round(avg_c or 0, 2),
            sample_size=cnt,
            grade_distribution=gd,
        ))
    return result


# ============================================================================
# By funnel step
# ============================================================================

@router.get(
    "/projects/{project_id}/mes/by-funnel-step/{funnel_id}",
    response_model=list[MESFunnelStepItem],
)
def get_mes_by_funnel_step(
    project_id: int,
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    from app.models import FunnelStep

    rows = (
        db.query(
            MessageEffectivenessScore.funnel_step_id,
            FunnelStep.step_type,
            FunnelStep.position,
            FunnelStep.branch,
            sqlfunc.avg(MessageEffectivenessScore.reach_score),
            sqlfunc.avg(MessageEffectivenessScore.reengagement_score),
            sqlfunc.avg(MessageEffectivenessScore.combined_score),
            sqlfunc.count(MessageEffectivenessScore.id),
        )
        .join(FunnelStep, FunnelStep.id == MessageEffectivenessScore.funnel_step_id)
        .filter(
            MessageEffectivenessScore.project_id == project_id,
            MessageEffectivenessScore.funnel_id == funnel_id,
            MessageEffectivenessScore.funnel_step_id.isnot(None),
            _settled_filter(),
        )
        .group_by(
            MessageEffectivenessScore.funnel_step_id,
            FunnelStep.step_type,
            FunnelStep.position,
            FunnelStep.branch,
        )
        .order_by(FunnelStep.position)
        .all()
    )

    result = []
    for sid, stype, pos, branch, avg_r, avg_re, avg_c, cnt in rows:
        gd = _grade_dist(
            db,
            db.query(MessageEffectivenessScore).filter(
                MessageEffectivenessScore.project_id == project_id,
                MessageEffectivenessScore.funnel_step_id == sid,
                _settled_filter(),
            ),
        )
        result.append(MESFunnelStepItem(
            step_id=sid,
            step_type=stype,
            position=pos,
            branch=branch or "main",
            avg_reach=round(avg_r or 0, 2),
            avg_reengagement=round(avg_re or 0, 2),
            avg_combined=round(avg_c or 0, 2),
            sample_size=cnt,
            grade_distribution=gd,
        ))
    return result


# ============================================================================
# Configuration
# ============================================================================

@router.get(
    "/projects/{project_id}/mes/config",
    response_model=MESConfigResponse,
)
def get_mes_config(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    config = db.query(MESConfig).filter(MESConfig.project_id == project_id).first()
    if not config:
        # Return defaults
        return MESConfigResponse(
            project_id=project_id,
            enabled=True,
            attribution_window_hours=24,
            pre_session_window_min=15,
            reach_weight=0.5,
            reengagement_weight=0.5,
            grade_thresholds={"high": 70, "medium": 40, "low": 0},
            reengagement_event_weights={"editor_": 15, "page_view": 10, "auth_login": 8, "gtm_": 12, "trial_": 10},
            exclude_event_patterns=["channel.*", "contact.*", "segment_*", "funnel_", "scoring_"],
            speed_decay_rate=0.08,
            event_decay_rate=0.1,
        )
    return config


@router.put(
    "/projects/{project_id}/mes/config",
    response_model=MESConfigResponse,
)
def update_mes_config(
    project_id: int,
    data: MESConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    config = db.query(MESConfig).filter(MESConfig.project_id == project_id).first()
    if not config:
        config = MESConfig(project_id=project_id)
        db.add(config)

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(config, key, value)

    db.commit()
    db.refresh(config)
    return config


# ============================================================================
# Manual recompute
# ============================================================================

@router.post("/projects/{project_id}/mes/recompute")
def trigger_recompute(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    updated = db.query(MessageEffectivenessScore).filter(
        MessageEffectivenessScore.project_id == project_id,
    ).update({"stale": True}, synchronize_session=False)
    db.commit()

    return {"message": f"Marked {updated} scores for recomputation"}


@router.post("/projects/{project_id}/mes/backfill")
def backfill_scores(
    project_id: int,
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create MES records for historical SendLogs (up to `days` back) and mark all stale.

    Unlike the scheduler (which only looks back 7 days), this creates MES rows
    for older messages so they can be scored with the current algorithm.
    """
    from datetime import timedelta
    from sqlalchemy.orm import aliased

    _get_project(db, project_id, current_user)

    cutoff = datetime.utcnow() - timedelta(days=days)
    mes_alias = aliased(MessageEffectivenessScore)

    missing = (
        db.query(SendLog)
        .outerjoin(mes_alias, mes_alias.send_log_id == SendLog.id)
        .filter(
            SendLog.project_id == project_id,
            SendLog.status.notin_(["queued"]),
            SendLog.queued_at >= cutoff,
            mes_alias.id.is_(None),
        )
        .all()
    )

    created = 0
    for sl in missing:
        try:
            mes = MessageEffectivenessScore(
                project_id=sl.project_id,
                send_log_id=sl.id,
                user_id=sl.user_id,
                channel=sl.channel or sl.resolved_channel or "unknown",
                template_id=sl.template_id,
                source_type=sl.source_type or "unknown",
                source_id=sl.source_id,
                stale=True,
            )
            db.add(mes)
            created += 1
        except Exception:
            pass

    # Also mark existing records as stale
    updated = db.query(MessageEffectivenessScore).filter(
        MessageEffectivenessScore.project_id == project_id,
    ).update({"stale": True}, synchronize_session=False)

    db.commit()

    return {
        "message": f"Created {created} new MES records, marked {updated} existing as stale",
        "created": created,
        "marked_stale": updated,
    }
