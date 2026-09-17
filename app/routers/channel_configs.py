"""
Router for project-level channel configurations (enable/disable toggles).
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.models import ProjectChannelConfig, ChannelCapability
from app.schemas.channel_configs import (
    ProjectChannelConfigResponse,
    ProjectChannelConfigUpsert,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Channel Configs"])

# Default enabled channels for auto-seeding
_DEFAULT_ENABLED = {"whatsapp", "email", "sms", "web"}


def _auto_seed(db: Session, project_id: int) -> None:
    """Seed channel configs for a project on first access."""
    existing = (
        db.query(ProjectChannelConfig.channel)
        .filter(ProjectChannelConfig.project_id == project_id)
        .all()
    )
    existing_channels = {row[0] for row in existing}

    all_channels = (
        db.query(ChannelCapability.channel).all()
    )
    all_channel_names = {row[0] for row in all_channels}

    missing = all_channel_names - existing_channels
    if not missing:
        return

    for ch in missing:
        db.add(ProjectChannelConfig(
            project_id=project_id,
            channel=ch,
            enabled=ch in _DEFAULT_ENABLED,
        ))
    db.commit()


@router.get(
    "/projects/{project_id}/channel-configs",
    response_model=List[ProjectChannelConfigResponse],
)
def list_channel_configs(
    project_id: int,
    db: Session = Depends(get_db),
    _user=Depends(require_project_role("viewer")),
):
    _auto_seed(db, project_id)
    configs = (
        db.query(ProjectChannelConfig)
        .filter(ProjectChannelConfig.project_id == project_id)
        .order_by(ProjectChannelConfig.channel)
        .all()
    )
    return configs


@router.get(
    "/projects/{project_id}/channel-configs/{channel}",
    response_model=ProjectChannelConfigResponse,
)
def get_channel_config(
    project_id: int,
    channel: str,
    db: Session = Depends(get_db),
    _user=Depends(require_project_role("viewer")),
):
    _auto_seed(db, project_id)
    config = (
        db.query(ProjectChannelConfig)
        .filter(
            ProjectChannelConfig.project_id == project_id,
            ProjectChannelConfig.channel == channel,
        )
        .first()
    )
    if not config:
        raise HTTPException(404, f"Channel config for '{channel}' not found")
    return config


@router.put(
    "/projects/{project_id}/channel-configs/{channel}",
    response_model=ProjectChannelConfigResponse,
)
def upsert_channel_config(
    project_id: int,
    channel: str,
    body: ProjectChannelConfigUpsert,
    db: Session = Depends(get_db),
    _user=Depends(require_project_role("admin")),
):
    config = (
        db.query(ProjectChannelConfig)
        .filter(
            ProjectChannelConfig.project_id == project_id,
            ProjectChannelConfig.channel == channel,
        )
        .first()
    )
    if config:
        if body.enabled is not None:
            config.enabled = body.enabled
        if body.config is not None:
            config.config = body.config
    else:
        config = ProjectChannelConfig(
            project_id=project_id,
            channel=channel,
            enabled=body.enabled if body.enabled is not None else True,
            config=body.config,
        )
        db.add(config)
    db.commit()
    db.refresh(config)
    return config
