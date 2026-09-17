"""
Handler Channel Links Router

CRUD for handler-to-channel linkages (chatbot/agent_team → channel → instance).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

from app.database import get_db
from app.routers.auth import get_current_user
from app.services.handler_channel_link_service import HandlerChannelLinkService

router = APIRouter(tags=["handler-channel-links"])


# ── Schemas ──────────────────────────────────────────────────────────────

class ChannelLinkResponse(BaseModel):
    id: int
    handler_type: str
    handler_id: int
    channel: str
    instance_id: Optional[int] = None
    config: Optional[dict] = None
    is_primary: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ChannelLinkUpsert(BaseModel):
    instance_id: Optional[int] = None
    config: Optional[dict] = None
    is_primary: bool = True


# ── Endpoints ────────────────────────────────────────────────────────────

@router.get(
    "/projects/{project_id}/handlers/{handler_type}/{handler_id}/channel-links",
    response_model=List[ChannelLinkResponse],
)
async def list_channel_links(
    project_id: int,
    handler_type: str,
    handler_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all channel links for a handler."""
    svc = HandlerChannelLinkService(db)
    return svc.get_links(handler_type, handler_id)


@router.put(
    "/projects/{project_id}/handlers/{handler_type}/{handler_id}/channel-links/{channel}",
    response_model=ChannelLinkResponse,
)
async def upsert_channel_link(
    project_id: int,
    handler_type: str,
    handler_id: int,
    channel: str,
    body: ChannelLinkUpsert,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create or update a channel link for a handler."""
    if handler_type not in ("chatbot", "agent_team"):
        raise HTTPException(status_code=400, detail="handler_type must be 'chatbot' or 'agent_team'")
    svc = HandlerChannelLinkService(db)
    link = svc.set_link(
        handler_type=handler_type,
        handler_id=handler_id,
        channel=channel,
        instance_id=body.instance_id,
        config=body.config,
        is_primary=body.is_primary,
    )
    return link


@router.delete(
    "/projects/{project_id}/handlers/{handler_type}/{handler_id}/channel-links/{channel}",
)
async def delete_channel_link(
    project_id: int,
    handler_type: str,
    handler_id: int,
    channel: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Remove a channel link for a handler."""
    svc = HandlerChannelLinkService(db)
    removed = svc.remove_link(handler_type, handler_id, channel)
    if not removed:
        raise HTTPException(status_code=404, detail="Link not found")
    return {"ok": True}
