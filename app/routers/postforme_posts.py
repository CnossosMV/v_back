"""
Post for Me Posts API Routes
Handles creating, listing, and managing social media posts
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models import User, Project, PostForMeCredential, PostForMePost
from app.routers.auth import get_current_user
from app.services.encryption_service import decrypt_value
from app.services.postforme.posts_service import PostsService
from app import schemas

router = APIRouter(prefix="/postforme/posts", tags=["Post for Me - Posts"])


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


def get_posts_service(project_id: int, db: Session) -> PostsService:
    """Get Posts Service for a project"""
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
    return PostsService(db, api_key)


@router.post("", response_model=schemas.PostResponse, status_code=status.HTTP_201_CREATED)
async def create_post(
    project_id: int,
    data: schemas.PostCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new social media post

    Query Params:
        project_id: Project ID

    Body:
        account_ids: List of Post for Me account IDs
        content: Post content with platform overrides
        scheduled_time: Optional scheduled time
        external_id: Optional external reference ID
        tags: Optional tags
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Get posts service
    service = get_posts_service(project_id, db)

    # Convert content to dict format for API
    content_dict = {}
    for key, value in data.content.items():
        content_dict[key] = {
            "text": value.text,
            "mediaIds": value.mediaIds or []
        }

    # Create post
    try:
        post = await service.create_post(
            project_id=project_id,
            user_id=current_user.id,
            account_ids=data.account_ids,
            content=content_dict,
            scheduled_time=data.scheduled_time,
            external_id=data.external_id,
            tags=data.tags
        )

        return post
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create post: {str(e)}"
        )


@router.get("", response_model=List[schemas.PostResponse])
def list_posts(
    project_id: int,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, le=100),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List posts for the project

    Query Params:
        project_id: Project ID
        status: Optional filter by status (draft, scheduled, published, etc.)
        limit: Max results (default 50, max 100)
        offset: Offset for pagination
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Build query
    query = db.query(PostForMePost).filter(
        PostForMePost.project_id == project_id
    )

    if status_filter:
        query = query.filter(PostForMePost.status == status_filter)

    # Order by most recent first
    query = query.order_by(PostForMePost.created_at.desc())

    # Apply pagination
    posts = query.offset(offset).limit(limit).all()

    return posts


@router.get("/{post_id}", response_model=schemas.PostWithResultsResponse)
async def get_post(
    post_id: int,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get post details with platform-specific results

    Path Params:
        post_id: Post ID

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Verify post belongs to project
    post = db.query(PostForMePost).filter(
        PostForMePost.id == post_id,
        PostForMePost.project_id == project_id
    ).first()

    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found"
        )

    # Get posts service
    service = get_posts_service(project_id, db)

    # Get post with latest results from API
    try:
        result = await service.get_post_with_results(post_id)
        return result
    except Exception as e:
        # If API sync fails, return database data
        print(f"Warning: Failed to sync results from API: {str(e)}")
        return {
            "post": post,
            "results": post.results
        }


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(
    post_id: int,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Delete a post

    Path Params:
        post_id: Post ID

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Verify post belongs to project
    post = db.query(PostForMePost).filter(
        PostForMePost.id == post_id,
        PostForMePost.project_id == project_id
    ).first()

    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found"
        )

    # Get posts service
    service = get_posts_service(project_id, db)

    # Delete post
    try:
        await service.delete_post(post_id)
        return None
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete post: {str(e)}"
        )


@router.post("/{post_id}/sync-results", status_code=status.HTTP_200_OK)
async def sync_post_results(
    post_id: int,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Manually sync post results from Post for Me API

    Path Params:
        post_id: Post ID

    Query Params:
        project_id: Project ID
    """
    # Verify project access
    project = verify_project_access(project_id, current_user, db)

    # Verify post belongs to project
    post = db.query(PostForMePost).filter(
        PostForMePost.id == post_id,
        PostForMePost.project_id == project_id
    ).first()

    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found"
        )

    # Get posts service
    service = get_posts_service(project_id, db)

    # Sync results
    try:
        results = await service.sync_post_results(post_id)
        return {
            "success": True,
            "synced": len(results),
            "message": f"Synced {len(results)} results"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync results: {str(e)}"
        )
