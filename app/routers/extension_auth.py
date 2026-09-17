"""
Extension Auth Router — manages Chrome Extension authentication tokens.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.routers.auth import get_current_user
from app.services.extension_auth_service import ExtensionAuthService
from app.schemas.nocode_mappings import (
    ExtensionTokenCreate, ExtensionTokenResponse, ExtensionTokenCreateResponse,
)
from typing import List

router = APIRouter(prefix="/auth/extension", tags=["Extension Auth"])


@router.post("/token", response_model=ExtensionTokenCreateResponse)
async def create_extension_token(
    data: ExtensionTokenCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create a scoped extension token (8h expiry)."""
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    svc = ExtensionAuthService(db)
    raw_token, token = svc.create_token(user_id, data.project_id, data.role.value)
    return ExtensionTokenCreateResponse(
        token=raw_token,
        id=token.id,
        projectId=token.project_id,
        tokenPrefix=token.token_prefix,
        role=token.role,
        expiresAt=token.expires_at,
    )


@router.post("/revoke")
async def revoke_extension_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Revoke an extension token."""
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    svc = ExtensionAuthService(db)
    success = svc.revoke_token(token_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"status": "revoked"}


@router.get("/tokens", response_model=List[ExtensionTokenResponse])
async def list_extension_tokens(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List active extension tokens for the current user."""
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    svc = ExtensionAuthService(db)
    tokens = svc.list_active_tokens(user_id)
    return [
        ExtensionTokenResponse(
            id=t.id,
            projectId=t.project_id,
            tokenPrefix=t.token_prefix,
            role=t.role,
            isActive=t.is_active,
            expiresAt=t.expires_at,
            createdAt=t.created_at,
            revokedAt=t.revoked_at,
        )
        for t in tokens
    ]
