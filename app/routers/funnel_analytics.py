"""REST API router for funnel analytics."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from app.schemas.funnels import (
    EnrollmentStatsResponse, StepMetricsResponse,
    ConversionFunnelResponse, EnrollmentTrendResponse,
)
from app.services.funnel_analytics_service import FunnelAnalyticsService

router = APIRouter(tags=["funnel-analytics"])


def _ws(user: User):
    if not user.workspace_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User must be associated with a workspace")
    return user.workspace_id


@router.get("/funnels/{funnel_id}/analytics/enrollment-stats", response_model=EnrollmentStatsResponse)
def enrollment_stats(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelAnalyticsService(db)
    return svc.enrollment_stats(funnel_id, _ws(current_user))


@router.get("/funnels/{funnel_id}/analytics/step-metrics", response_model=List[StepMetricsResponse])
def step_metrics(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelAnalyticsService(db)
    return svc.step_metrics(funnel_id, _ws(current_user))


@router.get("/funnels/{funnel_id}/analytics/conversion-funnel", response_model=List[ConversionFunnelResponse])
def conversion_funnel(
    funnel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelAnalyticsService(db)
    return svc.conversion_funnel(funnel_id, _ws(current_user))


@router.get("/funnels/{funnel_id}/analytics/enrollment-trend", response_model=List[EnrollmentTrendResponse])
def enrollment_trend(
    funnel_id: int,
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = FunnelAnalyticsService(db)
    return svc.enrollment_trend(funnel_id, _ws(current_user), days)
