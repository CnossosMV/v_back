"""
Per-template engagement analytics.

Mirrors the FunnelAnalyticsService pattern: aggregate SendLog + SendLogClick +
MessageEffectivenessScore by template_id, scoped to a project.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Project
from app.models.messaging import MessagingTemplate


def _get_template_scoped(
    db: Session, template_id: int, workspace_id: int
) -> MessagingTemplate:
    tpl = (
        db.query(MessagingTemplate)
        .join(Project, Project.id == MessagingTemplate.project_id)
        .filter(
            MessagingTemplate.id == template_id,
            Project.workspace_id == workspace_id,
        )
        .first()
    )
    if not tpl:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    return tpl


class TemplateAnalyticsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def engagement_summary(self, template_id: int, workspace_id: int) -> Dict[str, object]:
        tpl = _get_template_scoped(self.db, template_id, workspace_id)
        # Import here to avoid circular deps at module load.
        from app.models import SendLog, MessageEffectivenessScore  # type: ignore

        base = self.db.query(SendLog).filter(
            SendLog.project_id == tpl.project_id,
            SendLog.template_id == template_id,
        )
        sends = base.count()
        delivered = base.filter(SendLog.status.in_(["delivered", "read"])) .count()
        failed = base.filter(SendLog.status == "failed").count()
        opens_total = int(self.db.query(func.coalesce(func.sum(SendLog.open_count), 0)).filter(
            SendLog.project_id == tpl.project_id,
            SendLog.template_id == template_id,
        ).scalar() or 0)
        clicks_total = int(self.db.query(func.coalesce(func.sum(SendLog.click_count), 0)).filter(
            SendLog.project_id == tpl.project_id,
            SendLog.template_id == template_id,
        ).scalar() or 0)
        unique_opens = base.filter(SendLog.opened_at.isnot(None)).count()
        unique_clicks = base.filter(SendLog.first_click_at.isnot(None)).count()

        # Bounces: count delivery_status 'bounced' in DeliveryStatusEvent (simpler: failed rate).
        bounce_rate = round((failed / sends) * 100, 2) if sends else 0.0
        delivery_rate = round((delivered / sends) * 100, 2) if sends else 0.0
        open_rate = round((unique_opens / delivered) * 100, 2) if delivered else 0.0
        click_rate = round((unique_clicks / delivered) * 100, 2) if delivered else 0.0

        avg_reengagement = self.db.query(
            func.avg(MessageEffectivenessScore.reengagement_score)
        ).filter(
            MessageEffectivenessScore.project_id == tpl.project_id,
            MessageEffectivenessScore.template_id == template_id,
        ).scalar()

        return {
            "template_id": template_id,
            "project_id": tpl.project_id,
            "sends": sends,
            "delivered": delivered,
            "failed": failed,
            "opens": opens_total,
            "clicks": clicks_total,
            "unique_opens": unique_opens,
            "unique_clicks": unique_clicks,
            "delivery_rate": delivery_rate,
            "open_rate": open_rate,
            "click_rate": click_rate,
            "bounce_rate": bounce_rate,
            "reengagement_score": round(float(avg_reengagement), 2) if avg_reengagement is not None else None,
        }

    def engagement_trend(
        self, template_id: int, workspace_id: int, days: int = 30
    ) -> List[Dict[str, object]]:
        tpl = _get_template_scoped(self.db, template_id, workspace_id)
        from app.models import SendLog  # type: ignore

        since = datetime.utcnow() - timedelta(days=days)
        day_col = func.date_trunc('day', SendLog.queued_at).label("day")

        rows = (
            self.db.query(
                day_col,
                func.count(SendLog.id).label("sends"),
                func.count(SendLog.id).filter(
                    SendLog.status.in_(["delivered", "read"])
                ).label("delivered"),
                func.coalesce(func.sum(SendLog.open_count), 0).label("opens"),
                func.coalesce(func.sum(SendLog.click_count), 0).label("clicks"),
            )
            .filter(
                SendLog.project_id == tpl.project_id,
                SendLog.template_id == template_id,
                SendLog.queued_at >= since,
            )
            .group_by(day_col)
            .order_by(day_col)
            .all()
        )
        return [
            {
                "date": r.day.isoformat() if hasattr(r.day, "isoformat") else str(r.day),
                "sends": int(r.sends or 0),
                "delivered": int(r.delivered or 0),
                "opens": int(r.opens or 0),
                "clicks": int(r.clicks or 0),
            }
            for r in rows
        ]

    def top_links(
        self, template_id: int, workspace_id: int, limit: int = 10
    ) -> List[Dict[str, object]]:
        tpl = _get_template_scoped(self.db, template_id, workspace_id)
        from app.models import SendLog, SendLogClick  # type: ignore

        rows = (
            self.db.query(
                SendLogClick.original_url,
                func.sum(SendLogClick.click_count).label("clicks"),
                func.count(func.distinct(SendLogClick.send_log_id)).label("unique_sends"),
            )
            .join(SendLog, SendLog.id == SendLogClick.send_log_id)
            .filter(
                SendLog.project_id == tpl.project_id,
                SendLog.template_id == template_id,
            )
            .group_by(SendLogClick.original_url)
            .order_by(func.sum(SendLogClick.click_count).desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "url": r.original_url,
                "clicks": int(r.clicks or 0),
                "unique_sends": int(r.unique_sends or 0),
            }
            for r in rows
        ]
