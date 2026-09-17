"""
Specialist Tools Router

CRUD endpoints for managing specialist tools (webhooks, MCP servers),
testing tools, and viewing execution history.
"""

import json
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SpecialistAgent, SpecialistTool, ToolExecution, AgentTeam
from app.schemas.agent_teams import (
    SpecialistToolCreate,
    SpecialistToolUpdate,
    SpecialistToolResponse,
    ToolTestRequest,
    ToolTestResponse,
    ToolExecutionResponse,
    MCPDiscoverRequest,
    MCPDiscoverResponse,
)
from app.routers.auth import get_current_user
from app.services.chatbot.tool_executor import ToolExecutor
from app.services.encryption_service import encrypt_value

router = APIRouter(tags=["specialist-tools"])


def _tool_to_response(tool: SpecialistTool) -> SpecialistToolResponse:
    """Convert model to response, hiding encrypted auth."""
    return SpecialistToolResponse(
        id=tool.id,
        specialist_id=tool.specialist_id,
        tool_type=tool.tool_type,
        name=tool.name,
        description=tool.description,
        when_to_use=tool.when_to_use,
        config=tool.config,
        has_auth=bool(tool.auth_config_encrypted),
        is_active=tool.is_active,
        timeout_ms=tool.timeout_ms,
        last_used_at=tool.last_used_at,
        tool_metadata=tool.tool_metadata,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
    )


# ============================================================================
# Tool CRUD
# ============================================================================

@router.get(
    "/specialists/{specialist_id}/tools",
    response_model=List[SpecialistToolResponse],
)
async def list_tools(
    specialist_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all tools for a specialist."""
    specialist = db.query(SpecialistAgent).filter(
        SpecialistAgent.id == specialist_id
    ).first()
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    tools = (
        db.query(SpecialistTool)
        .filter(SpecialistTool.specialist_id == specialist_id)
        .order_by(SpecialistTool.created_at.asc())
        .all()
    )

    return [_tool_to_response(t) for t in tools]


@router.post(
    "/specialists/{specialist_id}/tools",
    response_model=SpecialistToolResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tool(
    specialist_id: int,
    tool_data: SpecialistToolCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create a new tool for a specialist."""
    specialist = db.query(SpecialistAgent).filter(
        SpecialistAgent.id == specialist_id
    ).first()
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    if tool_data.tool_type not in ("webhook", "mcp_server", "catalog_integration", "api_connection"):
        raise HTTPException(status_code=400, detail="Invalid tool_type")

    # Encrypt auth config if provided
    auth_encrypted = None
    if tool_data.auth_config:
        auth_encrypted = encrypt_value(json.dumps(tool_data.auth_config))

    tool = SpecialistTool(
        specialist_id=specialist_id,
        tool_type=tool_data.tool_type,
        name=tool_data.name,
        description=tool_data.description,
        when_to_use=tool_data.when_to_use,
        config=tool_data.config or {},
        auth_config_encrypted=auth_encrypted,
        is_active=tool_data.is_active,
        timeout_ms=tool_data.timeout_ms,
        tool_metadata=tool_data.tool_metadata or {},
    )

    db.add(tool)
    db.commit()
    db.refresh(tool)

    return _tool_to_response(tool)


@router.get("/specialist-tools/{tool_id}", response_model=SpecialistToolResponse)
async def get_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get a tool by ID."""
    tool = db.query(SpecialistTool).filter(SpecialistTool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    return _tool_to_response(tool)


@router.put("/specialist-tools/{tool_id}", response_model=SpecialistToolResponse)
async def update_tool(
    tool_id: int,
    tool_data: SpecialistToolUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update a tool."""
    tool = db.query(SpecialistTool).filter(SpecialistTool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    update_data = tool_data.model_dump(exclude_unset=True)

    # Handle auth config encryption separately
    if "auth_config" in update_data:
        auth_config = update_data.pop("auth_config")
        if auth_config is not None:
            tool.auth_config_encrypted = encrypt_value(json.dumps(auth_config))
        else:
            tool.auth_config_encrypted = None

    for key, value in update_data.items():
        if hasattr(tool, key):
            setattr(tool, key, value)

    db.commit()
    db.refresh(tool)

    return _tool_to_response(tool)


@router.delete("/specialist-tools/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete a tool."""
    tool = db.query(SpecialistTool).filter(SpecialistTool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    db.delete(tool)
    db.commit()


# ============================================================================
# Tool Testing & Execution History
# ============================================================================

@router.post(
    "/specialist-tools/{tool_id}/test",
    response_model=ToolTestResponse,
)
async def test_tool(
    tool_id: int,
    test_data: ToolTestRequest = ToolTestRequest(),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Test a tool with optional parameters."""
    tool = db.query(SpecialistTool).filter(SpecialistTool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    executor = ToolExecutor(db)

    try:
        result = executor.test_tool(tool_id, test_data.parameters)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ToolTestResponse(
        execution_id=result["execution_id"],
        status=result["status"],
        result=result["result"],
        error=result["error"],
        duration_ms=result["duration_ms"],
    )


@router.get(
    "/specialist-tools/{tool_id}/executions",
    response_model=List[ToolExecutionResponse],
)
async def get_execution_history(
    tool_id: int,
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get execution history for a tool."""
    tool = db.query(SpecialistTool).filter(SpecialistTool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    executor = ToolExecutor(db)
    executions = executor.get_execution_history(tool_id, limit=limit)

    return [
        ToolExecutionResponse(
            id=e.id,
            tool_id=e.tool_id,
            session_id=e.session_id,
            message_id=e.message_id,
            status=e.status,
            request_data=e.request_data,
            response_data=e.response_data,
            error_message=e.error_message,
            duration_ms=e.duration_ms,
            created_at=e.created_at,
        )
        for e in executions
    ]


# ============================================================================
# MCP Discovery
# ============================================================================

@router.post(
    "/specialists/{specialist_id}/tools/mcp-discover",
    response_model=MCPDiscoverResponse,
)
async def discover_mcp_tools(
    specialist_id: int,
    discover_data: MCPDiscoverRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Discover tools from an MCP server URL."""
    specialist = db.query(SpecialistAgent).filter(
        SpecialistAgent.id == specialist_id
    ).first()
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    executor = ToolExecutor(db)

    try:
        tools = executor.discover_mcp_tools(discover_data.server_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return MCPDiscoverResponse(tools=tools)
