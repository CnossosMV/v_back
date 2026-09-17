"""
Authorization Service

Role hierarchy: owner > admin > editor > support_agent > viewer
"""

from typing import List, Optional
from sqlalchemy.orm import Session
from app.models import ProjectMember, Project, Workspace, User

ROLE_HIERARCHY = {
    "owner": 50,
    "admin": 40,
    "editor": 30,
    "support_agent": 20,
    "viewer": 10,
}


def check_project_access(
    db: Session,
    user_id: int,
    project_id: int,
    min_role: str = "viewer",
) -> Optional[ProjectMember]:
    """Check if user has at least min_role on project. Returns membership or None."""
    member = (
        db.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            ProjectMember.is_active == True,
        )
        .first()
    )
    if not member:
        return None

    min_level = ROLE_HIERARCHY.get(min_role, 0)
    user_level = ROLE_HIERARCHY.get(member.role, 0)
    if user_level < min_level:
        return None

    return member


def get_user_projects(
    db: Session,
    user_id: int,
    role_filter: Optional[str] = None,
) -> List[dict]:
    """Return projects accessible to user, with their role."""
    query = (
        db.query(ProjectMember, Project)
        .join(Project, ProjectMember.project_id == Project.id)
        .filter(
            ProjectMember.user_id == user_id,
            ProjectMember.is_active == True,
            Project.is_active == True,
        )
    )
    if role_filter:
        query = query.filter(ProjectMember.role == role_filter)

    results = query.all()
    return [
        {
            "project": member_project,
            "role": membership.role,
            "member_id": membership.id,
        }
        for membership, member_project in results
    ]


def is_workspace_admin(db: Session, user: User) -> bool:
    """Check if user is workspace owner."""
    if not user.workspace_id:
        return False
    workspace = db.query(Workspace).filter(Workspace.id == user.workspace_id).first()
    if not workspace:
        return False
    return workspace.owner_id == user.id


def get_project_agents(
    db: Session,
    project_id: int,
) -> List[ProjectMember]:
    """Return members with support_agent role or higher."""
    min_level = ROLE_HIERARCHY["support_agent"]
    members = (
        db.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.is_active == True,
        )
        .all()
    )
    return [m for m in members if ROLE_HIERARCHY.get(m.role, 0) >= min_level]
