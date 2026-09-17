"""
Messaging API Keys Router
Manages secret API keys for backend-to-backend authentication
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime

from app.database import get_db
from app.models import Project, User
from app.models.messaging import MessagingApiKey
from app.schemas.messaging import (
    MessagingApiKeyCreate,
    MessagingApiKeyResponse,
    MessagingApiKeyCreateResponse,
    MessagingApiKeyRotateResponse
)
from app.routers.auth import get_current_user
from app.services.messaging import key_generator

router = APIRouter(prefix="/projects/{project_id}/messaging/api-keys", tags=["messaging-api-keys"])


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


@router.get("/", response_model=List[MessagingApiKeyResponse])
def list_api_keys(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all API keys for a project (secret keys are hidden)"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    keys = db.query(MessagingApiKey).filter(
        MessagingApiKey.project_id == project_id
    ).order_by(MessagingApiKey.created_at.desc()).all()

    return keys


@router.post("/", response_model=MessagingApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
def create_api_key(
    project_id: int,
    data: MessagingApiKeyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new API key.
    IMPORTANT: The secret_key is only returned once on creation.
    Store it securely - it cannot be retrieved again.
    """
    get_project_or_404(db, project_id, current_user.workspace_id)

    # Generate secret key
    secret_key, key_hash, key_prefix = key_generator.generate_secret_key()

    api_key = MessagingApiKey(
        project_id=project_id,
        name=data.name,
        secret_key_hash=key_hash,
        key_prefix=key_prefix,
        permissions=data.permissions or ["send", "track", "identify"],
        rate_limit_per_minute=data.rate_limit_per_minute or 1000,
        rate_limit_per_day=data.rate_limit_per_day or 100000
    )

    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    # Return response with the full secret key (only time it's shown)
    return MessagingApiKeyCreateResponse(
        id=api_key.id,
        project_id=api_key.project_id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        permissions=api_key.permissions,
        rate_limit_per_minute=api_key.rate_limit_per_minute,
        rate_limit_per_day=api_key.rate_limit_per_day,
        is_active=api_key.is_active,
        last_used_at=api_key.last_used_at,
        created_at=api_key.created_at,
        secret_key=secret_key  # Only returned on creation
    )


@router.get("/{key_id}", response_model=MessagingApiKeyResponse)
def get_api_key(
    project_id: int,
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific API key (secret key is hidden)"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    api_key = db.query(MessagingApiKey).filter(
        MessagingApiKey.id == key_id,
        MessagingApiKey.project_id == project_id
    ).first()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    return api_key


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(
    project_id: int,
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete an API key"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    api_key = db.query(MessagingApiKey).filter(
        MessagingApiKey.id == key_id,
        MessagingApiKey.project_id == project_id
    ).first()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    db.delete(api_key)
    db.commit()


@router.post("/{key_id}/rotate", response_model=MessagingApiKeyRotateResponse)
def rotate_api_key(
    project_id: int,
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Rotate an API key - generates a new secret while keeping the same ID and settings.
    IMPORTANT: The new secret_key is only returned once.
    """
    get_project_or_404(db, project_id, current_user.workspace_id)

    api_key = db.query(MessagingApiKey).filter(
        MessagingApiKey.id == key_id,
        MessagingApiKey.project_id == project_id
    ).first()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Generate new secret key
    secret_key, key_hash, key_prefix = key_generator.generate_secret_key()

    # Update the key
    api_key.secret_key_hash = key_hash
    api_key.key_prefix = key_prefix

    db.commit()

    return MessagingApiKeyRotateResponse(
        id=api_key.id,
        key_prefix=key_prefix,
        secret_key=secret_key,
        message="API key rotated successfully. Store the new secret key securely."
    )


@router.patch("/{key_id}/deactivate", response_model=MessagingApiKeyResponse)
def deactivate_api_key(
    project_id: int,
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Deactivate an API key without deleting it"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    api_key = db.query(MessagingApiKey).filter(
        MessagingApiKey.id == key_id,
        MessagingApiKey.project_id == project_id
    ).first()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key.is_active = False
    db.commit()
    db.refresh(api_key)

    return api_key


@router.patch("/{key_id}/activate", response_model=MessagingApiKeyResponse)
def activate_api_key(
    project_id: int,
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Reactivate a deactivated API key"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    api_key = db.query(MessagingApiKey).filter(
        MessagingApiKey.id == key_id,
        MessagingApiKey.project_id == project_id
    ).first()

    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key.is_active = True
    db.commit()
    db.refresh(api_key)

    return api_key
