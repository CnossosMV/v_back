"""
Pydantic schemas for Webhook Sources & Ingests.
"""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any


# ── WebhookSource ─────────────────────────────────────────────────────


class WebhookSourceCreate(BaseModel):
    source_slug: str = Field(..., max_length=100, pattern=r'^[a-z0-9_-]+$')
    display_name: str = Field(..., max_length=200)
    source_type: str = Field(default="custom")  # built_in / custom
    transformer_config: Optional[Dict[str, Any]] = None
    rate_limit_per_minute: Optional[int] = None


class WebhookSourceUpdate(BaseModel):
    display_name: Optional[str] = None
    status: Optional[str] = None  # active / paused / disabled
    transformer_config: Optional[Dict[str, Any]] = None
    rate_limit_per_minute: Optional[int] = None


class WebhookSourceResponse(BaseModel):
    id: int
    project_id: int
    source_slug: str
    display_name: str
    source_type: str
    status: str
    transformer_config: Optional[Dict[str, Any]] = None
    rate_limit_per_minute: Optional[int] = None
    last_received_at: Optional[datetime] = None
    total_received: int = 0
    total_failed: int = 0
    endpoint_url: Optional[str] = None  # Computed by router
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WebhookSourceCreatedResponse(WebhookSourceResponse):
    """Returned only on creation — includes the plaintext secret once."""
    secret: str


# ── WebhookIngest ─────────────────────────────────────────────────────


class WebhookIngestResponse(BaseModel):
    id: int
    project_id: int
    source_id: int
    source_slug: str
    received_at: datetime
    signature_valid: Optional[bool] = None
    idempotency_key: Optional[str] = None
    processing_status: str
    error_message: Optional[str] = None
    retry_count: int = 0
    next_retry_at: Optional[datetime] = None
    resulting_event_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WebhookIngestDetailResponse(WebhookIngestResponse):
    """Includes raw payload and headers for detail view."""
    headers: Optional[Dict[str, Any]] = None
    raw_payload: Optional[Dict[str, Any]] = None


class WebhookIngestListResponse(BaseModel):
    items: List[WebhookIngestResponse]
    total: int
    page: int
    page_size: int


# ── Test / Retry ──────────────────────────────────────────────────────


class TestWebhookRequest(BaseModel):
    headers: Optional[Dict[str, str]] = None
    payload: Dict[str, Any]


class TestWebhookResponse(BaseModel):
    signature_valid: bool
    transformed_event: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    contact_ref: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class BulkRetryRequest(BaseModel):
    source_slug: Optional[str] = None  # null = retry all failed


class BulkRetryResponse(BaseModel):
    queued_count: int
