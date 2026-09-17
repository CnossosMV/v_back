"""
Personalization Config Pydantic Schemas
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class PersonalizationConfigCreate(BaseModel):
    """Create / full upsert"""
    enabled: bool = False
    exposed_traits: Optional[List[str]] = None
    expose_scores: bool = False
    expose_name: bool = False
    expose_email: bool = False
    cache_ttl_seconds: int = Field(300, ge=0, le=86400)
    require_analytics_consent: bool = True
    auto_track_spa_pages: bool = False


class PersonalizationConfigUpdate(BaseModel):
    """Partial patch"""
    enabled: Optional[bool] = None
    exposed_traits: Optional[List[str]] = None
    expose_scores: Optional[bool] = None
    expose_name: Optional[bool] = None
    expose_email: Optional[bool] = None
    cache_ttl_seconds: Optional[int] = Field(None, ge=0, le=86400)
    require_analytics_consent: Optional[bool] = None
    auto_track_spa_pages: Optional[bool] = None


class PersonalizationConfigResponse(BaseModel):
    id: int
    project_id: int
    enabled: bool
    exposed_traits: Optional[List[str]] = None
    expose_scores: bool
    expose_name: bool
    expose_email: bool
    cache_ttl_seconds: int
    require_analytics_consent: bool
    auto_track_spa_pages: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VisitorDataTestRequest(BaseModel):
    anonymous_id: str = Field(..., min_length=1, max_length=100)


class VisitorDataResponse(BaseModel):
    anonymous: bool
    traits: Dict[str, Any] = {}
    scores: List[Dict[str, Any]] = []


class AvailableFieldsResponse(BaseModel):
    trait_keys: List[str] = []
    score_definitions: List[Dict[str, str]] = []
