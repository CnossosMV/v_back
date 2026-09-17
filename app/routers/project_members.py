"""
Project Members Router

Manages per-project user membership and role-based access.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.routers.auth import get_current_user
from app.models import ProjectMember, Project, User
from app.dependencies import require_project_role
from app.services.authorization_service import (
    get_user_projects,
    get_project_agents,
    is_workspace_admin,
    ROLE_HIERARCHY,
)
from app.schemas.project_members import (
    ProjectMemberResponse,
    AddMemberRequest,
    UpdateMemberRequest,
    AgentResponse,
    UserProjectResponse,
)

router = APIRouter(tags=["project-members"])

VALID_ROLES = list(ROLE_HIERARCHY.keys())


def _member_to_response(member: ProjectMember) -> ProjectMemberResponse:
    return ProjectMemberResponse(
        id=member.id,
        project_id=member.project_id,
        user_id=member.user_id,
        role=member.role,
        is_active=member.is_active,
        invited_by_id=member.invited_by_id,
        created_at=member.created_at,
        updated_at=member.updated_at,
        user_name=member.user.name if member.user else None,
        user_email=member.user.email if member.user else None,
    )


@router.get("/projects/{project_id}/members", response_model=List[ProjectMemberResponse])
async def list_members(
    project_id: int,
    role: Optional[str] = Query(None),
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    """List all members of a project (admin+)."""
    query = db.query(ProjectMember).filter(ProjectMember.project_id == project_id)
    if role:
        query = query.filter(ProjectMember.role == role)
    members = query.all()
    return [_member_to_response(m) for m in members]


@router.post("/projects/{project_id}/members", response_model=ProjectMemberResponse)
async def add_member(
    project_id: int,
    request: AddMemberRequest,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add a member to a project. Creates user if not exists (admin+)."""
    if request.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Valid: {VALID_ROLES}")

    # Find or create user
    user = db.query(User).filter(User.email == request.email).first()
    if not user:
        inviter_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
        inviter = db.query(User).filter(User.id == inviter_id).first()
        user = User(
            email=request.email,
            name=request.name or request.email.split("@")[0],
            workspace_id=inviter.workspace_id if inviter else None,
            is_active=True,
            is_verified=False,
        )
        db.add(user)
        db.flush()

    # Check if already a member
    existing = (
        db.query(ProjectMember)
        .filter(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id)
        .first()
    )
    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.role = request.role
            db.commit()
            db.refresh(existing)
            return _member_to_response(existing)
        raise HTTPException(status_code=409, detail="User is already a member of this project")

    inviter_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    member = ProjectMember(
        project_id=project_id,
        user_id=user.id,
        role=request.role,
        invited_by_id=inviter_id,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return _member_to_response(member)


@router.patch("/projects/{project_id}/members/{member_id}", response_model=ProjectMemberResponse)
async def update_member(
    project_id: int,
    member_id: int,
    request: UpdateMemberRequest,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    """Update a member's role or active status (admin+)."""
    member = (
        db.query(ProjectMember)
        .filter(ProjectMember.id == member_id, ProjectMember.project_id == project_id)
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    if member.role == "owner":
        raise HTTPException(status_code=403, detail="Cannot modify the project owner")

    if request.role is not None:
        if request.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"Invalid role. Valid: {VALID_ROLES}")
        if request.role == "owner":
            raise HTTPException(status_code=403, detail="Cannot assign owner role")
        member.role = request.role

    if request.is_active is not None:
        member.is_active = request.is_active

    db.commit()
    db.refresh(member)
    return _member_to_response(member)


@router.delete("/projects/{project_id}/members/{member_id}")
async def remove_member(
    project_id: int,
    member_id: int,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    """Remove a member from a project (admin+)."""
    member = (
        db.query(ProjectMember)
        .filter(ProjectMember.id == member_id, ProjectMember.project_id == project_id)
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    if member.role == "owner":
        raise HTTPException(status_code=403, detail="Cannot remove the project owner")

    db.delete(member)
    db.commit()
    return {"detail": "Member removed"}


@router.get("/projects/{project_id}/members/agents", response_model=List[AgentResponse])
async def list_agents(
    project_id: int,
    _auth=Depends(require_project_role("support_agent")),
    db: Session = Depends(get_db),
):
    """List support_agent+ users for ticket assignment dropdown."""
    agents = get_project_agents(db, project_id)
    return [
        AgentResponse(
            user_id=m.user_id,
            name=m.user.name if m.user else "Unknown",
            email=m.user.email if m.user else "",
            role=m.role,
        )
        for m in agents
    ]


@router.get("/users/me/projects", response_model=List[UserProjectResponse])
async def get_my_projects(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List projects accessible to the current user."""
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")

    # Workspace owners see all projects in their workspace
    if is_workspace_admin(db, current_user):
        projects = (
            db.query(Project)
            .filter(Project.workspace_id == current_user.workspace_id, Project.is_active == True)
            .all()
        )
        return [
            UserProjectResponse(
                id=p.id,
                name=p.name,
                description=p.description,
                workspace_id=p.workspace_id,
                is_active=p.is_active,
                role="owner",
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
            for p in projects
        ]

    # Non-owners see only projects where they have active membership
    memberships = get_user_projects(db, user_id)
    return [
        UserProjectResponse(
            id=item["project"].id,
            name=item["project"].name,
            description=item["project"].description,
            workspace_id=item["project"].workspace_id,
            is_active=item["project"].is_active,
            role=item["role"],
            created_at=item["project"].created_at,
            updated_at=item["project"].updated_at,
        )
        for item in memberships
    ]
