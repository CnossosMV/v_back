"""Project-scoped resolution for explicit email sender instances."""

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import EmailInstance, Project


def resolve_email_instance(
    db: Session,
    *,
    project_id: int,
    instance_id: int,
    require_ready: bool = True,
) -> EmailInstance | None:
    """Resolve a project sender without allowing cross-tenant fallback."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return None
    query = db.query(EmailInstance).filter(
        EmailInstance.id == instance_id,
        EmailInstance.workspace_id == project.workspace_id,
        or_(
            EmailInstance.project_id == project_id,
            and_(EmailInstance.project_id.is_(None), EmailInstance.workspace_id == project.workspace_id),
        ),
    )
    if require_ready:
        query = query.filter(
            EmailInstance.is_active == True,  # noqa: E712
            EmailInstance.connection_status == "verified",
        )
    return query.first()
