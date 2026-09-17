"""
Messaging Middleware Pydantic Schemas
"""
from pydantic import BaseModel, Field, EmailStr, model_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from enum import Enum

from app.schemas.orchestration_attention import AttentionPolicyInput


# ==========================================
# Enums (matching SQLAlchemy enums)
# ==========================================

class ChannelType(str, Enum):
    email = "email"
    sms = "sms"
    whatsapp = "whatsapp"
    inapp = "inapp"
    push = "push"
    webhook = "webhook"


class AuthType(str, Enum):
    none = "none"
    bearer = "bearer"
    basic = "basic"
    api_key = "api_key"
    custom_header = "custom_header"


class MessageStatus(str, Enum):
    pending = "pending"
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    bounced = "bounced"


# ==========================================
# Messaging Domain Schemas
# ==========================================

class MessagingDomainBase(BaseModel):
    domain: str = Field(..., min_length=1, max_length=255, description="Domain name (e.g., example.com)")


class MessagingDomainCreate(MessagingDomainBase):
    allowed_origins: Optional[List[str]] = None


class MessagingDomainUpdate(BaseModel):
    domain: Optional[str] = Field(None, min_length=1, max_length=255)
    allowed_origins: Optional[List[str]] = None
    is_active: Optional[bool] = None


class MessagingDomainResponse(MessagingDomainBase):
    id: int
    project_id: int
    write_key: str
    is_verified: bool
    verification_token: Optional[str] = None
    verified_at: Optional[datetime] = None
    snippet_installed_at: Optional[datetime] = None
    is_active: bool
    allowed_origins: Optional[List[str]] = None
    rate_limit_per_minute: Optional[int] = 100
    rate_limit_per_day: Optional[int] = 10000
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MessagingDomainSnippetResponse(BaseModel):
    domain_id: int
    write_key: str
    snippet: str
    async_snippet: str
    sdk_endpoint: Optional[str] = None
    tracking_hostname: Optional[str] = None


class MessagingDomainVerifyResponse(BaseModel):
    verified: bool
    message: str


class TrackingDomainDnsInstructions(BaseModel):
    cname_name: str
    cname_target: str
    txt_name: str
    txt_value: str


class MessagingTrackingDomainCreate(BaseModel):
    hostname: str = Field(..., min_length=1, max_length=255, description="Tracking hostname, e.g. track.example.com")
    domain_id: Optional[int] = Field(None, description="Messaging domain/write-key to use for SDK ingest")
    cookie_keeper_enabled: bool = True


class MessagingTrackingDomainUpdate(BaseModel):
    domain_id: Optional[int] = None
    cookie_keeper_enabled: Optional[bool] = None
    proxy_status: Optional[str] = Field(None, max_length=50)


class MessagingTrackingDomainResponse(BaseModel):
    id: int
    project_id: int
    domain_id: Optional[int] = None
    hostname: str
    mode: str
    cname_target: str
    verification_token: str
    cloudflare_custom_hostname_id: Optional[str] = None
    dns_status: str
    ssl_status: str
    proxy_status: str
    cookie_keeper_enabled: bool
    last_seen_at: Optional[datetime] = None
    config_version: int
    status_details: Optional[Dict[str, Any]] = None
    dns_instructions: TrackingDomainDnsInstructions
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MessagingTrackingDomainVerifyResponse(BaseModel):
    verified: bool
    dns_status: str
    ssl_status: str
    proxy_status: str
    message: str
    details: Optional[Dict[str, Any]] = None


class TrackingDomainEdgeConfigResponse(BaseModel):
    project_id: int
    tracking_domain_id: int
    hostname: str
    write_key: str
    api_origin: str
    sdk_origin: str
    cookie_domain: Optional[str] = None
    cookie_keeper_enabled: bool
    config_version: int
    proxy_paths: List[str]


class ProxyRequestLogCreate(BaseModel):
    hostname: str
    method: str = Field(..., max_length=12)
    path: str = Field(..., max_length=500)
    action: Optional[str] = Field(None, max_length=50)
    event_id: Optional[str] = Field(None, max_length=255)
    anonymous_id: Optional[str] = Field(None, max_length=100)
    status_code: Optional[int] = None
    click_ids: Optional[Dict[str, Any]] = None
    cookies_refreshed: Optional[List[str]] = None
    request_summary: Optional[Dict[str, Any]] = None


class ProxyRequestLogResponse(BaseModel):
    id: int
    project_id: int
    tracking_domain_id: Optional[int] = None
    hostname: str
    method: str
    path: str
    action: Optional[str] = None
    event_id: Optional[str] = None
    anonymous_id: Optional[str] = None
    status_code: Optional[int] = None
    click_ids: Optional[Dict[str, Any]] = None
    cookies_refreshed: Optional[List[str]] = None
    request_summary: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ==========================================
# Messaging API Key Schemas
# ==========================================

class MessagingApiKeyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class MessagingApiKeyCreate(MessagingApiKeyBase):
    permissions: Optional[List[str]] = ["send", "track", "identify"]
    rate_limit_per_minute: Optional[int] = 1000
    rate_limit_per_day: Optional[int] = 100000
    allowed_ips: Optional[List[str]] = None  # IP allowlist (IPv4/IPv6/CIDR)


class MessagingApiKeyResponse(MessagingApiKeyBase):
    id: int
    project_id: int
    key_prefix: str  # First 8 chars for display
    permissions: List[str]
    rate_limit_per_minute: int
    rate_limit_per_day: int
    allowed_ips: Optional[List[str]] = None  # IP allowlist (IPv4/IPv6/CIDR)
    is_active: bool
    last_used_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MessagingApiKeyCreateResponse(MessagingApiKeyResponse):
    """Response for create - includes the full secret_key (shown only once)"""
    secret_key: str


class MessagingApiKeyRotateResponse(BaseModel):
    """Response for key rotation"""
    id: int
    key_prefix: str
    secret_key: str
    message: str


# ==========================================
# Messaging Channel Schemas
# ==========================================

class MessagingChannelBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-z0-9_-]+$')
    channel_type: ChannelType
    webhook_url: str = Field(..., min_length=1, max_length=500)


class MessagingChannelCreate(MessagingChannelBase):
    auth_type: Optional[AuthType] = AuthType.none
    auth_config: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, str]] = None
    is_default: Optional[bool] = False
    max_retries: Optional[int] = 3
    retry_delay_seconds: Optional[int] = 60
    timeout_seconds: Optional[int] = 30


class MessagingChannelUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    webhook_url: Optional[str] = Field(None, min_length=1, max_length=500)
    auth_type: Optional[AuthType] = None
    auth_config: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, str]] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None
    max_retries: Optional[int] = None
    retry_delay_seconds: Optional[int] = None
    timeout_seconds: Optional[int] = None


class MessagingChannelResponse(MessagingChannelBase):
    id: int
    project_id: int
    auth_type: AuthType
    headers: Optional[Dict[str, str]] = None
    is_default: bool
    is_active: bool
    max_retries: int
    retry_delay_seconds: int
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MessagingChannelTestResponse(BaseModel):
    success: bool
    message: str
    response_status: Optional[int] = None
    response_time_ms: Optional[int] = None


# ==========================================
# Messaging Template Schemas
# ==========================================

class MessagingTemplateBase(BaseModel):
    slug: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-z0-9_-]+$')
    name: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1)


class MessagingTemplateCreate(MessagingTemplateBase):
    channel_id: Optional[int] = None
    channel_type: Optional[str] = "email"
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    reply_to: Optional[str] = None
    folder: Optional[str] = Field(None, max_length=100)
    body_format: Optional[str] = "html"
    media_url: Optional[str] = Field(None, max_length=1000)
    subject: Optional[str] = Field(None, max_length=500)
    metadata: Optional[Dict[str, Any]] = None
    trigger_events: Optional[List[str]] = None
    purpose_key: Optional[str] = Field(
        None, min_length=1, max_length=120,
        pattern=r'^[a-z0-9][a-z0-9._-]*$',
    )
    attention_policy: Optional[AttentionPolicyInput] = None
    # WhatsApp-specific
    meta_template_name: Optional[str] = Field(None, max_length=200)
    meta_language: Optional[str] = Field(None, max_length=20)
    meta_components: Optional[List[Dict[str, Any]]] = None
    whatsapp_instance_id: Optional[int] = None
    # i18n — variant family (rows share slug, differ by locale)
    locale: Optional[str] = Field(None, max_length=10)
    source_locale: Optional[str] = Field(None, max_length=10)
    translation_status: Optional[str] = Field(None, max_length=20)  # source|machine|reviewed


class MessagingTemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    channel_id: Optional[int] = None
    channel_type: Optional[str] = None
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    reply_to: Optional[str] = None
    folder: Optional[str] = Field(None, max_length=100)
    body_format: Optional[str] = None
    media_url: Optional[str] = Field(None, max_length=1000)
    subject: Optional[str] = Field(None, max_length=500)
    body: Optional[str] = Field(None, min_length=1)
    metadata: Optional[Dict[str, Any]] = None
    trigger_events: Optional[List[str]] = None
    is_active: Optional[bool] = None
    automation_enabled: Optional[bool] = None
    purpose_key: Optional[str] = Field(
        None, min_length=1, max_length=120,
        pattern=r'^[a-z0-9][a-z0-9._-]*$',
    )
    attention_policy: Optional[AttentionPolicyInput] = None
    meta_template_name: Optional[str] = Field(None, max_length=200)
    meta_language: Optional[str] = Field(None, max_length=20)
    meta_components: Optional[List[Dict[str, Any]]] = None
    whatsapp_instance_id: Optional[int] = None
    # i18n — locale of a row is fixed at create; translation_status flips on review/publish
    locale: Optional[str] = Field(None, max_length=10)
    source_locale: Optional[str] = Field(None, max_length=10)
    translation_status: Optional[str] = Field(None, max_length=20)


class TemplateLintRequest(BaseModel):
    body: str
    subject: Optional[str] = None


class MessagingTemplateResponse(MessagingTemplateBase):
    id: int
    project_id: int
    channel_id: Optional[int] = None
    channel_type: Optional[str] = "email"
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    reply_to: Optional[str] = None
    folder: Optional[str] = None
    body_format: Optional[str] = "html"
    media_url: Optional[str] = None
    subject: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(None, validation_alias="template_metadata")
    trigger_events: Optional[List[str]] = None
    is_active: bool
    automation_enabled: bool = False
    purpose_key: Optional[str] = None
    attention_policy: Optional[Dict[str, Any]] = None
    # WhatsApp-specific (null for non-whatsapp templates)
    meta_template_name: Optional[str] = None
    meta_language: Optional[str] = None
    meta_components: Optional[List[Dict[str, Any]]] = None
    whatsapp_instance_id: Optional[int] = None
    external_source: Optional[str] = None
    external_last_synced_at: Optional[datetime] = None
    # i18n
    locale: Optional[str] = None
    source_locale: Optional[str] = None
    translation_status: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True


class TemplateFolderResponse(BaseModel):
    folder: str
    count: int


class MessagingTemplatePreviewRequest(BaseModel):
    variables: Dict[str, Any] = {}


class MessagingTemplatePreviewResponse(BaseModel):
    rendered_subject: Optional[str] = None
    rendered_body: str
    variables_used: List[str]
    missing_variables: List[str]


# ==========================================
# Messaging User Schemas
# ==========================================

class MessagingUserBase(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=255)


class MessagingUserCreate(MessagingUserBase):
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)
    properties: Optional[Dict[str, Any]] = None


class MessagingUserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)
    properties: Optional[Dict[str, Any]] = None
    is_subscribed: Optional[bool] = None


class ContactVerificationSnapshot(BaseModel):
    status: str
    provider: str
    provider_key_source: str
    provider_version: Optional[str] = None
    provider_status: Optional[str] = None
    checked_at: datetime
    expires_at: datetime
    is_expired: bool
    last_attempt_status: str
    last_error_code: Optional[str] = None


class MessagingUserResponse(MessagingUserBase):
    id: int
    project_id: int
    email: Optional[str] = None
    phone: Optional[str] = None
    phone_e164: Optional[str] = None
    # WhatsApp number verification (valid / invalid / unverified / checking)
    whatsapp_status: Optional[str] = None
    whatsapp_checked_at: Optional[datetime] = None
    verifications: Dict[str, ContactVerificationSnapshot] = Field(default_factory=dict)
    name: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None
    is_subscribed: bool
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime
    segment_rule_id: Optional[int] = None
    segment_name: Optional[str] = None
    segment_updated_at: Optional[datetime] = None
    # Identity resolution fields
    status: Optional[str] = "active"
    merged_into: Optional[int] = None
    lifecycle_stage: Optional[str] = None
    tags: Optional[List[str]] = None
    primary_channel: Optional[Dict[str, Any]] = None
    created_via: Optional[str] = "api"
    consent_channels: Optional[Dict[str, Any]] = None
    thermal_state: Optional[str] = None
    # i18n / best-time-to-send
    locale: Optional[str] = None
    timezone: Optional[str] = None
    send_windows: Optional[Dict[str, Any]] = None
    # Opt-out state (STOP keyword / unsubscribe link / per-channel)
    global_opt_out: bool = False
    opted_out_channels: Optional[List[str]] = None
    # Automation pause (suppress automated messages while handled manually)
    automations_paused: bool = False
    automations_paused_at: Optional[datetime] = None
    automations_paused_reason: Optional[str] = None
    automations_pause_mode: Optional[str] = None

    class Config:
        from_attributes = True


class PauseAutomationsRequest(BaseModel):
    mode: Literal["hold", "skip"]
    reason: Optional[str] = Field(None, max_length=255)


class SendWindowsUpdateRequest(BaseModel):
    """Per-contact best-time-to-send override; null clears (inherit project)."""
    send_windows: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _validate(self):
        if self.send_windows is not None:
            from app.services.channels.send_windows import validate_send_windows
            errors = validate_send_windows(self.send_windows)
            if errors:
                raise ValueError("; ".join(errors))
        return self


# ==========================================
# Messaging Event Schemas
# ==========================================

class MessagingEventBase(BaseModel):
    event_name: str = Field(..., min_length=1, max_length=255)


class MessagingEventCreate(MessagingEventBase):
    user_id: Optional[int] = None
    anonymous_id: Optional[str] = Field(None, max_length=100)
    properties: Optional[Dict[str, Any]] = None


class MessagingEventResponse(MessagingEventBase):
    id: int
    project_id: int
    user_id: Optional[int] = None
    anonymous_id: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None
    source: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    processed: bool
    processing_notes: Optional[Dict[str, Any]] = None
    external_event_id: Optional[str] = None
    campaign_origin: Optional[str] = None
    attribution: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MessagingEventGroup(BaseModel):
    """A bucket of events sharing the same (user/anonymous, event_name, source) within a time window."""
    user_id: Optional[int] = None
    anonymous_id: Optional[str] = None
    event_name: str
    source: str
    bucket_start: datetime
    first_at: datetime
    last_at: datetime
    count: int
    any_unprocessed: bool
    any_warnings: bool
    events: List[MessagingEventResponse]


class MessagingEventGroupList(BaseModel):
    groups: List[MessagingEventGroup]
    total_groups: int


# ==========================================
# Messaging Log Schemas
# ==========================================

class MessagingLogResponse(BaseModel):
    id: int
    project_id: int
    template_id: Optional[int] = None
    channel_id: Optional[int] = None
    user_id: Optional[int] = None
    event_id: Optional[int] = None
    template_slug: Optional[str] = None
    channel_type: Optional[str] = None
    recipient: str
    rendered_subject: Optional[str] = None
    rendered_body: Optional[str] = None
    status: MessageStatus
    provider_message_id: Optional[str] = None
    provider_response: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    attempt_count: int
    last_attempt_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MessagingLogDetailResponse(MessagingLogResponse):
    """Extended response with related objects"""
    template: Optional[MessagingTemplateResponse] = None
    channel: Optional[MessagingChannelResponse] = None
    user: Optional[MessagingUserResponse] = None
    event: Optional[MessagingEventResponse] = None


# ==========================================
# Public API Schemas (SDK endpoints)
# ==========================================

class IdentifyRequest(BaseModel):
    """Identify a user - used by frontend SDK"""
    user_id: str = Field(..., min_length=1, max_length=255, description="Client's user ID")
    traits: Optional[Dict[str, Any]] = Field(None, description="User properties")
    anonymous_id: Optional[str] = Field(None, max_length=100, description="Anonymous visitor ID from cookie")
    account_id: Optional[str] = Field(None, max_length=255, description="Account/company ID for B2B grouping")
    context: Optional[Dict[str, Any]] = Field(None, description="SDK context (locale, timezone, etc.)")
    device_fingerprint: Optional[str] = Field(None, max_length=20, description="Device fingerprint hash (Tier 5 hint)")
    ads_consent: Optional[Dict[str, Any]] = Field(None, description="Ads consent evidence for ad_user_data/ad_personalization")


class TrackRequest(BaseModel):
    """Track an event - used by frontend SDK"""
    event: str = Field(..., min_length=1, max_length=255, description="Event name")
    properties: Optional[Dict[str, Any]] = Field(None, description="Event properties")
    event_id: Optional[str] = Field(None, max_length=255, description="Client-generated dedupe/event ID")
    user_id: Optional[str] = Field(None, description="Optional user ID if identified")
    anonymous_id: Optional[str] = Field(None, max_length=100, description="Anonymous visitor ID from cookie")
    device_fingerprint: Optional[str] = Field(None, max_length=20, description="Device fingerprint hash (Tier 5 hint)")
    session_id: Optional[str] = Field(None, max_length=100, description="SDK session ID")
    client_ts: Optional[str] = Field(None, description="Client-side ISO timestamp")
    context: Optional[Dict[str, Any]] = Field(None, description="SDK context and attribution hints")
    ads_consent: Optional[Dict[str, Any]] = Field(None, description="Ads consent evidence for ad_user_data/ad_personalization")


class PageRequest(BaseModel):
    """Track a page view - used by frontend SDK"""
    name: Optional[str] = Field(None, max_length=255, description="Page name")
    properties: Optional[Dict[str, Any]] = Field(None, description="Page properties")
    event_id: Optional[str] = Field(None, max_length=255, description="Client-generated dedupe/event ID")
    anonymous_id: Optional[str] = Field(None, max_length=100, description="Anonymous visitor ID from cookie")
    device_fingerprint: Optional[str] = Field(None, max_length=20, description="Device fingerprint hash (Tier 5 hint)")
    session_id: Optional[str] = Field(None, max_length=100, description="SDK session ID")
    client_ts: Optional[str] = Field(None, description="Client-side ISO timestamp")
    context: Optional[Dict[str, Any]] = Field(None, description="SDK context and attribution hints")
    ads_consent: Optional[Dict[str, Any]] = Field(None, description="Ads consent evidence for ad_user_data/ad_personalization")


class SendRequest(BaseModel):
    """Send a message directly - used by backend API"""
    template_slug: str = Field(..., min_length=1, max_length=100)
    recipient: str = Field(..., min_length=1, max_length=255, description="Email, phone, or identifier")
    variables: Optional[Dict[str, Any]] = Field(None, description="Template variables")
    user_id: Optional[str] = Field(None, description="Optional user ID")
    channel_slug: Optional[str] = Field(None, description="Override default channel")


class SendResponse(BaseModel):
    success: bool
    message_id: Optional[int] = None
    status: MessageStatus
    error: Optional[str] = None


class VerifyInstallRequest(BaseModel):
    """Verify SDK installation"""
    pass


class VerifyInstallResponse(BaseModel):
    verified: bool
    domain: str
    project_id: int
    message: str


class EmailPermissionClaim(BaseModel):
    """Auditable permission asserted by an authenticated backend integration."""

    permission_type: Literal["marketing"] = "marketing"
    status: Literal["granted", "denied", "withdrawn"]
    source: str = Field(..., min_length=1, max_length=100)
    captured_at: datetime
    policy_version: Optional[str] = Field(None, max_length=100)
    evidence_ref: str = Field(..., min_length=1, max_length=255)
    expires_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None


class BackendEventRequest(BaseModel):
    """Track event from backend - used by secret key auth"""
    event: str = Field(..., min_length=1, max_length=255)
    event_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=255,
        description="Stable source occurrence ID used for idempotent retries",
    )
    user_id: Optional[str] = Field(None, min_length=1, max_length=255)
    anonymous_id: Optional[str] = Field(None, max_length=100)
    properties: Optional[Dict[str, Any]] = None
    contact_properties: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Current source-owned contact facts. These are deep-merged into the "
            "contact profile; event properties remain occurrence-only."
        ),
    )
    timestamp: Optional[datetime] = None
    session_id: Optional[str] = Field(None, max_length=100)
    client_ts: Optional[str] = Field(None)
    context: Optional[Dict[str, Any]] = None
    ads_consent: Optional[Dict[str, Any]] = None
    email_permission: Optional[EmailPermissionClaim] = None


    @model_validator(mode="after")
    def require_identity(self):
        if not self.user_id and not self.anonymous_id:
            raise ValueError("user_id or anonymous_id is required")
        return self

class BackendUserRequest(BaseModel):
    """Create/update user from backend - used by secret key auth"""
    user_id: str = Field(..., min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)
    properties: Optional[Dict[str, Any]] = None
    ads_consent: Optional[Dict[str, Any]] = None
    email_permission: Optional[EmailPermissionClaim] = None


# ==========================================
# Send Test Message Schema
# ==========================================

class SendTestRequest(BaseModel):
    template_id: int
    recipient: str = Field(..., min_length=1, max_length=2000)
    variables: Optional[Dict[str, Any]] = None
    channel_id: Optional[int] = None
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    reply_to: Optional[str] = None
    smtp_config_id: Optional[int] = None


class SendTestResponse(BaseModel):
    success: bool
    message: str
    log_id: Optional[int] = None
    rendered_subject: Optional[str] = None
    rendered_body: Optional[str] = None


# ==========================================
# List/Filter Schemas
# ==========================================

class PaginationParams(BaseModel):
    skip: int = 0
    limit: int = 50


class MessagingUserFilter(PaginationParams):
    search: Optional[str] = None  # Search by email, name, external_id
    is_subscribed: Optional[bool] = None


class MessagingEventFilter(PaginationParams):
    event_name: Optional[str] = None
    user_id: Optional[int] = None
    processed: Optional[bool] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


class MessagingLogFilter(PaginationParams):
    status: Optional[MessageStatus] = None
    template_id: Optional[int] = None
    channel_id: Optional[int] = None
    user_id: Optional[int] = None
    recipient: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


# ==========================================
# Overview/Stats Schemas
# ==========================================

class MessagingOverviewStats(BaseModel):
    total_users: int
    total_events_today: int
    total_messages_sent: int
    total_messages_delivered: int
    total_messages_failed: int
    delivery_rate: float  # percentage
    active_templates: int
    active_channels: int


class MessagingDailyStats(BaseModel):
    date: str  # YYYY-MM-DD
    events: int
    messages_sent: int
    messages_delivered: int
    messages_failed: int


# ==========================================
# Consent Schemas (GDPR/LGPD)
# ==========================================

class ConsentRequest(BaseModel):
    """Request to update user consent"""
    marketing: Optional[bool] = None
    analytics: Optional[bool] = None


class ConsentResponse(BaseModel):
    """Response with current consent state"""
    marketing: bool
    analytics: bool
    ad_user_data: bool = False
    ad_personalization: bool = False
    given_at: Optional[datetime] = None
    version: Optional[str] = None


class SetConsentRequest(BaseModel):
    """SDK request to set consent"""
    marketing: Optional[bool] = None
    analytics: Optional[bool] = None
    user_id: Optional[str] = None  # Optional, can use anonymous_id
    ads_consent: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    policy_version: Optional[str] = None
    evidence_id: Optional[str] = None
    page_url: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class SetConsentResponse(BaseModel):
    """SDK response after setting consent"""
    success: bool
    consent: ConsentResponse


# ==========================================
# Alias/Identity Resolution Schemas
# ==========================================

class AliasRequest(BaseModel):
    """Request to merge anonymous profile to user"""
    previous_id: str = Field(..., description="Anonymous ID to merge from")
    user_id: str = Field(..., description="User ID to merge to")


class AliasResponse(BaseModel):
    """Response after alias/merge"""
    success: bool
    merged: bool
    events_reassigned: int = 0


# ==========================================
# Anonymous Profile Schemas
# ==========================================

class MessagingAnonymousProfileResponse(BaseModel):
    """Response for anonymous profile"""
    id: int
    project_id: int
    anonymous_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    merged_to_user_id: Optional[int] = None
    merged_at: Optional[datetime] = None
    properties: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ==========================================
# Extended User Response with Consent
# ==========================================

class MessagingUserWithConsentResponse(MessagingUserResponse):
    """Extended user response including consent fields"""
    email_hash: Optional[str] = None
    phone_hash: Optional[str] = None
    consent_marketing: Optional[bool] = False
    consent_analytics: Optional[bool] = False
    consent_given_at: Optional[datetime] = None
    consent_version: Optional[str] = None


# ==========================================
# DSAR (Data Subject Access Request) Schemas
# ==========================================

class DSARRequestType(str, Enum):
    export = "export"
    delete = "delete"
    rectify = "rectify"
    withdraw_consent = "withdraw_consent"


class DSARRequestStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class DSARCreateRequest(BaseModel):
    """Create a DSAR request (public API)"""
    request_type: DSARRequestType
    email: Optional[str] = None
    external_id: Optional[str] = None
    updates: Optional[Dict[str, Any]] = None  # For rectify requests


class DSARProcessRequest(BaseModel):
    """Process a pending DSAR request (admin API)"""
    updates: Optional[Dict[str, Any]] = None  # For rectify requests


class DSARRequestResponse(BaseModel):
    """Response for a DSAR request"""
    id: int
    project_id: int
    request_type: DSARRequestType
    user_id: Optional[int] = None
    external_id: Optional[str] = None
    status: DSARRequestStatus
    requested_at: datetime
    completed_at: Optional[datetime] = None
    result_url: Optional[str] = None
    result_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    requested_by_ip: Optional[str] = None
    processed_by_user_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DSARCreateResponse(BaseModel):
    """Response after creating a DSAR request"""
    success: bool
    request_id: int
    status: DSARRequestStatus
    message: str


class DSARExportResponse(BaseModel):
    """Response for export request with data"""
    success: bool
    request_id: int
    data: Optional[Dict[str, Any]] = None
    message: str


# ==========================================
# Event Schema Schemas
# ==========================================

class EventSchemaPropertyDef(BaseModel):
    """Definition for a single property in an event schema"""
    type: str = Field(..., description="JSON Schema type: string, number, integer, boolean, array, object")
    description: Optional[str] = None


class EventSchemaCreate(BaseModel):
    """Create a new event schema"""
    event_name: str = Field(..., min_length=1, max_length=255, pattern=r'^[a-z_][a-z0-9_]*$')
    display_name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    category: Optional[str] = Field(None, max_length=100)
    properties_schema: Optional[Dict[str, Any]] = None  # JSON Schema format
    required_properties: Optional[List[str]] = None


class EventSchemaUpdate(BaseModel):
    """Update an event schema"""
    display_name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    category: Optional[str] = Field(None, max_length=100)
    properties_schema: Optional[Dict[str, Any]] = None
    required_properties: Optional[List[str]] = None
    is_active: Optional[bool] = None


class EventSchemaResponse(BaseModel):
    """Response for an event schema"""
    id: int
    project_id: int
    event_name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    properties_schema: Optional[Dict[str, Any]] = None
    required_properties: Optional[List[str]] = None
    is_standard: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EventSchemaValidationResult(BaseModel):
    """Result of event validation"""
    valid: bool
    errors: List[str] = []


class ImportStandardEventsResponse(BaseModel):
    """Response after importing standard events"""
    success: bool
    imported_count: int
    skipped_count: int
    events: List[str] = []


# ==========================================
# Destination Schemas
# ==========================================

class DestinationType(str, Enum):
    ga4 = "ga4"
    meta_pixel = "meta_pixel"
    google_ads = "google_ads"
    linkedin = "linkedin"
    tiktok = "tiktok"
    custom = "custom"


class DestinationCreate(BaseModel):
    """Create a new destination"""
    destination_type: DestinationType
    name: str = Field(..., min_length=1, max_length=255)
    config: Dict[str, Any]  # Will be encrypted
    consent_required: Optional[List[str]] = None  # e.g., ['analytics', 'marketing']


class DestinationUpdate(BaseModel):
    """Update a destination"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    config: Optional[Dict[str, Any]] = None
    consent_required: Optional[List[str]] = None
    is_active: Optional[bool] = None


class DestinationResponse(BaseModel):
    """Response for a destination (config not included)"""
    id: int
    project_id: int
    destination_type: DestinationType
    name: str
    is_active: bool
    consent_required: Optional[List[str]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DestinationDetailResponse(DestinationResponse):
    """Response for destination with decrypted config"""
    config: Optional[Dict[str, Any]] = None


class DestinationTemplateField(BaseModel):
    """Field definition in destination template"""
    type: str
    required: bool = False
    sensitive: bool = False
    description: Optional[str] = None
    default: Optional[Any] = None


class DestinationTemplate(BaseModel):
    """Destination configuration template"""
    name: str
    fields: Dict[str, DestinationTemplateField]
    default_consent: List[str] = []


# ==========================================
# Audience Sync Schemas
# ==========================================

class AudienceProviderType(str, Enum):
    meta_pixel = "meta_pixel"
    google_ads = "google_ads"
    tiktok = "tiktok"


class AudienceRuleFilter(BaseModel):
    field: str
    operator: str = "equals"
    value: Optional[Any] = None
    within_days: Optional[int] = None


class AudienceRuleConfig(BaseModel):
    match: str = "all"
    filters: List[AudienceRuleFilter] = []


class MessagingAudienceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    source_type: str = "dynamic_rule"
    source_config: Optional[Dict[str, Any]] = None
    rule_config: Optional[Dict[str, Any]] = None
    status: str = "active"
    refresh_mode: str = "manual"


class MessagingAudienceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    source_type: Optional[str] = None
    source_config: Optional[Dict[str, Any]] = None
    rule_config: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    refresh_mode: Optional[str] = None


class MessagingAudienceResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    source_type: str = "dynamic_rule"
    source_config: Optional[Dict[str, Any]] = None
    rule_config: Optional[Dict[str, Any]] = None
    status: str
    refresh_mode: str
    created_by_user_id: Optional[int] = None
    last_evaluated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MessagingAudienceDestinationCreate(BaseModel):
    provider_type: AudienceProviderType
    destination_id: Optional[int] = None
    external_audience_id: Optional[str] = None
    external_audience_name: Optional[str] = None
    sync_mode: str = "add_remove"
    is_active: bool = True
    config: Optional[Dict[str, Any]] = None


class MessagingAudienceDestinationUpdate(BaseModel):
    destination_id: Optional[int] = None
    external_audience_id: Optional[str] = None
    external_audience_name: Optional[str] = None
    sync_mode: Optional[str] = None
    is_active: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None


class MessagingAudienceDestinationResponse(BaseModel):
    id: int
    project_id: int
    audience_id: int
    destination_id: Optional[int] = None
    provider_type: str
    external_audience_id: Optional[str] = None
    external_audience_name: Optional[str] = None
    sync_mode: str
    is_active: bool
    last_sync_status: Optional[str] = None
    last_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    provider_ready: bool = Field(False, description="True when the provider-side audience/list ID required for sync is configured")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AudienceEligibilitySample(BaseModel):
    user_id: int
    external_id: str
    display: Optional[str] = None
    state: str
    reason: Optional[str] = None
    identifiers_present: Dict[str, bool] = {}


class MessagingAudiencePreviewResponse(BaseModel):
    total_users: int
    matched: int
    eligible: int
    missing_consent: int
    missing_identifier: int
    opted_out: int
    excluded: int
    samples: List[AudienceEligibilitySample] = []


class AudienceCsvImportPreviewResponse(BaseModel):
    headers: List[str]
    sample_rows: List[List[str]] = []
    total_rows: int
    suggested_mapping: Dict[str, str] = {}
    matched_existing: int = 0
    will_create: int = 0
    missing_identifier: int = 0


class AudienceImportResultResponse(BaseModel):
    audience: MessagingAudienceResponse
    total_rows: int
    matched_existing: int
    created: int
    updated: int
    skipped: int
    eligible: int
    excluded: int
    missing_consent: int
    missing_identifier: int
    errors: List[Dict[str, Any]] = []


class AudienceProjectImportPreviewRequest(BaseModel):
    source_project_id: int


class AudienceProjectImportRequest(BaseModel):
    source_project_id: int
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    consent_source: str = Field(..., min_length=1, max_length=100)
    policy_version: str = Field(..., min_length=1, max_length=100)
    evidence_id: Optional[str] = None
    evidence_url: Optional[str] = None
    confirm_ads_consent: bool = False
    overwrite_existing: bool = False


class AudienceProjectImportPreviewResponse(BaseModel):
    source_project_id: int
    source_project_name: str
    total_contacts: int
    matched_existing: int
    will_create: int


class MessagingAudienceMembershipResponse(BaseModel):
    id: int
    project_id: int
    audience_id: int
    user_id: int
    state: str
    eligibility_reason: Optional[str] = None
    eligibility_details: Optional[Dict[str, Any]] = None
    identifiers_present: Optional[Dict[str, Any]] = None
    last_evaluated_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None
    provider_status: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MessagingAudienceSyncJobResponse(BaseModel):
    id: int
    project_id: int
    audience_id: int
    audience_destination_id: Optional[int] = None
    provider_type: str
    operation: str
    status: str
    total_count: int
    eligible_count: int
    add_count: int
    remove_count: int
    failed_count: int
    dry_run: bool
    request_summary: Optional[Dict[str, Any]] = None
    response_summary: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MessagingAudienceSyncRequest(BaseModel):
    dry_run: bool = False


class MessagingAudienceWebhookCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: str = Field(..., min_length=1, max_length=1000)
    secret: Optional[str] = Field(None, max_length=2000)
    events: Optional[List[str]] = None
    is_active: bool = True


class MessagingAudienceWebhookUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    url: Optional[str] = Field(None, min_length=1, max_length=1000)
    secret: Optional[str] = Field(None, max_length=2000)
    events: Optional[List[str]] = None
    is_active: Optional[bool] = None


class MessagingAudienceWebhookResponse(BaseModel):
    id: int
    project_id: int
    name: str
    url: str
    events: Optional[List[str]] = None
    is_active: bool
    has_secret: bool = False
    last_delivery_status: Optional[str] = None
    last_delivered_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class MessagingAudienceWebhookDeliveryResponse(BaseModel):
    id: int
    project_id: int
    webhook_endpoint_id: int
    audience_id: Optional[int] = None
    audience_destination_id: Optional[int] = None
    sync_job_id: Optional[int] = None
    event_name: str
    status: str
    attempt_count: int
    next_retry_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    error_message: Optional[str] = None
    payload_summary: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==========================================
# Event Mapping Schemas
# ==========================================

class PropertyMappingDef(BaseModel):
    """Single property mapping definition"""
    source: str  # Versya property name
    target: str  # Destination property name
    transform: Optional[str] = None  # Optional transformation


class EventMappingCreate(BaseModel):
    """Create a new event mapping"""
    event_schema_id: int
    destination_id: int
    destination_event_name: str = Field(..., min_length=1, max_length=255)
    property_mappings: Optional[List[PropertyMappingDef]] = None
    provider_settings: Optional[Dict[str, Any]] = None


class EventMappingUpdate(BaseModel):
    """Update an event mapping"""
    destination_event_name: Optional[str] = Field(None, min_length=1, max_length=255)
    property_mappings: Optional[List[PropertyMappingDef]] = None
    provider_settings: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class EventMappingResponse(BaseModel):
    """Response for an event mapping"""
    id: int
    project_id: int
    event_schema_id: int
    destination_id: int
    destination_event_name: str
    property_mappings: Optional[List[Dict[str, Any]]] = None
    provider_settings: Optional[Dict[str, Any]] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EventMappingDetailResponse(EventMappingResponse):
    """Response with related objects"""
    event_schema: Optional[EventSchemaResponse] = None
    destination: Optional[DestinationResponse] = None


class DestinationDeliveryResponse(BaseModel):
    """Server-side destination delivery/debug log."""
    id: int
    project_id: int
    destination_id: int
    event_id: int
    event_mapping_id: Optional[int] = None
    destination_type: str
    provider_event_name: str
    status: str
    attempt_count: int
    max_attempts: int
    dedupe_key: str
    request_summary: Optional[Dict[str, Any]] = None
    attribution_resolution: Optional[Dict[str, Any]] = None
    response_summary: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    skipped_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==========================================
# GTM Container Schemas
# ==========================================

class GTMContainerCreate(BaseModel):
    """Create a new GTM container config"""
    container_name: str = Field(..., min_length=1, max_length=255)
    domain_id: Optional[int] = None
    gtm_container_id: Optional[str] = Field(None, pattern=r'^GTM-[A-Z0-9]+$')


class GTMContainerUpdate(BaseModel):
    """Update a GTM container config"""
    container_name: Optional[str] = Field(None, min_length=1, max_length=255)
    domain_id: Optional[int] = None
    gtm_container_id: Optional[str] = Field(None, pattern=r'^GTM-[A-Z0-9]+$')


class GTMContainerResponse(BaseModel):
    """Response for a GTM container config"""
    id: int
    project_id: int
    domain_id: Optional[int] = None
    container_name: str
    gtm_container_id: Optional[str] = None
    last_generated_at: Optional[datetime] = None
    version: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class GTMContainerGenerateResponse(BaseModel):
    """Response after generating GTM container"""
    success: bool
    version: int
    generated_at: datetime
    container_json: Optional[Dict[str, Any]] = None
    message: str


# ==========================================
# SDK Config Schemas
# ==========================================

class SDKConfigResponse(BaseModel):
    """SDK runtime configuration response"""
    version: str
    projectId: int
    eventSchemas: List[Dict[str, Any]]
    noCodeMappings: List[Dict[str, Any]]
    dataLayer: Dict[str, Any]
    consent: Dict[str, Any]
    settings: Dict[str, Any]


# ==========================================
# WhatsApp Test Message Schemas
# ==========================================

class WhatsAppTestMessageRequest(BaseModel):
    """Request to send a WhatsApp test message"""
    to_number: str = Field(..., min_length=10, max_length=20, description="WhatsApp number with country code (E.164 format, e.g., 5511999999999)")
    message: str = Field(..., min_length=1, max_length=4096, description="Message text to send")


class WhatsAppTestMessageResponse(BaseModel):
    """Response after sending a WhatsApp test message"""
    success: bool
    message_id: Optional[str] = None
    status: str  # sent, delivered, read, failed
    to_number: str
    error: Optional[str] = None


class WhatsAppMessageStatusResponse(BaseModel):
    """Response for WhatsApp message status check"""
    message_id: str
    status: str  # sent, delivered, read, failed
    to_number: str
    updated_at: Optional[datetime] = None


# ==========================================
# Contact Create / CSV Import Schemas
# ==========================================

class ContactCreateRequest(BaseModel):
    """Request to manually create a contact"""
    external_id: Optional[str] = Field(None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)
    tags: Optional[List[str]] = None
    lifecycle_stage: Optional[str] = Field(None, max_length=30)
    properties: Optional[Dict[str, Any]] = None

    @model_validator(mode='before')
    @classmethod
    def strip_empty_strings(cls, data: Any) -> Any:
        """Convert empty strings to None so EmailStr etc. don't reject them."""
        if isinstance(data, dict):
            for key in ('email', 'phone', 'name', 'external_id', 'lifecycle_stage'):
                if key in data and data[key] == '':
                    data[key] = None
        return data

    def model_post_init(self, __context: Any) -> None:
        if not self.name and not self.email and not self.phone:
            raise ValueError("At least one of name, email, or phone must be provided")


class ContactCreateResponse(BaseModel):
    """Response after creating a contact"""
    contact: MessagingUserResponse
    warnings: List[str] = []


class CSVImportPreviewResponse(BaseModel):
    """Response for CSV import preview"""
    headers: List[str]
    sample_rows: List[List[str]]
    total_rows: int
    suggested_mapping: Dict[str, str]


class CSVImportResultResponse(BaseModel):
    """Response after CSV import"""
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[Dict[str, Any]] = []


# ==========================================
# Send Message to Contact Schemas
# ==========================================

EXTERNAL_TOUCH_TYPES = ("phone_call", "in_person", "whatsapp", "email", "sms", "other")

# touch_type -> pacing channel written to ContactLedger/SendLog. whatsapp/email/
# sms map to REAL channels so their cooldowns bite; the rest are pseudo-channels
# that always count toward total caps (per-channel only if the tenant configures
# such keys in contact_caps/channel_cooldowns).
EXTERNAL_TOUCH_CHANNEL_MAP = {
    "phone_call": "phone",
    "in_person": "in_person",
    "whatsapp": "whatsapp",
    "email": "email",
    "sms": "sms",
    "other": "other",
}


class ExternalTouchRequest(BaseModel):
    """Log an out-of-band interaction (call, visit, personal WhatsApp...)."""
    touch_type: str
    note: str = Field(..., min_length=1, max_length=2000)
    occurred_at: Optional[datetime] = None  # default now; backdating allowed
    count_as_message: bool = True  # False = pure note, no pacing impact

    @model_validator(mode="after")
    def _validate(self):
        if self.touch_type not in EXTERNAL_TOUCH_TYPES:
            raise ValueError(
                f"touch_type must be one of {', '.join(EXTERNAL_TOUCH_TYPES)}"
            )
        if self.occurred_at is not None:
            occurred = self.occurred_at
            if occurred.tzinfo is not None:
                from datetime import timezone
                occurred = occurred.astimezone(timezone.utc).replace(tzinfo=None)
                self.occurred_at = occurred
            from datetime import timedelta
            if occurred > datetime.utcnow() + timedelta(minutes=5):
                raise ValueError("occurred_at cannot be in the future")
        return self


class ExternalTouchResponse(BaseModel):
    send_log_id: int
    event_id: int
    channel: str
    counted_as_message: bool
    occurred_at: datetime


class SendToContactRequest(BaseModel):
    """Request to send a message to a specific contact.

    Three modes:
    - Internal template: set template_id (email or WhatsApp)
    - Free text (Evolution WhatsApp): set channel + free_text
    - Meta WhatsApp template: set meta_template_name + meta_template_language
    """
    # Internal template mode
    template_id: Optional[int] = None
    variables: Optional[Dict[str, Any]] = None
    # Free text mode (Evolution API WhatsApp)
    channel: Optional[str] = Field(None, description="Channel for free-text mode: whatsapp")
    free_text: Optional[str] = Field(None, max_length=4096)
    # Meta WhatsApp template mode
    meta_template_name: Optional[str] = None
    meta_template_language: Optional[str] = "en_US"
    meta_template_components: Optional[List[Dict[str, Any]]] = None
    # Reply routing (all modes)
    route_reply_to: Optional[str] = Field(None, description="Reply routing: human, chatbot, agent_team, or null")
    route_reply_handler_id: Optional[int] = Field(None, description="Chatbot or agent team ID for reply routing")
    # Email attachments (email template mode only): [{url, filename}]
    attachments: Optional[List[Dict[str, Any]]] = None


class SendToContactResponse(BaseModel):
    """Response after sending a message to a contact"""
    success: bool
    message: str
    channel: Optional[str] = None
    send_log_id: Optional[int] = None
    rendered_subject: Optional[str] = None
    rendered_body: Optional[str] = None
