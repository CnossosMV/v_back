"""
Routing States — list and manage contact routing states.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.auth import get_current_user
from app.models import ContactRoutingState, Project
from app.schemas.inbound_router import ContactRoutingStateResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Routing States"])


@router.get("/projects/{project_id}/routing-states", response_model=List[ContactRoutingStateResponse])
def list_routing_states(
    project_id: int,
    handler_type: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    query = db.query(ContactRoutingState).filter(
        ContactRoutingState.project_id == project_id,
    )

    if handler_type:
        query = query.filter(ContactRoutingState.handler_type == handler_type)
    if channel:
        query = query.filter(ContactRoutingState.channel == channel)

    query = query.order_by(ContactRoutingState.assigned_at.desc())
    states = query.offset(offset).limit(limit).all()
    return states


@router.delete("/routing-states/{state_id}")
def clear_routing_state(
    state_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    state = db.query(ContactRoutingState).filter(
        ContactRoutingState.id == state_id,
    ).first()
    if not state:
        raise HTTPException(status_code=404, detail="Routing state not found")

    state.handler_type = "idle"
    state.handler_id = None
    state.handler_priority = 0
    state.session_id = None
    state.expires_at = None
    db.commit()
    return {"success": True}
