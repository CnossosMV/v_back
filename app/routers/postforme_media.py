"""
Post for Me Media API Routes
Handles media upload URL generation and tracking
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import get_db
from app.models import User, Project, PostForMeCredential, PostForMeMedia
from app.routers.auth import get_current_user
from app.services.encryption_service import decrypt_value
from app.services.postforme.client import PostForMeClient
from app import schemas

router = APIRouter(prefix="/postforme/media", tags=["Post for Me - Media"])


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
            detail="No active Post for Me credentials found. Please configure API key first."
        )

    api_key = decrypt_value(credential.api_key_encrypted)
    return PostForMeClient(api_key)


@router.post("/upload-url", response_model=schemas.MediaUploadUrlResponse, status_code=status.HTTP_201_CREATED)
async def create_upload_url(
    project_id: int,
    data: schemas.MediaUploadUrlRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate signed URL for media upload

    Query Params:
        project_id: Project ID

    Body:
        file_name: Name of file to upload

    Returns:
        Upload URL and media ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Get Post for Me client
    client = get_postforme_client(project_id, db)

    # Create upload URL via API
    try:
        api_response = await client.create_upload_url(data.file_name)

        # Save to database for tracking
        media = PostForMeMedia(
            project_id=project_id,
            postforme_media_id=api_response.get("id"),
            file_name=data.file_name,
            upload_url=api_response.get("uploadUrl"),
            status='pending',
            upload_expires_at=datetime.fromisoformat(
                api_response["expiresAt"].replace("Z", "+00:00")
            ) if api_response.get("expiresAt") else None,
            uploaded_by=current_user.id
        )

        db.add(media)
        db.commit()
        db.refresh(media)

        return {
            "id": api_response.get("id"),
            "upload_url": api_response.get("uploadUrl"),
            "expires_at": media.upload_expires_at
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create upload URL: {str(e)}"
        )


@router.get("/{media_id}", response_model=schemas.MediaResponse)
def get_media(
    media_id: str,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get media details

    Path Params:
        media_id: Post for Me media ID

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Find media in database
    media = db.query(PostForMeMedia).filter(
        PostForMeMedia.postforme_media_id == media_id,
        PostForMeMedia.project_id == project_id
    ).first()

    if not media:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found"
        )

    return media
