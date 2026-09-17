"""
Team Analytics Router

Endpoints for retrieving team analytics, history, specialist breakdowns,
routing metrics, and cost estimates.
"""

from typing import List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AgentTeam
from app.schemas.agent_teams import (
    AnalyticsSnapshotResponse,
    SpecialistBreakdownResponse,
    RoutingMetricsResponse,
    CostBreakdownResponse,
)
from app.routers.auth import get_current_user
from app.services.chatbot.team_analytics_service import TeamAnalyticsService

router = APIRouter(tags=["team-analytics"])


def _get_team_or_404(team_id: int, db: Session) -> AgentTeam:
    team = db.query(AgentTeam).filter(AgentTeam.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")
    return team


@router.get(
    "/agent-teams/{team_id}/analytics",
    response_model=AnalyticsSnapshotResponse,
)
async def get_analytics(
    team_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get current analytics for a team (auto-computes if stale)."""
    _get_team_or_404(team_id, db)
    service = TeamAnalyticsService(db)
    snapshot = service.get_or_compute_latest(team_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="No analytics available")
    return snapshot


@router.get(
    "/agent-teams/{team_id}/analytics/history",
    response_model=List[AnalyticsSnapshotResponse],
)
async def get_analytics_history(
    team_id: int,
    days: int = Query(30, le=365, description="Number of days"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get historical analytics snapshots."""
    _get_team_or_404(team_id, db)
    service = TeamAnalyticsService(db)
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    return service.get_history(team_id, start, end)


@router.get(
    "/agent-teams/{team_id}/analytics/specialists",
    response_model=List[SpecialistBreakdownResponse],
)
async def get_specialist_breakdown(
    team_id: int,
    days: int = Query(30, le=365),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get per-specialist analytics breakdown."""
    _get_team_or_404(team_id, db)
    service = TeamAnalyticsService(db)
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    return service.get_specialist_breakdown(team_id, start, end)


@router.get(
    "/agent-teams/{team_id}/analytics/routing",
    response_model=RoutingMetricsResponse,
)
async def get_routing_metrics(
    team_id: int,
    days: int = Query(30, le=365),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get routing accuracy metrics."""
    _get_team_or_404(team_id, db)
    service = TeamAnalyticsService(db)
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    return service.get_routing_metrics(team_id, start, end)


@router.get(
    "/agent-teams/{team_id}/analytics/costs",
    response_model=CostBreakdownResponse,
)
async def get_cost_breakdown(
    team_id: int,
    days: int = Query(30, le=365),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get cost breakdown for a team."""
    _get_team_or_404(team_id, db)
    service = TeamAnalyticsService(db)
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    return service.get_cost_breakdown(team_id, start, end)
