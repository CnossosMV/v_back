from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Project
from app.models.messaging import WorkspaceVerificationEntitlement


class VerificationAccessError(ValueError):
    pass


@dataclass(frozen=True)
class VerificationAuthorization:
    workspace_id: int
    access_mode: str
    billing_disposition: str


class VerificationAccessService:
    """Single commercial-policy seam used before every provider operation."""

    def __init__(self, db: Session):
        self.db = db

    def authorize(self, project_id: int) -> VerificationAuthorization:
        project = self.db.query(Project).filter(Project.id == project_id, Project.is_active == True).first()
        if not project:
            raise VerificationAccessError("Project not found")
        entitlement = self.db.query(WorkspaceVerificationEntitlement).filter(
            WorkspaceVerificationEntitlement.workspace_id == project.workspace_id,
            WorkspaceVerificationEntitlement.is_active == True,
        ).first()
        if not entitlement or entitlement.access_mode == "disabled":
            raise VerificationAccessError("Contact verification is not enabled for this workspace")
        if entitlement.valid_until and entitlement.valid_until <= datetime.utcnow():
            raise VerificationAccessError("Contact verification entitlement has expired")
        return VerificationAuthorization(
            workspace_id=project.workspace_id,
            access_mode=entitlement.access_mode,
            billing_disposition="waived" if entitlement.access_mode == "unmetered" else "reserved",
        )

    def reserve(self, project_id: int, units: int) -> Optional[str]:
        auth = self.authorize(project_id)
        if auth.access_mode == "credits":
            raise VerificationAccessError("Credit-backed verification is not available yet")
        return None

    def settle(self, reservation_id: Optional[str], provider_units: int) -> None:
        return None

    def release(self, reservation_id: Optional[str]) -> None:
        return None
