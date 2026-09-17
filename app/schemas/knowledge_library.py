"""Pydantic schemas for Knowledge Asset Library."""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any


# ============================================================================
# Assets
# ============================================================================

class AssetCreate(BaseModel):
    asset_type: str  # document/url/faq/snippet/text/image/video/audio
    name: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    language: Optional[str] = None
    source_data: Dict[str, Any] = Field(default_factory=dict)
    status: Optional[str] = "active"
    usage_mode: Optional[str] = None  # rag/direct/both
    slug: Optional[str] = None


class AssetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    language: Optional[str] = None
    source_data: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    usage_mode: Optional[str] = None
    slug: Optional[str] = None


class AssetResponse(BaseModel):
    id: int
    project_id: int
    asset_type: str
    name: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    language: Optional[str] = None
    source_data: Dict[str, Any] = Field(default_factory=dict)
    processing_status: str
    processing_error: Optional[str] = None
    chunk_count: int
    embedding_model: Optional[str] = None
    last_processed_at: Optional[datetime] = None
    content_hash: Optional[str] = None
    version: int
    previous_version_id: Optional[int] = None
    status: str
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    collection_ids: List[int] = Field(default_factory=list)
    usage_mode: str = 'rag'
    slug: Optional[str] = None
    storage_key: Optional[str] = None
    file_size: Optional[int] = None

    class Config:
        from_attributes = True


class AssetListResponse(BaseModel):
    items: List[AssetResponse]
    total: int
    page: int
    page_size: int


# ============================================================================
# Collections
# ============================================================================

class CollectionCreate(BaseModel):
    name: str
    description: Optional[str] = None
    visibility: str = "selective"  # all/selective


class CollectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[str] = None


class CollectionResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    visibility: str
    asset_count: int = 0
    consumer_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CollectionDetailResponse(CollectionResponse):
    assets: List[AssetResponse] = Field(default_factory=list)


class CollectionAssetsUpdate(BaseModel):
    asset_ids: List[int]


# ============================================================================
# Consumer Bindings
# ============================================================================

class BindingCreate(BaseModel):
    collection_id: int
    permission: str = "rag"  # rag/suggest/browse


class BindingUpdate(BaseModel):
    permission: str


class BindingResponse(BaseModel):
    id: int
    project_id: int
    consumer_type: str
    consumer_id: int
    collection_id: int
    collection_name: Optional[str] = None
    permission: str
    created_at: datetime

    class Config:
        from_attributes = True


class EffectiveCollectionResponse(BaseModel):
    collection_id: int
    collection_name: str
    visibility: str
    permission: str
    source: str  # "explicit" or "all_visibility"
    asset_count: int = 0


# ============================================================================
# Search & Retrieval
# ============================================================================

class SearchRequest(BaseModel):
    query: str
    collection_ids: Optional[List[int]] = None
    consumer_type: Optional[str] = None
    consumer_id: Optional[int] = None
    types: Optional[List[str]] = None
    limit: int = 10
    min_score: float = 0.0


class SearchResult(BaseModel):
    asset_id: int
    asset_name: str
    asset_type: str
    chunk_text: str
    score: float
    collection_ids: List[int] = Field(default_factory=list)
    source_data: Dict[str, Any] = Field(default_factory=dict)
    usage_mode: str = 'rag'


class RetrieveRequest(BaseModel):
    query: str
    consumer_type: str
    consumer_id: int
    k: int = 5
    min_score: float = 0.3
    mode: str = "chunks"  # chunks/full_text


class RetrieveResult(BaseModel):
    results: List[SearchResult]
    mode: str
