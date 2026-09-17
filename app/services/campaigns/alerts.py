"""Operational alert upsert used by campaign planning/dispatch."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import Project
from app.models.campaigns import OperationalAlert


def upsert_operational_alert(
    db: Session,
    *,
    project_id: int,
    dedupe_key: str,
    alert_type: str,
    title: str,
    message: str,
    severity: str = "warning",
    context: dict[str, Any] | None = None,
) -> OperationalAlert:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise ValueError("Project not found for operational alert")
    now = datetime.utcnow()
    row = db.query(OperationalAlert).filter(
        OperationalAlert.workspace_id == project.workspace_id,
        OperationalAlert.dedupe_key == dedupe_key,
    ).first()
    if row:
        row.project_id = project_id
        row.alert_type = alert_type
        row.severity = severity
        row.status = "open"
        row.title = title
        row.message = message
        row.context = context
        row.last_seen_at = now
        row.resolved_at = None
        row.resolved_by_user_id = None
    else:
        row = OperationalAlert(
            workspace_id=project.workspace_id,
            project_id=project_id,
            alert_type=alert_type,
            severity=severity,
            status="open",
            dedupe_key=dedupe_key,
            title=title,
            message=message,
            context=context,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(row)
    db.flush()
    return row
