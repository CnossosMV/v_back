"""
Messaging Channels Router
Manages delivery channels with webhook configuration
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import Project, User
from app.models.messaging import MessagingChannel
from app.schemas.messaging import (
    MessagingChannelCreate,
    MessagingChannelUpdate,
    MessagingChannelResponse,
    MessagingChannelTestResponse
)
from app.routers.auth import get_current_user
from app.services.messaging import webhook_dispatcher

router = APIRouter(prefix="/projects/{project_id}/messaging/channels", tags=["messaging-channels"])


def get_project_or_404(db: Session, project_id: int, workspace_id: int) -> Project:
    """Get project and verify workspace access"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/", response_model=List[MessagingChannelResponse])
def list_channels(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all messaging channels for a project"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    channels = db.query(MessagingChannel).filter(
        MessagingChannel.project_id == project_id
    ).order_by(MessagingChannel.created_at.desc()).all()

    return channels


@router.post("/", response_model=MessagingChannelResponse, status_code=status.HTTP_201_CREATED)
def create_channel(
    project_id: int,
    data: MessagingChannelCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new messaging channel"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    # Check if slug already exists for this project
    existing = db.query(MessagingChannel).filter(
        MessagingChannel.project_id == project_id,
        MessagingChannel.slug == data.slug
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Channel with this slug already exists"
        )

    # If setting as default, unset other defaults
    if data.is_default:
        db.query(MessagingChannel).filter(
            MessagingChannel.project_id == project_id,
            MessagingChannel.is_default == True
        ).update({"is_default": False})

    # Encrypt auth config if present
    auth_config = None
    if data.auth_config:
        auth_config = webhook_dispatcher.encrypt_auth_config(data.auth_config)

    channel = MessagingChannel(
        project_id=project_id,
        name=data.name,
        slug=data.slug,
        channel_type=data.channel_type,
        webhook_url=data.webhook_url,
        auth_type=data.auth_type,
        auth_config=auth_config,
        headers=data.headers,
        is_default=data.is_default or False,
        max_retries=data.max_retries or 3,
        retry_delay_seconds=data.retry_delay_seconds or 60,
        timeout_seconds=data.timeout_seconds or 30
    )

    db.add(channel)
    db.commit()
    db.refresh(channel)

    return channel


@router.get("/{channel_id}", response_model=MessagingChannelResponse)
def get_channel(
    project_id: int,
    channel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific messaging channel"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    channel = db.query(MessagingChannel).filter(
        MessagingChannel.id == channel_id,
        MessagingChannel.project_id == project_id
    ).first()

    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    return channel


@router.put("/{channel_id}", response_model=MessagingChannelResponse)
def update_channel(
    project_id: int,
    channel_id: int,
    data: MessagingChannelUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a messaging channel"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    channel = db.query(MessagingChannel).filter(
        MessagingChannel.id == channel_id,
        MessagingChannel.project_id == project_id
    ).first()

    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Update fields
    if data.name is not None:
        channel.name = data.name

    if data.webhook_url is not None:
        channel.webhook_url = data.webhook_url

    if data.auth_type is not None:
        channel.auth_type = data.auth_type

    if data.auth_config is not None:
        channel.auth_config = webhook_dispatcher.encrypt_auth_config(data.auth_config)

    if data.headers is not None:
        channel.headers = data.headers

    if data.is_default is not None:
        if data.is_default:
            # Unset other defaults
            db.query(MessagingChannel).filter(
                MessagingChannel.project_id == project_id,
                MessagingChannel.is_default == True,
                MessagingChannel.id != channel_id
            ).update({"is_default": False})
        channel.is_default = data.is_default

    if data.is_active is not None:
        channel.is_active = data.is_active

    if data.max_retries is not None:
        channel.max_retries = data.max_retries

    if data.retry_delay_seconds is not None:
        channel.retry_delay_seconds = data.retry_delay_seconds

    if data.timeout_seconds is not None:
        channel.timeout_seconds = data.timeout_seconds

    db.commit()
    db.refresh(channel)

    return channel


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(
    project_id: int,
    channel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a messaging channel"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    channel = db.query(MessagingChannel).filter(
        MessagingChannel.id == channel_id,
        MessagingChannel.project_id == project_id
    ).first()

    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    db.delete(channel)
    db.commit()


@router.post("/{channel_id}/test", response_model=MessagingChannelTestResponse)
async def test_channel(
    project_id: int,
    channel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Test a channel configuration by sending a test request to the webhook"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    channel = db.query(MessagingChannel).filter(
        MessagingChannel.id == channel_id,
        MessagingChannel.project_id == project_id
    ).first()

    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Test the webhook
    result = await webhook_dispatcher.test_channel(channel)

    return MessagingChannelTestResponse(
        success=result['success'],
        message="Webhook test successful" if result['success'] else f"Webhook test failed: {result.get('error', 'Unknown error')}",
        response_status=result.get('status_code'),
        response_time_ms=result.get('response_time_ms')
    )
