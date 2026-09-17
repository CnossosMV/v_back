"""Deduplicated in-app and administrator email operational alerts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Project, ProjectMember, User, Workspace
from app.models.campaigns import OperationalAlert


class OperationalAlertService:
    def __init__(self, db: Session):
        self.db = db

    def open_or_touch(
        self,
        *,
        project_id: int,
        alert_type: str,
        dedupe_key: str,
        title: str,
        message: str,
        severity: str = "warning",
        context: Optional[dict[str, Any]] = None,
        notify_admins: bool = True,
    ) -> tuple[OperationalAlert, bool]:
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project {project_id} not found")
        alert = self.db.query(OperationalAlert).filter(
            OperationalAlert.workspace_id == project.workspace_id,
            OperationalAlert.dedupe_key == dedupe_key,
        ).first()
        created = alert is None
        now = datetime.utcnow()
        if alert is None:
            alert = OperationalAlert(
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
            self.db.add(alert)
        else:
            alert.last_seen_at = now
            alert.title = title
            alert.message = message
            alert.context = context
            if alert.status == "resolved":
                alert.status = "open"
                alert.resolved_at = None
                alert.resolved_by_user_id = None
                created = True
        self.db.commit()
        self.db.refresh(alert)
        if created and notify_admins:
            self._notify_admins(project, alert)
        return alert, created

    def resolve(self, project_id: int, dedupe_key: str, user_id: Optional[int] = None) -> Optional[OperationalAlert]:
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return None
        alert = self.db.query(OperationalAlert).filter(
            OperationalAlert.workspace_id == project.workspace_id,
            OperationalAlert.dedupe_key == dedupe_key,
        ).first()
        if not alert:
            return None
        alert.status = "resolved"
        alert.resolved_at = datetime.utcnow()
        alert.resolved_by_user_id = user_id
        self.db.commit()
        return alert

    def acknowledge(self, project_id: int, alert_id: int, user_id: int) -> OperationalAlert:
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError("Project not found")
        alert = self.db.query(OperationalAlert).filter(
            OperationalAlert.id == alert_id,
            OperationalAlert.project_id == project_id,
            OperationalAlert.workspace_id == project.workspace_id,
        ).first()
        if not alert:
            raise ValueError("Operational alert not found")
        alert.status = "acknowledged"
        alert.acknowledged_at = datetime.utcnow()
        alert.acknowledged_by_user_id = user_id
        self.db.commit()
        self.db.refresh(alert)
        return alert

    def _notify_admins(self, project: Project, alert: OperationalAlert) -> None:
        recipients = self._admin_emails(project)
        if not recipients:
            return
        from app.services.email_service import EmailService

        service = EmailService()
        subject = f"[Versya] {alert.title}"
        body = (
            f"<h2>{alert.title}</h2>"
            f"<p>{alert.message or ''}</p>"
            f"<p>Project / Projeto: {project.name}</p>"
            "<p>Open Versya to review and acknowledge this alert. "
            "Abra o Versya para revisar e reconhecer este alerta.</p>"
        )
        for recipient in recipients:
            service.send_html_email(to_email=recipient, subject=subject, html_body=body)

    def _admin_emails(self, project: Project) -> list[str]:
        workspace = self.db.query(Workspace).filter(Workspace.id == project.workspace_id).first()
        user_ids: set[int] = set()
        if workspace and workspace.owner_id:
            user_ids.add(workspace.owner_id)
        user_ids.update(
            row[0]
            for row in self.db.query(ProjectMember.user_id).filter(
                ProjectMember.project_id == project.id,
                ProjectMember.is_active == True,  # noqa: E712
                ProjectMember.role.in_(["admin", "owner"]),
            ).all()
        )
        if not user_ids:
            return []
        return sorted({
            row[0]
            for row in self.db.query(User.email).filter(
                User.id.in_(user_ids),
                User.is_active == True,  # noqa: E712
                User.email.isnot(None),
            ).all()
            if row[0]
        })
