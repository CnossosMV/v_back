"""
LLM Usage Router

Endpoints for querying project-level LLM usage data.
"""
from datetime import datetime, date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, cast, Date
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import LLMUsageRecord
from app.routers.auth import get_current_user
from app.dependencies import require_project_role
from app.schemas.llm_usage import (
    UsageSummaryResponse,
    UsageByGroup,
    DailyUsageResponse,
    DailyUsagePoint,
)

router = APIRouter(tags=["llm_usage"])


@router.get(
    "/projects/{project_id}/llm-usage/summary",
    response_model=UsageSummaryResponse,
)
def get_usage_summary(
    project_id: int,
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    """Get aggregated LLM usage summary for a project."""
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today() + timedelta(days=1)

    base_q = db.query(LLMUsageRecord).filter(
        LLMUsageRecord.project_id == project_id,
        LLMUsageRecord.created_at >= datetime.combine(start_date, datetime.min.time()),
        LLMUsageRecord.created_at < datetime.combine(end_date, datetime.min.time()),
    )

    # Totals
    totals = base_q.with_entities(
        func.coalesce(func.sum(LLMUsageRecord.input_tokens), 0),
        func.coalesce(func.sum(LLMUsageRecord.output_tokens), 0),
        func.coalesce(func.sum(LLMUsageRecord.total_tokens), 0),
        func.coalesce(func.sum(LLMUsageRecord.cost_estimate), 0.0),
        func.coalesce(func.sum(LLMUsageRecord.call_count), 0),
    ).first()

    def _group_by(column):
        rows = base_q.with_entities(
            column,
            func.sum(LLMUsageRecord.input_tokens),
            func.sum(LLMUsageRecord.output_tokens),
            func.sum(LLMUsageRecord.total_tokens),
            func.sum(LLMUsageRecord.cost_estimate),
            func.sum(LLMUsageRecord.call_count),
        ).group_by(column).all()
        return [
            UsageByGroup(
                group=r[0], input_tokens=r[1] or 0, output_tokens=r[2] or 0,
                total_tokens=r[3] or 0, cost_estimate=r[4] or 0.0, call_count=r[5] or 0,
            )
            for r in rows
        ]

    return UsageSummaryResponse(
        total_input_tokens=totals[0],
        total_output_tokens=totals[1],
        total_tokens=totals[2],
        total_cost=totals[3],
        total_calls=totals[4],
        by_provider=_group_by(LLMUsageRecord.provider),
        by_model=_group_by(LLMUsageRecord.model),
        by_purpose=_group_by(LLMUsageRecord.purpose),
        by_key_source=_group_by(LLMUsageRecord.key_source),
    )


@router.get(
    "/projects/{project_id}/llm-usage/daily",
    response_model=DailyUsageResponse,
)
def get_daily_usage(
    project_id: int,
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    """Get daily LLM usage breakdown for charts."""
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today() + timedelta(days=1)

    rows = (
        db.query(
            cast(LLMUsageRecord.created_at, Date).label("day"),
            func.sum(LLMUsageRecord.total_tokens),
            func.sum(LLMUsageRecord.cost_estimate),
            func.sum(LLMUsageRecord.call_count),
        )
        .filter(
            LLMUsageRecord.project_id == project_id,
            LLMUsageRecord.created_at >= datetime.combine(start_date, datetime.min.time()),
            LLMUsageRecord.created_at < datetime.combine(end_date, datetime.min.time()),
        )
        .group_by("day")
        .order_by("day")
        .all()
    )

    return DailyUsageResponse(
        days=[
            DailyUsagePoint(
                date=r[0], total_tokens=r[1] or 0,
                cost_estimate=r[2] or 0.0, call_count=r[3] or 0,
            )
            for r in rows
        ]
    )
