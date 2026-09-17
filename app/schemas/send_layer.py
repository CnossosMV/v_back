"""
Pydantic schemas for the Send Layer.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ── Outbound content ─────────────────────────────────────────────────────

class OutboundContentSchema(BaseModel):
    content_type: str = "text"
    text: Optional[str] = None
    html: Optional[str] = None
    subject: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    media_caption: Optional[str] = None
    template_name: Optional[str] = None
    template_language: Optional[str] = None
    template_components: Optional[List[Dict[str, Any]]] = None
    buttons: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None


# ── Send request ─────────────────────────────────────────────────────────

class SendRequest(BaseModel):
    user_id: Optional[int] = None
    recipient: str
    content: OutboundContentSchema
    channel: Optional[str] = None
    fallback_order: Optional[List[str]] = None
    on_channel_unavailable: Optional[str] = "try_fallback"
    source_type: str = "manual"
    source_id: Optional[int] = None
    instance_config: Optional[Dict[str, Any]] = None
    scheduled_at: Optional[datetime] = None
    skip_policy: bool = False


# ── Send log response ────────────────────────────────────────────────────

class DeliveryStatusEventResponse(BaseModel):
    id: int
    status: str
    provider_status: Optional[str] = None
    provider_timestamp: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class SendLogResponse(BaseModel):
    id: int
    project_id: int
    user_id: Optional[int] = None
    channel: str
    recipient: str
    content_type: str
    content_summary: Optional[str] = None
    source_type: str
    source_id: Optional[int] = None
    decision_trace: Optional[List[Dict[str, Any]]] = None

    @field_validator("decision_trace", mode="before")
    @classmethod
    def wrap_dict_in_list(cls, v):
        if isinstance(v, dict):
            return [v]
        return v

    preferred_channel: Optional[str] = None
    resolved_channel: Optional[str] = None
    fallback_order: Optional[List[str]] = None
    fallback_attempt: int = 0
    status: str
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    queued_at: datetime
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    deferred_source_enrollment_id: Optional[int] = None
    tracking_token: Optional[str] = None
    opened_at: Optional[datetime] = None
    open_count: int = 0
    click_count: int = 0
    first_click_at: Optional[datetime] = None
    instance_id: Optional[int] = None
    template_id: Optional[int] = None
    template_name: Optional[str] = None
    priority: int = 0
    retry_of_id: Optional[int] = None
    intent_tier: Optional[int] = None
    intent_class: Optional[str] = None
    attention_scope: Optional[str] = None
    purpose_key: Optional[str] = None
    attention_policy_snapshot: Optional[Dict[str, Any]] = None
    planning_status: Optional[str] = None
    planning_evaluated_at: Optional[datetime] = None
    planning_horizon_end: Optional[datetime] = None
    planning_snapshot: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class SendLogDetailResponse(SendLogResponse):
    content_payload: Optional[Dict[str, Any]] = None
    render_context: Optional[Dict[str, Any]] = None
    provider_response: Optional[Dict[str, Any]] = None
    delivery_events: List[DeliveryStatusEventResponse] = []
    deferred_funnel_id: Optional[int] = None


class DeferredSendStatsResponse(BaseModel):
    deferred: int = 0
    expired: int = 0
    skipped: int = 0
    sent: int = 0
    total: int = 0


class SendLogClickResponse(BaseModel):
    id: int
    link_index: int
    original_url: str
    click_count: int
    first_click_at: Optional[datetime] = None
    last_click_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SendLogListResponse(BaseModel):
    items: List[SendLogResponse]
    total: int
    page: int
    page_size: int


# ── Project send config ──────────────────────────────────────────────────

class ProjectSendConfigResponse(BaseModel):
    id: int
    project_id: int
    default_strategy: str
    default_fallback_order: List[str]
    rate_limit_messages: int
    rate_limit_window_minutes: int
    quiet_hours_enabled: bool
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    quiet_hours_timezone: str
    quiet_hours_action: str
    use_send_layer: bool = True
    soft_bounce_threshold: int = 3
    soft_bounce_window_days: int = 7
    send_windows: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProjectSendConfigUpdate(BaseModel):
    default_strategy: Optional[str] = None
    default_fallback_order: Optional[List[str]] = None
    rate_limit_messages: Optional[int] = None
    rate_limit_window_minutes: Optional[int] = None
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    quiet_hours_timezone: Optional[str] = None
    quiet_hours_action: Optional[str] = None
    use_send_layer: Optional[bool] = None
    soft_bounce_threshold: Optional[int] = None
    soft_bounce_window_days: Optional[int] = None
    send_windows: Optional[Dict[str, Any]] = None

    @field_validator("send_windows")
    @classmethod
    def _validate_send_windows(cls, v):
        if v is None:
            return v
        from app.services.channels.send_windows import validate_send_windows
        errors = validate_send_windows(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v


# ── Channel capabilities ─────────────────────────────────────────────────

class ChannelCapabilityResponse(BaseModel):
    id: int
    channel: str
    display_name: str
    max_text_length: Optional[int] = None
    supports_media: bool
    supported_media_types: Optional[List[str]] = None
    max_media_size_mb: Optional[int] = None
    supports_buttons: bool
    max_buttons: Optional[int] = None
    supports_templates: bool
    supports_rich_text: bool
    supports_reactions: bool
    has_session_window: bool
    session_window_hours: Optional[int] = None
    requires_opt_in: bool
    supports_read_receipts: bool
    supported_statuses: Optional[List[str]] = None
    icon_hint: Optional[str] = None
    is_inbound_capable: bool = False
    inbound_requires_setup: bool = False

    class Config:
        from_attributes = True


class ChannelInstanceSummary(BaseModel):
    id: int
    name: Optional[str] = None
    provider_type: str
    status: str
    phone: Optional[str] = None
    is_bidirectional: Optional[bool] = None
    from_domain: Optional[str] = None
    inbound_addresses: Optional[List[dict]] = None


class ChannelRegistryEntry(BaseModel):
    channel: str
    display_name: str
    icon_hint: Optional[str] = None
    supported_statuses: List[str] = []
    enabled: bool = True
    available: bool
    instance_count: int
    instances: List[ChannelInstanceSummary] = []
    capabilities: ChannelCapabilityResponse
    is_inbound_capable: bool = False
    inbound_requires_setup: bool = False


class ChannelRegistryResponse(BaseModel):
    channels: List[ChannelRegistryEntry]


# ── Send log stats ──────────────────────────────────────────────────────

class StatusCount(BaseModel):
    status: str
    count: int

class ChannelCount(BaseModel):
    channel: str
    count: int

class SourceTypeCount(BaseModel):
    source_type: str
    count: int

class TimeBucket(BaseModel):
    period: str
    count: int

class SendLogStatsResponse(BaseModel):
    total: int = 0
    by_status: List[StatusCount] = []
    by_channel: List[ChannelCount] = []
    by_source_type: List[SourceTypeCount] = []
    delivery_rate: float = 0.0
    open_rate: float = 0.0
    failure_rate: float = 0.0
    over_time: List[TimeBucket] = []


# ── Post-message events ────────────────────────────────────────────────

class DeliveryFeedbackResponse(BaseModel):
    id: int
    channel: str
    recipient: str
    feedback_type: str
    reason: Optional[str] = None
    provider: Optional[str] = None
    provider_code: Optional[str] = None
    action_taken: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class PostMessageEventResponse(BaseModel):
    id: int
    event_name: str
    properties: Optional[Dict[str, Any]] = None
    source: str
    created_at: datetime

    class Config:
        from_attributes = True


class PostMessageEventsListResponse(BaseModel):
    items: List[PostMessageEventResponse]
    total: int
    send_log_id: int
    user_id: Optional[int] = None
    since: Optional[datetime] = None


# ── Decision view (Phase 2) ──────────────────────────────────────────────

class DecisionTimelineItem(BaseModel):
    """One outbound decision for a contact: what was selected/sent/held/
    superseded and the reasoning trace behind it."""
    id: int
    channel: Optional[str] = None
    resolved_channel: Optional[str] = None
    recipient: str
    status: str
    source_type: str
    source_id: Optional[int] = None
    content_summary: Optional[str] = None
    intent_class: Optional[str] = None
    intent_tier: Optional[int] = None
    priority: int = 0
    superseded_at: Optional[datetime] = None
    superseded_reason: Optional[str] = None
    error_message: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    queued_at: datetime
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    deferred_source_enrollment_id: Optional[int] = None
    retry_of_id: Optional[int] = None
    decision_trace: Optional[List[Dict[str, Any]]] = None

    @field_validator("decision_trace", mode="before")
    @classmethod
    def _wrap_trace(cls, v):
        if isinstance(v, dict):
            return [v]
        return v

    class Config:
        from_attributes = True


class DecisionTimelineResponse(BaseModel):
    items: List[DecisionTimelineItem]
    total: int
    contact_id: int
    recipient: Optional[str] = None
