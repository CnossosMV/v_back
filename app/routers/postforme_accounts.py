"""
Post for Me Social Accounts API Routes
Handles connecting, syncing, and managing social media accounts
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import os

from app.database import get_db
from app.models import User, Project, PostForMeCredential, PostForMeSocialAccount
from app.routers.auth import get_current_user
from app.services.encryption_service import decrypt_value
from app.services.postforme.client import PostForMeClient
from app import schemas

router = APIRouter(prefix="/postforme/accounts", tags=["Post for Me - Accounts"])


def verify_project_access(
    project_id: int,
    current_user: User,
    db: Session
) -> Project:
    """Verify user has access to project"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id,
        Project.is_active == True
    ).first()

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found or access denied"
        )

    return project


def get_postforme_client(project_id: int, db: Session) -> PostForMeClient:
    """Get Post for Me client for a project"""
    credential = db.query(PostForMeCredential).filter(
        PostForMeCredential.project_id == project_id,
        PostForMeCredential.is_active == True
    ).first()

    if not credential:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active Post for Me credentials found for this project. Please configure API key first."
        )

    api_key = decrypt_value(credential.api_key_encrypted)
    return PostForMeClient(api_key)


def sync_account_to_db(
    project_id: int,
    credential_id: int,
    account_data: dict,
    db: Session
) -> PostForMeSocialAccount:
    """Sync account from Post for Me API to database"""

    # Check if exists
    account = db.query(PostForMeSocialAccount).filter(
        PostForMeSocialAccount.postforme_account_id == account_data.get("id")
    ).first()

    if account:
        # Update existing
        account.account_name = account_data.get("name")
        account.account_username = account_data.get("username")
        account.account_profile_url = account_data.get("profileUrl")
        account.is_connected = account_data.get("isConnected", True)
        account.platform_metadata = account_data.get("metadata", {})
        account.last_sync_at = datetime.utcnow()
        account.updated_at = datetime.utcnow()
    else:
        # Create new
        account = PostForMeSocialAccount(
            credential_id=credential_id,
            project_id=project_id,
            postforme_account_id=account_data.get("id"),
            platform=account_data.get("platform"),
            account_name=account_data.get("name"),
            account_username=account_data.get("username"),
            account_profile_url=account_data.get("profileUrl"),
            is_connected=account_data.get("isConnected", True),
            platform_metadata=account_data.get("metadata", {}),
            last_sync_at=datetime.utcnow()
        )
        db.add(account)

    db.commit()
    db.refresh(account)
    return account


@router.get("", response_model=List[schemas.PostForMeSocialAccountResponse])
async def list_social_accounts(
    project_id: int,
    platform: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all connected social accounts for the project

    Query Params:
        project_id: Project ID
        platform: Optional filter by platform (facebook, instagram, twitter, etc.)
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Get Post for Me client
    client = get_postforme_client(project_id, db)

    # Get credential for database sync
    credential = db.query(PostForMeCredential).filter(
        PostForMeCredential.project_id == project_id
    ).first()

    # Fetch from Post for Me API
    try:
        api_response = await client.get_social_accounts(platform=platform, limit=100)

        # Sync with local database
        for account_data in api_response.get("data", []):
            sync_account_to_db(project_id, credential.id, account_data, db)

    except Exception as e:
        # If API fails, continue with database data
        print(f"Warning: Failed to sync from Post for Me API: {str(e)}")

    # Return from database
    query = db.query(PostForMeSocialAccount).filter(
        PostForMeSocialAccount.project_id == project_id
    )

    if platform:
        query = query.filter(PostForMeSocialAccount.platform == platform)

    accounts = query.order_by(PostForMeSocialAccount.created_at.desc()).all()

    return accounts


@router.post("/auth-url", response_model=schemas.AuthUrlResponse)
async def generate_auth_url(
    project_id: int,
    data: schemas.AuthUrlRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate OAuth URL for connecting a social account

    Query Params:
        project_id: Project ID

    Body:
        platform: Platform name (facebook, instagram, twitter, etc.)
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Get Post for Me client
    client = get_postforme_client(project_id, db)

    # Generate callback URL
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3001")
    redirect_url = f"{frontend_url}/credentials/postforme/callback?project_id={project_id}"

    # Get auth URL from Post for Me
    try:
        api_response = await client.generate_auth_url(
            platform=data.platform,
            redirect_url=redirect_url
        )

        return {
            "auth_url": api_response.get("authUrl"),
            "platform": data.platform
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate auth URL: {str(e)}"
        )


@router.post("/sync", status_code=status.HTTP_200_OK)
async def sync_accounts(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Manually sync social accounts from Post for Me API

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Get Post for Me client
    client = get_postforme_client(project_id, db)

    # Get credential
    credential = db.query(PostForMeCredential).filter(
        PostForMeCredential.project_id == project_id
    ).first()

    # Fetch all accounts
    try:
        api_response = await client.get_social_accounts(limit=100)

        synced_count = 0
        for account_data in api_response.get("data", []):
            sync_account_to_db(project_id, credential.id, account_data, db)
            synced_count += 1

        return {
            "success": True,
            "synced": synced_count,
            "message": f"Synced {synced_count} accounts"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync accounts: {str(e)}"
        )


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_account(
    account_id: str,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Disconnect a social account

    Path Params:
        account_id: Post for Me account ID

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Get Post for Me client
    client = get_postforme_client(project_id, db)

    # Find account in database
    account = db.query(PostForMeSocialAccount).filter(
        PostForMeSocialAccount.postforme_account_id == account_id,
        PostForMeSocialAccount.project_id == project_id
    ).first()

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found"
        )

    # Disconnect via API
    try:
        await client.disconnect_social_account(account_id)
    except Exception as e:
        # Continue even if API fails
        print(f"Warning: Failed to disconnect via API: {str(e)}")

    # Update in database
    account.is_connected = False
    account.connection_status = 'disconnected'
    db.commit()

    return None


@router.get("/{account_id}", response_model=schemas.PostForMeSocialAccountResponse)
async def get_account(
    account_id: str,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get specific social account details

    Path Params:
        account_id: Post for Me account ID

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Find account in database
    account = db.query(PostForMeSocialAccount).filter(
        PostForMeSocialAccount.postforme_account_id == account_id,
        PostForMeSocialAccount.project_id == project_id
    ).first()

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found"
        )

    return account
