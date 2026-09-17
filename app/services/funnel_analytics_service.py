"""Analytics service for funnel enrollment metrics."""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_

from app.models import Funnel, FunnelStep, FunnelEnrollment, FunnelEnrollmentLog, Project
from app.models.messaging import MessagingUser

logger = logging.getLogger(__name__)


class FunnelAnalyticsService:
    def __init__(self, db: Session):
        self.db = db

    def _verify_access(self, funnel_id: int, workspace_id: int) -> Funnel:
        funnel = (
            self.db.query(Funnel)
            .join(Project)
            .filter(
                Funnel.id == funnel_id,
                Project.workspace_id == workspace_id,
                Project.is_active == True,
            )
            .first()
        )
        if not funnel:
            from fastapi import HTTPException, status
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Funnel not found")
        return funnel

    def _non_sandbox_ids(self, funnel_id: int):
        """Return a subquery of enrollment IDs excluding sandbox contacts."""
        return (
            self.db.query(FunnelEnrollment.id)
            .join(MessagingUser, FunnelEnrollment.user_id == MessagingUser.id)
            .filter(
                FunnelEnrollment.funnel_id == funnel_id,
                MessagingUser.is_sandbox == False,
            )
            .subquery()
        )

    def enrollment_stats(self, funnel_id: int, workspace_id: int) -> Dict[str, Any]:
        """Aggregate enrollment counts and average completion time."""
        self._verify_access(funnel_id, workspace_id)

        ns = self._non_sandbox_ids(funnel_id)

        total = self.db.query(func.count(FunnelEnrollment.id)).filter(
            FunnelEnrollment.funnel_id == funnel_id,
            FunnelEnrollment.id.in_(ns),
        ).scalar() or 0

        active = self.db.query(func.count(FunnelEnrollment.id)).filter(
            FunnelEnrollment.funnel_id == funnel_id,
            FunnelEnrollment.status == "active",
            FunnelEnrollment.id.in_(ns),
        ).scalar() or 0

        completed = self.db.query(func.count(FunnelEnrollment.id)).filter(
            FunnelEnrollment.funnel_id == funnel_id,
            FunnelEnrollment.status == "completed",
            FunnelEnrollment.id.in_(ns),
        ).scalar() or 0

        exited = self.db.query(func.count(FunnelEnrollment.id)).filter(
            FunnelEnrollment.funnel_id == funnel_id,
            FunnelEnrollment.status == "exited",
            FunnelEnrollment.id.in_(ns),
        ).scalar() or 0

        # Exited by reason breakdown
        exit_reasons = (
            self.db.query(
                FunnelEnrollment.exit_reason,
                func.count(FunnelEnrollment.id),
            )
            .filter(
                FunnelEnrollment.funnel_id == funnel_id,
                FunnelEnrollment.status == "exited",
                FunnelEnrollment.exit_reason.isnot(None),
                FunnelEnrollment.id.in_(ns),
            )
            .group_by(FunnelEnrollment.exit_reason)
            .all()
        )
        exited_by_reason = {r: c for r, c in exit_reasons}

        # Average completion hours
        avg_hours_row = (
            self.db.query(
                func.avg(
                    func.extract('epoch', FunnelEnrollment.exited_at - FunnelEnrollment.enrolled_at) / 3600
                )
            )
            .filter(
                FunnelEnrollment.funnel_id == funnel_id,
                FunnelEnrollment.status == "completed",
                FunnelEnrollment.exited_at.isnot(None),
                FunnelEnrollment.id.in_(ns),
            )
            .scalar()
        )
        avg_completion_hours = round(float(avg_hours_row), 2) if avg_hours_row else None

        return {
            "total": total,
            "active": active,
            "completed": completed,
            "exited": exited,
            "exited_by_reason": exited_by_reason,
            "avg_completion_hours": avg_completion_hours,
        }

    def step_metrics(self, funnel_id: int, workspace_id: int) -> List[Dict[str, Any]]:
        """Per-step metrics: users entered, completed, avg time, drop-off."""
        self._verify_access(funnel_id, workspace_id)

        steps = (
            self.db.query(FunnelStep)
            .filter(FunnelStep.funnel_id == funnel_id, FunnelStep.branch == "main", FunnelStep.parent_step_id.is_(None))
            .order_by(FunnelStep.position)
            .all()
        )

        results = []
        for step in steps:
            entered = self.db.query(func.count(FunnelEnrollmentLog.id)).filter(
                FunnelEnrollmentLog.step_id == step.id,
                FunnelEnrollmentLog.action == "entered",
            ).scalar() or 0

            completed_actions = self.db.query(func.count(FunnelEnrollmentLog.id)).filter(
                FunnelEnrollmentLog.step_id == step.id,
                FunnelEnrollmentLog.action.in_(["advanced", "action_executed", "exited"]),
            ).scalar() or 0

            # Average time in step (seconds) — diff between entered and next log
            avg_time = None
            if entered > 0:
                # Approximate: count advanced logs and compute average
                from sqlalchemy import text
                avg_q = self.db.execute(
                    text("""
                        SELECT AVG(EXTRACT(EPOCH FROM (adv.created_at - ent.created_at)))
                        FROM funnel_enrollment_logs ent
                        JOIN funnel_enrollment_logs adv ON adv.enrollment_id = ent.enrollment_id
                            AND adv.id > ent.id
                            AND adv.action IN ('advanced', 'action_executed', 'exited')
                        WHERE ent.step_id = :step_id AND ent.action = 'entered'
                        AND adv.created_at > ent.created_at
                    """),
                    {"step_id": step.id},
                ).scalar()
                if avg_q:
                    avg_time = round(float(avg_q), 1)

            drop_off_rate = round(1 - (completed_actions / entered), 4) if entered > 0 else 0

            results.append({
                "step_id": step.id,
                "step_type": step.step_type,
                "position": step.position,
                "users_entered": entered,
                "users_completed": completed_actions,
                "avg_time_seconds": avg_time,
                "drop_off_rate": drop_off_rate,
            })

        return results

    def conversion_funnel(self, funnel_id: int, workspace_id: int) -> List[Dict[str, Any]]:
        """Cumulative drop-off through main-flow steps."""
        self._verify_access(funnel_id, workspace_id)

        steps = (
            self.db.query(FunnelStep)
            .filter(FunnelStep.funnel_id == funnel_id, FunnelStep.branch == "main", FunnelStep.parent_step_id.is_(None))
            .order_by(FunnelStep.position)
            .all()
        )

        ns = self._non_sandbox_ids(funnel_id)
        total_enrollments = self.db.query(func.count(FunnelEnrollment.id)).filter(
            FunnelEnrollment.funnel_id == funnel_id,
            FunnelEnrollment.id.in_(ns),
        ).scalar() or 0

        results = []
        for step in steps:
            entered = self.db.query(func.count(FunnelEnrollmentLog.id)).filter(
                FunnelEnrollmentLog.step_id == step.id,
                FunnelEnrollmentLog.action == "entered",
            ).scalar() or 0

            pct = round((entered / total_enrollments) * 100, 1) if total_enrollments > 0 else 0

            step_name = f"{step.step_type} (pos {step.position})"
            cfg = step.step_config or {}
            if step.step_type == "wait" and cfg.get("duration"):
                step_name = f"Wait {cfg['duration']} {cfg.get('unit', 'hours')}"
            elif step.step_type == "action" and cfg.get("action_type"):
                step_name = f"Action: {cfg['action_type']}"
            elif step.step_type == "condition":
                step_name = "Condition"
            elif step.step_type == "exit":
                step_name = "Exit"

            results.append({
                "step_name": step_name,
                "position": step.position,
                "entered": entered,
                "pct": pct,
            })

        return results

    def enrollment_trend(self, funnel_id: int, workspace_id: int, days: int = 30) -> List[Dict[str, Any]]:
        """Daily enrollment, completion, and exit counts."""
        self._verify_access(funnel_id, workspace_id)

        start_date = datetime.utcnow() - timedelta(days=days)
        ns = self._non_sandbox_ids(funnel_id)

        # Enrolled per day
        enrolled_rows = (
            self.db.query(
                func.date(FunnelEnrollment.enrolled_at).label("date"),
                func.count(FunnelEnrollment.id).label("count"),
            )
            .filter(
                FunnelEnrollment.funnel_id == funnel_id,
                FunnelEnrollment.enrolled_at >= start_date,
                FunnelEnrollment.id.in_(ns),
            )
            .group_by(func.date(FunnelEnrollment.enrolled_at))
            .all()
        )

        # Completed per day
        completed_rows = (
            self.db.query(
                func.date(FunnelEnrollment.exited_at).label("date"),
                func.count(FunnelEnrollment.id).label("count"),
            )
            .filter(
                FunnelEnrollment.funnel_id == funnel_id,
                FunnelEnrollment.status == "completed",
                FunnelEnrollment.exited_at >= start_date,
                FunnelEnrollment.id.in_(ns),
            )
            .group_by(func.date(FunnelEnrollment.exited_at))
            .all()
        )

        # Exited per day
        exited_rows = (
            self.db.query(
                func.date(FunnelEnrollment.exited_at).label("date"),
                func.count(FunnelEnrollment.id).label("count"),
            )
            .filter(
                FunnelEnrollment.funnel_id == funnel_id,
                FunnelEnrollment.status == "exited",
                FunnelEnrollment.exited_at >= start_date,
                FunnelEnrollment.id.in_(ns),
            )
            .group_by(func.date(FunnelEnrollment.exited_at))
            .all()
        )

        # Merge into date-keyed dict
        dates: Dict[str, Dict[str, int]] = {}
        for row in enrolled_rows:
            d = str(row.date)
            dates.setdefault(d, {"enrolled": 0, "completed": 0, "exited": 0})
            dates[d]["enrolled"] = row.count

        for row in completed_rows:
            d = str(row.date)
            dates.setdefault(d, {"enrolled": 0, "completed": 0, "exited": 0})
            dates[d]["completed"] = row.count

        for row in exited_rows:
            d = str(row.date)
            dates.setdefault(d, {"enrolled": 0, "completed": 0, "exited": 0})
            dates[d]["exited"] = row.count

        return sorted(
            [{"date": d, **v} for d, v in dates.items()],
            key=lambda x: x["date"],
        )
