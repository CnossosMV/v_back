"""
Media Assets Router

CRUD for project-scoped media assets (files & links) used by chatbots and agent teams.
Includes file upload and public file serving.
"""
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models import User, Project
from app.models.media_assets import ProjectMediaAsset
from app.schemas.media_assets import (
    MediaAssetCreate, MediaAssetUpdate,
    MediaAssetResponse, MediaAssetListResponse,
)
from app.services.media_asset_service import MediaAssetService
from app.routers.auth import get_current_user

router = APIRouter(tags=["Media Assets"])


# ============================================================================
# Helpers
# ============================================================================

def _get_project(db: Session, project_id: int, user: User) -> Project:
    """Validate project access."""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == user.workspace_id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _asset_to_response(asset: ProjectMediaAsset) -> MediaAssetResponse:
    return MediaAssetResponse(
        id=asset.id,
        project_id=asset.project_id,
        chatbot_id=asset.chatbot_id,
        specialist_id=asset.specialist_id,
        label=asset.label,
        slug=asset.slug,
        description=asset.description,
        media_type=asset.media_type,
        media_url=asset.media_url,
        file_name=asset.file_name,
        mime_type=asset.mime_type,
        tags=asset.tags,
        is_active=asset.is_active,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _infer_media_type(filename: str) -> str:
    """Infer media type from file extension."""
    lower = filename.lower()
    if any(lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
        return "image"
    if any(lower.endswith(ext) for ext in (".mp4", ".avi", ".mov", ".webm", ".mkv")):
        return "video"
    if any(lower.endswith(ext) for ext in (".mp3", ".ogg", ".wav", ".aac", ".flac")):
        return "audio"
    return "document"


# ============================================================================
# Inbound Media Serving (audio, image, video, document from customers)
# ============================================================================

_INBOUND_EXT_MIME = {
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "aac": "audio/aac",
    "webm": "video/webm",
    "3gp": "video/3gpp",
    "mp4": "video/mp4",
}


@router.get("/media/inbound/{project_id}/{date}/{filename}")
async def serve_inbound_media(project_id: int, date: str, filename: str):
    """Serve a media file received from an inbound message (audio, image, video, etc.)."""
    from app.services.inbound.media_storage import MEDIA_ROOT

    file_path = Path(MEDIA_ROOT) / str(project_id) / date / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    content_type, _ = mimetypes.guess_type(str(file_path))
    if not content_type or content_type == "application/octet-stream":
        ext = file_path.suffix.lstrip(".").lower()
        content_type = _INBOUND_EXT_MIME.get(ext) or content_type or "application/octet-stream"

    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        filename=filename,
    )


# ============================================================================
# Public File Serving (no auth — URL shared via WhatsApp)
# ============================================================================

@router.get("/media-assets/files/{project_id}/{filename}")
async def serve_media_file(project_id: int, filename: str):
    """Serve an uploaded media asset file."""
    from app.services.media_asset_service import MEDIA_ASSETS_DIR

    file_path = Path(MEDIA_ASSETS_DIR) / str(project_id) / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    content_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        path=str(file_path),
        media_type=content_type or "application/octet-stream",
        filename=filename,
    )


# ============================================================================
# Email Attachment Upload (file only — no asset record)
# ============================================================================

@router.post("/projects/{project_id}/email-attachments/upload")
async def upload_email_attachment(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Store a file for use as an email attachment and return its URL."""
    from app.services.email_attachments import MAX_ATTACHMENT_BYTES

    _get_project(db, project_id, user)

    content = await file.read()
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB)",
        )

    svc = MediaAssetService(db)
    filename = file.filename or "attachment"
    _, public_url = svc.save_uploaded_file(project_id, content, filename)
    mime = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return {
        "url": public_url,
        "filename": filename,
        "mime_type": mime,
        "size": len(content),
    }


# ============================================================================
# Project-Scoped CRUD
# ============================================================================

@router.get("/projects/{project_id}/media-assets", response_model=MediaAssetListResponse)
async def list_media_assets(
    project_id: int,
    chatbot_id: Optional[int] = Query(None),
    specialist_id: Optional[int] = Query(None),
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    svc = MediaAssetService(db)
    assets = svc.list_assets(
        project_id=project_id,
        chatbot_id=chatbot_id,
        specialist_id=specialist_id,
        active_only=active_only,
    )
    return MediaAssetListResponse(assets=[_asset_to_response(a) for a in assets])


@router.get("/projects/{project_id}/media-assets/{asset_id}", response_model=MediaAssetResponse)
async def get_media_asset(
    project_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    svc = MediaAssetService(db)
    asset = svc.get_asset(project_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Media asset not found")
    return _asset_to_response(asset)


@router.post("/projects/{project_id}/media-assets", response_model=MediaAssetResponse, status_code=201)
async def create_media_asset(
    project_id: int,
    data: MediaAssetCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    svc = MediaAssetService(db)

    # Check slug uniqueness
    existing = svc.get_asset_by_slug(project_id, data.slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Slug '{data.slug}' already exists in this project")

    asset = svc.create_asset(
        project_id=project_id,
        label=data.label,
        slug=data.slug,
        description=data.description,
        media_type=data.media_type,
        media_url=data.media_url,
        tags=data.tags,
        chatbot_id=data.chatbot_id,
        specialist_id=data.specialist_id,
        is_active=data.is_active,
    )
    return _asset_to_response(asset)


@router.post("/projects/{project_id}/media-assets/upload", response_model=MediaAssetResponse, status_code=201)
async def upload_media_asset(
    project_id: int,
    file: UploadFile = File(...),
    label: str = Form(...),
    slug: str = Form(...),
    description: Optional[str] = Form(None),
    chatbot_id: Optional[int] = Form(None),
    specialist_id: Optional[int] = Form(None),
    tags: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    svc = MediaAssetService(db)

    # Check slug uniqueness
    existing = svc.get_asset_by_slug(project_id, slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Slug '{slug}' already exists in this project")

    # Read file content
    content = await file.read()
    filename = file.filename or "uploaded_file"
    file_path, public_url = svc.save_uploaded_file(project_id, content, filename)

    # Detect media type from file
    media_type = _infer_media_type(filename)
    mime = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",")] if tags else None

    asset = svc.create_asset(
        project_id=project_id,
        label=label,
        slug=slug,
        description=description,
        media_type=media_type,
        media_url=public_url,
        file_path=file_path,
        file_name=filename,
        mime_type=mime,
        tags=tag_list,
        chatbot_id=chatbot_id,
        specialist_id=specialist_id,
    )
    return _asset_to_response(asset)


@router.put("/projects/{project_id}/media-assets/{asset_id}", response_model=MediaAssetResponse)
async def update_media_asset(
    project_id: int,
    asset_id: int,
    data: MediaAssetUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    svc = MediaAssetService(db)
    asset = svc.get_asset(project_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Media asset not found")

    # Check slug uniqueness if changing
    if data.slug and data.slug != asset.slug:
        existing = svc.get_asset_by_slug(project_id, data.slug)
        if existing:
            raise HTTPException(status_code=409, detail=f"Slug '{data.slug}' already exists in this project")

    update_fields = data.model_dump(exclude_unset=True)
    asset = svc.update_asset(asset, **update_fields)
    return _asset_to_response(asset)


@router.delete("/projects/{project_id}/media-assets/{asset_id}")
async def delete_media_asset(
    project_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    svc = MediaAssetService(db)
    asset = svc.get_asset(project_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Media asset not found")

    svc.delete_asset(asset)
    return {"detail": "Media asset deleted"}
