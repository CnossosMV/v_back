"""
Post for Me Credentials Management API Routes
Handles saving, validating, and managing Post for Me API keys per project
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import get_db
from app.models import User, Project, PostForMeCredential
from app.routers.auth import get_current_user
from app.services.encryption_service import encrypt_value, decrypt_value
from app.services.postforme.client import PostForMeClient
from app import schemas

router = APIRouter(prefix="/postforme/credentials", tags=["Post for Me - Credentials"])


def verify_project_access(
    project_id: int,
    current_user: User,
    db: Session
) -> Project:
    """
    Verify user has access to project
    Returns project if authorized, raises HTTPException otherwise
    """
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


@router.post("", response_model=schemas.PostForMeCredentialResponse, status_code=status.HTTP_201_CREATED)
async def create_or_update_credential(
    project_id: int,
    data: schemas.PostForMeCredentialCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save or update Post for Me API credentials for a project

    Query Params:
        project_id: Project ID

    Body:
        api_key: Post for Me API key
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Validate API key by making a test request
    client = PostForMeClient(data.api_key)
    try:
        # Test the API key with a simple request
        await client.get_social_accounts(limit=1)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid API key: {str(e)}"
        )

    # Check if credential already exists for this project
    credential = db.query(PostForMeCredential).filter(
        PostForMeCredential.project_id == project_id
    ).first()

    if credential:
        # Update existing
        credential.api_key_encrypted = encrypt_value(data.api_key)
        credential.is_active = True
        credential.last_validated_at = datetime.utcnow()
        credential.updated_at = datetime.utcnow()
    else:
        # Create new
        credential = PostForMeCredential(
            project_id=project_id,
            api_key_encrypted=encrypt_value(data.api_key),
            is_active=True,
            last_validated_at=datetime.utcnow(),
            created_by=current_user.id
        )
        db.add(credential)

    db.commit()
    db.refresh(credential)

    return credential


@router.get("", response_model=schemas.PostForMeCredentialResponse)
def get_credential(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get Post for Me credentials for a project (without exposing the API key)

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    credential = db.query(PostForMeCredential).filter(
        PostForMeCredential.project_id == project_id
    ).first()

    if not credential:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Post for Me credentials found for this project"
        )

    return credential


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Delete Post for Me credentials for a project

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    credential = db.query(PostForMeCredential).filter(
        PostForMeCredential.project_id == project_id
    ).first()

    if not credential:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No credentials found"
        )

    # Delete will cascade to related records
    db.delete(credential)
    db.commit()

    return None


@router.post("/validate", status_code=status.HTTP_200_OK)
async def validate_credential(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Validate existing Post for Me credentials

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    credential = db.query(PostForMeCredential).filter(
        PostForMeCredential.project_id == project_id
    ).first()

    if not credential:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No credentials found"
        )

    # Test API key
    api_key = decrypt_value(credential.api_key_encrypted)
    client = PostForMeClient(api_key)

    try:
        await client.get_social_accounts(limit=1)

        # Update validation timestamp
        credential.last_validated_at = datetime.utcnow()
        credential.is_active = True
        db.commit()

        return {
            "valid": True,
            "message": "Credentials are valid",
            "last_validated": credential.last_validated_at
        }
    except Exception as e:
        credential.is_active = False
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid credentials: {str(e)}"
        )
