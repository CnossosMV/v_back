"""
Agent Teams Router

CRUD endpoints for managing Agent Teams, deployment, chatbot migration,
orchestrated chat, and session management.
"""

import os
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import AgentTeam, SpecialistAgent, ChatSession, ChatMessage, TeamSessionEvent, Project
from app.schemas.agent_teams import (
    AgentTeamCreate,
    AgentTeamUpdate,
    AgentTeamResponse,
    AgentTeamListResponse,
    SpecialistAgentResponse,
    RouterConfigResponse,
    MigrateChatbotRequest,
    MigrateChatbotResponse,
    TeamChatRequest,
    TeamChatResponse,
    TeamSessionResponse,
    TeamSessionEventResponse,
    TeamMessageResponse,
)
from app.routers.auth import get_current_user
from app.services.chatbot.agent_team_service import AgentTeamService
from app.services.chatbot.orchestration_engine import OrchestrationEngine

router = APIRouter(tags=["agent-teams"])


# ============================================================================
# Agent Teams CRUD
# ============================================================================

@router.get("/projects/{project_id}/agent-teams", response_model=List[AgentTeamListResponse])
async def list_agent_teams(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all agent teams for a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = AgentTeamService(db)
    teams = service.list_teams(project_id)

    result = []
    for team in teams:
        specialist_count = db.query(SpecialistAgent).filter(
            SpecialistAgent.team_id == team.id
        ).count()

        result.append(AgentTeamListResponse(
            id=team.id,
            project_id=team.project_id,
            name=team.name,
            description=team.description,
            status=team.status,
            deployment_channels=team.deployment_channels or [],
            specialist_count=specialist_count,
            created_at=team.created_at,
            updated_at=team.updated_at,
        ))

    return result


@router.post(
    "/projects/{project_id}/agent-teams",
    response_model=AgentTeamResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_team(
    project_id: int,
    team_data: AgentTeamCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create a new agent team. Auto-creates a router config."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = AgentTeamService(db)
    team = service.create_team(
        project_id=project_id,
        data=team_data.model_dump(),
        created_by=current_user.id,
    )

    return team


@router.get("/agent-teams/{team_id}", response_model=AgentTeamResponse)
async def get_agent_team(
    team_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get an agent team with specialists and router config."""
    service = AgentTeamService(db)
    team = service.get_team(team_id)

    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")

    return team


@router.put("/agent-teams/{team_id}", response_model=AgentTeamResponse)
async def update_agent_team(
    team_id: int,
    team_data: AgentTeamUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update an agent team."""
    service = AgentTeamService(db)
    team = service.update_team(team_id, team_data.model_dump(exclude_unset=True))

    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")

    return team


@router.delete("/agent-teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_team(
    team_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete an agent team and all related data (cascade)."""
    service = AgentTeamService(db)
    if not service.delete_team(team_id):
        raise HTTPException(status_code=404, detail="Agent team not found")


# ============================================================================
# Deployment
# ============================================================================

@router.post("/agent-teams/{team_id}/deploy", response_model=AgentTeamResponse)
async def deploy_agent_team(
    team_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Deploy an agent team (set status to active)."""
    service = AgentTeamService(db)
    try:
        team = service.deploy_team(team_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")

    return team


@router.post("/agent-teams/{team_id}/pause", response_model=AgentTeamResponse)
async def pause_agent_team(
    team_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Pause an agent team."""
    service = AgentTeamService(db)
    team = service.pause_team(team_id)

    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")

    return team


# ============================================================================
# Migration
# ============================================================================

@router.post(
    "/projects/{project_id}/agent-teams/migrate/{chatbot_id}",
    response_model=MigrateChatbotResponse,
    status_code=status.HTTP_201_CREATED,
)
async def migrate_chatbot_to_team(
    project_id: int,
    chatbot_id: int,
    migration_data: MigrateChatbotRequest = MigrateChatbotRequest(),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Migrate an existing chatbot into an Agent Team with a single specialist."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = AgentTeamService(db)

    try:
        result = service.migrate_from_chatbot(
            project_id=project_id,
            chatbot_id=chatbot_id,
            team_name=migration_data.team_name,
            created_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return MigrateChatbotResponse(
        team=AgentTeamResponse.model_validate(result["team"]),
        specialist=SpecialistAgentResponse.model_validate(result["specialist"]),
        knowledge_sources_migrated=result["knowledge_sources_migrated"],
        message=f"Successfully migrated chatbot to agent team with {result['knowledge_sources_migrated']} knowledge sources",
    )


# ============================================================================
# Chat (Orchestrated)
# ============================================================================

@router.post("/agent-teams/{team_id}/chat", response_model=TeamChatResponse)
async def chat_with_team(
    team_id: int,
    request: TeamChatRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Send a message to an agent team and get an orchestrated response."""
    team = db.query(AgentTeam).filter(AgentTeam.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")

    engine = OrchestrationEngine(db)

    try:
        result = engine.process_message(
            team_id=team_id,
            message=request.message,
            user_identifier=request.user_identifier or f"user_{current_user.id}",
            channel=request.channel,
            session_id=request.session_id,
            user_id=current_user.id,
            context=request.context,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return TeamChatResponse(
        response=result["response"],
        session_id=result["session_id"],
        images=result.get("images", []),
        debug=result.get("debug"),
    )


# ============================================================================
# Sessions
# ============================================================================

@router.get("/agent-teams/{team_id}/sessions", response_model=List[TeamSessionResponse])
async def list_team_sessions(
    team_id: int,
    is_active: Optional[bool] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List sessions for an agent team."""
    team = db.query(AgentTeam).filter(AgentTeam.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")

    query = db.query(ChatSession).filter(ChatSession.team_id == team_id)
    if is_active is not None:
        query = query.filter(ChatSession.is_active == is_active)

    sessions = query.order_by(ChatSession.last_interaction_at.desc()).offset(offset).limit(limit).all()

    result = []
    for session in sessions:
        msg_count = db.query(func.count(ChatMessage.id)).filter(
            ChatMessage.session_id == session.id
        ).scalar() or 0

        result.append(TeamSessionResponse(
            id=session.id,
            team_id=session.team_id,
            user_identifier=session.user_identifier,
            channel=session.channel,
            is_active=session.is_active,
            session_state=session.session_state,
            current_agent_id=session.current_agent_id,
            context_summary=session.context_summary,
            extracted_entities=session.extracted_entities,
            started_at=session.started_at,
            last_interaction_at=session.last_interaction_at,
            message_count=msg_count,
        ))

    return result


@router.get("/agent-teams/{team_id}/sessions/{session_id}", response_model=TeamSessionResponse)
async def get_team_session(
    team_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get a specific team session."""
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.team_id == team_id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    msg_count = db.query(func.count(ChatMessage.id)).filter(
        ChatMessage.session_id == session.id
    ).scalar() or 0

    return TeamSessionResponse(
        id=session.id,
        team_id=session.team_id,
        user_identifier=session.user_identifier,
        channel=session.channel,
        is_active=session.is_active,
        session_state=session.session_state,
        current_agent_id=session.current_agent_id,
        context_summary=session.context_summary,
        extracted_entities=session.extracted_entities,
        started_at=session.started_at,
        last_interaction_at=session.last_interaction_at,
        message_count=msg_count,
    )


@router.get(
    "/agent-teams/{team_id}/sessions/{session_id}/messages",
    response_model=List[TeamMessageResponse],
)
async def get_session_messages(
    team_id: int,
    session_id: int,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get messages for a team session."""
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.team_id == team_id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.timestamp.asc())
        .limit(limit)
        .all()
    )

    return [
        TeamMessageResponse(
            id=m.id,
            session_id=m.session_id,
            role=m.role,
            content=m.content,
            agent_id=m.agent_id,
            routing_decision=m.routing_decision,
            images=m.images or [],
            timestamp=m.timestamp,
        )
        for m in messages
    ]


@router.get(
    "/agent-teams/{team_id}/sessions/{session_id}/events",
    response_model=List[TeamSessionEventResponse],
)
async def get_session_events(
    team_id: int,
    session_id: int,
    event_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get events for a team session."""
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.team_id == team_id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    engine = OrchestrationEngine(db)
    events = engine.get_session_events(session_id, event_type=event_type)

    return [
        TeamSessionEventResponse(
            id=e.id,
            session_id=e.session_id,
            event_type=e.event_type,
            event_data=e.event_data,
            agent_id=e.agent_id,
            created_at=e.created_at,
        )
        for e in events
    ]


@router.post("/agent-teams/{team_id}/sessions/{session_id}/end", status_code=status.HTTP_200_OK)
async def end_team_session(
    team_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """End a team session."""
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.team_id == team_id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    engine = OrchestrationEngine(db)
    if not engine.end_session(session_id):
        raise HTTPException(status_code=400, detail="Failed to end session")

    return {"message": "Session ended", "session_id": session_id}
