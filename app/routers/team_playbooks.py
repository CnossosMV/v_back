"""
Team Playbooks Router

Endpoints for browsing playbook templates and creating teams from them.
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import TeamPlaybook
from app.schemas.agent_teams import (
    PlaybookResponse,
    PlaybookPreviewResponse,
    CreateFromPlaybookRequest,
    AgentTeamResponse,
)
from app.routers.auth import get_current_user
from app.services.chatbot.playbook_service import PlaybookService

router = APIRouter(tags=["team-playbooks"])


@router.get(
    "/agent-teams/playbooks",
    response_model=List[PlaybookResponse],
)
async def list_playbooks(
    category: str = Query(None, description="Filter by category"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all active playbook templates."""
    service = PlaybookService(db)
    return service.list_playbooks(category=category)


@router.get(
    "/agent-teams/playbooks/{slug}",
    response_model=PlaybookPreviewResponse,
)
async def get_playbook(
    slug: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get playbook detail/preview."""
    service = PlaybookService(db)
    preview = service.get_playbook_preview(slug)
    if not preview:
        raise HTTPException(status_code=404, detail="Playbook not found")
    return preview


@router.post(
    "/projects/{project_id}/agent-teams/from-playbook/{slug}",
    response_model=AgentTeamResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_from_playbook(
    project_id: int,
    slug: str,
    data: CreateFromPlaybookRequest = CreateFromPlaybookRequest(),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create a new agent team from a playbook template."""
    service = PlaybookService(db)

    customizations = {}
    if data.name:
        customizations["name"] = data.name
    if data.description:
        customizations["description"] = data.description
    if data.deployment_channels:
        customizations["deployment_channels"] = data.deployment_channels

    try:
        team = service.create_team_from_playbook(
            project_id=project_id,
            slug=slug,
            customizations=customizations if customizations else None,
            created_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return team
