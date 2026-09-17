"""
Pydantic schemas for Project LLM Configuration.

One key per provider per project (max 3: openai, anthropic, google).
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class LLMProviderConfigCreate(BaseModel):
    """Schema for adding/updating a provider key for a project."""
    api_key: str = Field(..., min_length=1, description="API key (will be encrypted)")
    preferred_model: Optional[str] = Field(None, description="e.g. gpt-4o-mini, gpt-4o")
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0, description="0.0-2.0")


class LLMProviderConfigUpdate(BaseModel):
    """Schema for updating an existing provider config (key is optional)."""
    api_key: Optional[str] = Field(None, min_length=1, description="New API key (will be encrypted)")
    preferred_model: Optional[str] = Field(None, description="e.g. gpt-4o-mini, gpt-4o")
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0, description="0.0-2.0")


class LLMProviderConfigResponse(BaseModel):
    """Response for a single provider config — never exposes the actual key."""
    id: int
    project_id: int
    provider: str
    preferred_model: Optional[str] = None
    temperature: Optional[float] = None
    is_active: bool
    has_key: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LLMConfigListResponse(BaseModel):
    """List of all provider configs for a project."""
    configs: List[LLMProviderConfigResponse]


class LLMTestResponse(BaseModel):
    """Result of testing an LLM key."""
    success: bool
    message: str
    model_used: Optional[str] = None


class AvailableModel(BaseModel):
    """A single available model."""
    provider: str
    model_id: str
    display_name: str
    type: str = "chat"  # "chat" or "embedding"
    dimensions: Optional[int] = None  # only for embedding models


class AvailableModelsResponse(BaseModel):
    """List of available models grouped by provider."""
    models: List[AvailableModel]


class DiscoveredModel(BaseModel):
    """A model discovered from a provider's API."""
    model_id: str
    display_name: str
    model_type: str = "chat"  # "chat" or "embedding"
    owned_by: Optional[str] = None


class DiscoverModelsResponse(BaseModel):
    """Response from model discovery endpoint."""
    provider: str
    models: List[DiscoveredModel]
    from_api: bool = True  # False when falling back to static list


class SystemProviderStatus(BaseModel):
    """Status of a system LLM provider."""
    provider: str
    available: bool
    has_rate_limits: bool = False


class SystemStatusResponse(BaseModel):
    """Status of all system LLM providers."""
    providers: List[SystemProviderStatus]
