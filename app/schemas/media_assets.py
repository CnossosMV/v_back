"""
Pydantic schemas for Project Media Assets.

Covers CRUD operations, file uploads, and asset catalog building.
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ============================================================================
# Asset Schemas
# ============================================================================

class MediaAssetCreate(BaseModel):
    """Create a new media asset (URL-based)."""
    label: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-z0-9\-]+$')
    description: Optional[str] = None
    media_type: str = Field(..., description="image, video, audio, document, link")
    media_url: Optional[str] = Field(None, max_length=1000)
    tags: Optional[List[str]] = None
    chatbot_id: Optional[int] = None
    specialist_id: Optional[int] = None
    is_active: bool = True


class MediaAssetUpdate(BaseModel):
    """Update an existing media asset."""
    label: Optional[str] = Field(None, min_length=1, max_length=200)
    slug: Optional[str] = Field(None, min_length=1, max_length=100, pattern=r'^[a-z0-9\-]+$')
    description: Optional[str] = None
    media_type: Optional[str] = None
    media_url: Optional[str] = Field(None, max_length=1000)
    tags: Optional[List[str]] = None
    is_active: Optional[bool] = None


class MediaAssetResponse(BaseModel):
    """Response for a single media asset."""
    id: int
    project_id: int
    chatbot_id: Optional[int] = None
    specialist_id: Optional[int] = None
    label: str
    slug: str
    description: Optional[str] = None
    media_type: str
    media_url: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    tags: Optional[List[str]] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MediaAssetListResponse(BaseModel):
    """List of media assets."""
    assets: List[MediaAssetResponse]
