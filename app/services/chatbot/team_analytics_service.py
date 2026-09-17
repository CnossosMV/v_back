"""
Team Analytics Service

Computes and caches analytics snapshots for agent teams.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, case

from app.models import (
    TeamAnalyticsSnapshot, AgentTeam, ChatSession, ChatMessage,
    TeamSessionEvent, ToolExecution, SpecialistAgent,
)

logger = logging.getLogger(__name__)

# Approximate token pricing (USD per 1k tokens)
MODEL_PRICING = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
}
DEFAULT_PRICING = {"input": 0.001, "output": 0.002}


class TeamAnalyticsService:
    """Service for computing and retrieving team analytics."""

    def __init__(self, db: Session):
        self.db = db

    # ========================================================================
    # Snapshot retrieval
    # ========================================================================

    def get_or_compute_latest(self, team_id: int) -> Optional[TeamAnalyticsSnapshot]:
        """
        Get cached latest snapshot or compute a fresh one.
        Re-computes if snapshot is older than 1 hour.
        """
        now = datetime.utcnow()
        latest = (
            self.db.query(TeamAnalyticsSnapshot)
            .filter(TeamAnalyticsSnapshot.team_id == team_id)
            .order_by(TeamAnalyticsSnapshot.period_end.desc())
            .first()
        )

        if latest and (now - latest.period_end).total_seconds() < 3600:
            return latest

        # Compute fresh snapshot for last 30 days
        period_start = now - timedelta(days=30)
        return self.compute_analytics(team_id, period_start, now)

    def get_history(
        self,
        team_id: int,
        start: datetime,
        end: datetime,
        granularity: str = "daily",
    ) -> List[TeamAnalyticsSnapshot]:
        """Get historical snapshots within a date range."""
        return (
            self.db.query(TeamAnalyticsSnapshot)
            .filter(
                TeamAnalyticsSnapshot.team_id == team_id,
                TeamAnalyticsSnapshot.period_start >= start,
                TeamAnalyticsSnapshot.period_end <= end,
            )
            .order_by(TeamAnalyticsSnapshot.period_start.asc())
            .all()
        )

    # ========================================================================
    # Compute analytics
    # ========================================================================

    def compute_analytics(
        self,
        team_id: int,
        period_start: datetime,
        period_end: datetime,
    ) -> TeamAnalyticsSnapshot:
        """Compute an analytics snapshot for the given period."""

        # Session metrics
        session_stats = self._compute_session_stats(team_id, period_start, period_end)

        # Specialist distribution
        specialist_dist = self._compute_specialist_distribution(team_id, period_start, period_end)

        # Routing accuracy
        routing_accuracy = self._compute_routing_accuracy(team_id, period_start, period_end)

        # Token / cost estimates
        cost_data = self._compute_cost_estimate(team_id, period_start, period_end)

        # Tool metrics
        tool_data = self._compute_tool_metrics(team_id, period_start, period_end)

        # Response time
        avg_response_time = self._compute_avg_response_time(team_id, period_start, period_end)

        snapshot = TeamAnalyticsSnapshot(
            team_id=team_id,
            period_start=period_start,
            period_end=period_end,
            total_sessions=session_stats["total"],
            resolved_sessions=session_stats["resolved"],
            escalated_sessions=session_stats["escalated"],
            avg_turns_to_resolve=session_stats["avg_turns"],
            specialist_distribution=specialist_dist,
            routing_accuracy=routing_accuracy,
            total_tokens=cost_data["total_tokens"],
            estimated_cost=cost_data["estimated_cost"],
            tool_call_count=tool_data["call_count"],
            tool_success_rate=tool_data["success_rate"],
            avg_response_time_ms=avg_response_time,
        )

        self.db.add(snapshot)
        self.db.commit()
        self.db.refresh(snapshot)
        return snapshot

    # ========================================================================
    # Detailed breakdowns
    # ========================================================================

    def get_specialist_breakdown(
        self,
        team_id: int,
        period_start: datetime,
        period_end: datetime,
    ) -> List[Dict]:
        """Per-specialist session count, resolution rate, avg turns."""
        specialists = (
            self.db.query(SpecialistAgent)
            .filter(SpecialistAgent.team_id == team_id)
            .all()
        )

        result = []
        for spec in specialists:
            # Count sessions that had this specialist as current_agent
            sessions = (
                self.db.query(ChatSession)
                .filter(
                    ChatSession.team_id == team_id,
                    ChatSession.current_agent_id == spec.id,
                    ChatSession.created_at.between(period_start, period_end),
                )
                .all()
            )

            total = len(sessions)
            resolved = sum(1 for s in sessions if s.session_state == "RESOLVED")
            escalated = sum(1 for s in sessions if s.session_state == "ESCALATED")

            # Count messages by this specialist
            message_count = (
                self.db.query(func.count(ChatMessage.id))
                .filter(
                    ChatMessage.agent_id == spec.id,
                    ChatMessage.timestamp.between(period_start, period_end),
                )
                .scalar() or 0
            )

            result.append({
                "specialist_id": spec.id,
                "name": spec.name,
                "icon": spec.icon,
                "total_sessions": total,
                "resolved_sessions": resolved,
                "escalated_sessions": escalated,
                "resolution_rate": (resolved / total) if total > 0 else None,
                "message_count": message_count,
            })

        return result

    def get_routing_metrics(
        self,
        team_id: int,
        period_start: datetime,
        period_end: datetime,
    ) -> Dict:
        """Routing accuracy: sessions with no re-routing / total sessions."""
        sessions = (
            self.db.query(ChatSession)
            .filter(
                ChatSession.team_id == team_id,
                ChatSession.created_at.between(period_start, period_end),
            )
            .all()
        )

        total = len(sessions)
        if total == 0:
            return {"total_sessions": 0, "single_agent_sessions": 0, "accuracy": None}

        # Count sessions where agent never switched (no agent_switch events)
        single_agent = 0
        for session in sessions:
            switch_count = (
                self.db.query(func.count(TeamSessionEvent.id))
                .filter(
                    TeamSessionEvent.session_id == session.id,
                    TeamSessionEvent.event_type == "agent_switch",
                )
                .scalar() or 0
            )
            if switch_count == 0:
                single_agent += 1

        return {
            "total_sessions": total,
            "single_agent_sessions": single_agent,
            "accuracy": single_agent / total if total > 0 else None,
        }

    def get_cost_breakdown(
        self,
        team_id: int,
        period_start: datetime,
        period_end: datetime,
    ) -> Dict:
        """Token and cost breakdown."""
        return self._compute_cost_estimate(team_id, period_start, period_end)

    # ========================================================================
    # Internal computation helpers
    # ========================================================================

    def _compute_session_stats(
        self, team_id: int, start: datetime, end: datetime
    ) -> Dict:
        sessions = (
            self.db.query(ChatSession)
            .filter(
                ChatSession.team_id == team_id,
                ChatSession.created_at.between(start, end),
            )
            .all()
        )

        total = len(sessions)
        resolved = sum(1 for s in sessions if s.session_state == "RESOLVED")
        escalated = sum(1 for s in sessions if s.session_state == "ESCALATED")

        # Avg turns for resolved sessions
        turns_list = [s.message_count for s in sessions if s.session_state == "RESOLVED" and s.message_count]
        avg_turns = (sum(turns_list) / len(turns_list)) if turns_list else None

        return {
            "total": total,
            "resolved": resolved,
            "escalated": escalated,
            "avg_turns": avg_turns,
        }

    def _compute_specialist_distribution(
        self, team_id: int, start: datetime, end: datetime
    ) -> Dict:
        """Return {agent_id: session_count} distribution."""
        rows = (
            self.db.query(
                ChatSession.current_agent_id,
                func.count(ChatSession.id),
            )
            .filter(
                ChatSession.team_id == team_id,
                ChatSession.created_at.between(start, end),
                ChatSession.current_agent_id.isnot(None),
            )
            .group_by(ChatSession.current_agent_id)
            .all()
        )
        return {str(agent_id): count for agent_id, count in rows}

    def _compute_routing_accuracy(
        self, team_id: int, start: datetime, end: datetime
    ) -> Optional[float]:
        metrics = self.get_routing_metrics(team_id, start, end)
        return metrics["accuracy"]

    def _compute_cost_estimate(
        self, team_id: int, start: datetime, end: datetime
    ) -> Dict:
        """Estimate cost from message metadata tokens."""
        # Get messages for this team's sessions
        messages = (
            self.db.query(ChatMessage)
            .join(ChatSession, ChatMessage.session_id == ChatSession.id)
            .filter(
                ChatSession.team_id == team_id,
                ChatMessage.timestamp.between(start, end),
            )
            .all()
        )

        total_tokens = 0
        estimated_cost = 0.0

        for msg in messages:
            decision = msg.routing_decision or {}
            tokens = decision.get("tokens", {})
            input_tokens = tokens.get("input", 0)
            output_tokens = tokens.get("output", 0)
            model = decision.get("model", "gpt-4o-mini")

            total_tokens += input_tokens + output_tokens
            pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
            estimated_cost += (input_tokens / 1000) * pricing["input"]
            estimated_cost += (output_tokens / 1000) * pricing["output"]

        return {
            "total_tokens": total_tokens,
            "estimated_cost": round(estimated_cost, 6),
        }

    def _compute_tool_metrics(
        self, team_id: int, start: datetime, end: datetime
    ) -> Dict:
        """Tool call count and success rate."""
        executions = (
            self.db.query(ToolExecution)
            .join(ChatSession, ToolExecution.session_id == ChatSession.id)
            .filter(
                ChatSession.team_id == team_id,
                ToolExecution.created_at.between(start, end),
            )
            .all()
        )

        call_count = len(executions)
        success_count = sum(1 for e in executions if e.status == "success")

        return {
            "call_count": call_count,
            "success_rate": (success_count / call_count) if call_count > 0 else None,
        }

    def _compute_avg_response_time(
        self, team_id: int, start: datetime, end: datetime
    ) -> Optional[int]:
        """Average response time from session events."""
        events = (
            self.db.query(TeamSessionEvent)
            .join(ChatSession, TeamSessionEvent.session_id == ChatSession.id)
            .filter(
                ChatSession.team_id == team_id,
                TeamSessionEvent.event_type == "message_response",
                TeamSessionEvent.created_at.between(start, end),
            )
            .all()
        )

        times = []
        for evt in events:
            data = evt.event_data or {}
            if "response_time_ms" in data:
                times.append(data["response_time_ms"])

        return int(sum(times) / len(times)) if times else None
