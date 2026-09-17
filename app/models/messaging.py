"""
Messaging Middleware Models
Event-driven messaging system for multi-channel communications.
"""
from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey, JSON, Enum, Index, UniqueConstraint, Float, CheckConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import enum
import uuid


class ChannelType(str, enum.Enum):
    """Supported messaging channel types"""
    email = "email"
    sms = "sms"
    whatsapp = "whatsapp"
    inapp = "inapp"
    push = "push"
    webhook = "webhook"


class AuthType(str, enum.Enum):
    """Authentication types for webhook channels"""
    none = "none"
    bearer = "bearer"
    basic = "basic"
    api_key = "api_key"
    custom_header = "custom_header"


class MessageStatus(str, enum.Enum):
    """Message delivery status"""
    pending = "pending"
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    bounced = "bounced"


class MessagingDomain(Base):
    """
    Verified domains for frontend SDK authentication.
    Each domain gets a write_key (pk_live_xxx) for client-side tracking.
    """
    __tablename__ = "messaging_domains"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    domain = Column(String(255), nullable=False, index=True)  # e.g., "example.com"
    write_key = Column(String(100), nullable=False, unique=True, index=True)  # pk_live_xxx
    is_verified = Column(Boolean, default=False, nullable=False)
    verification_token = Column(String(100), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    snippet_installed_at = Column(DateTime, nullable=True)  # When SDK first reported installation
    is_active = Column(Boolean, default=True, nullable=False)
    allowed_origins = Column(ARRAY(String), nullable=True)  # Additional allowed origins
    rate_limit_per_minute = Column(Integer, default=100, nullable=True)  # SDK rate limit per minute
    rate_limit_per_day = Column(Integer, default=10000, nullable=True)  # SDK rate limit per day
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="messaging_domains")
    tracking_domains = relationship("MessagingTrackingDomain", back_populates="domain", cascade="all, delete-orphan")


class MessagingApiKey(Base):
    """
    Secret API keys for backend-to-backend authentication.
    Secret keys (sk_live_xxx) are hashed and only shown once on creation.
    """
    __tablename__ = "messaging_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    secret_key_hash = Column(String(255), nullable=False)  # Hashed sk_live_xxx
    key_prefix = Column(String(20), nullable=False)  # First 8 chars for display (e.g., "sk_live_ab")
    permissions = Column(ARRAY(String), default=["send", "track", "identify"])
    rate_limit_per_minute = Column(Integer, default=1000, nullable=False)
    rate_limit_per_day = Column(Integer, default=100000, nullable=False)
    allowed_ips = Column(ARRAY(String(45)), nullable=True)  # IP allowlist (IPv4/IPv6/CIDR)
    is_active = Column(Boolean, default=True, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="messaging_api_keys")


class MessagingChannel(Base):
    """
    DEPRECATED: Legacy webhook-based channel model.
    Replaced by project_channel_configs + channel_capabilities + handler_channel_links.
    Table kept for FK dependency from messaging_templates.channel_id.
    Do NOT use for new features — use ChannelRegistryService + ProjectChannelConfig instead.
    """
    __tablename__ = "messaging_channels"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False, index=True)
    channel_type = Column(Enum(ChannelType), nullable=False)
    webhook_url = Column(String(500), nullable=False)
    auth_type = Column(Enum(AuthType), default=AuthType.none, nullable=False)
    auth_config = Column(JSON, nullable=True)  # Encrypted credentials in service layer
    headers = Column(JSON, nullable=True)  # Additional headers to include
    is_default = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    max_retries = Column(Integer, default=3, nullable=False)
    retry_delay_seconds = Column(Integer, default=60, nullable=False)
    timeout_seconds = Column(Integer, default=30, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="messaging_channels")
    templates = relationship("MessagingTemplate", back_populates="channel")

    __table_args__ = (
        Index('ix_messaging_channels_project_slug', 'project_id', 'slug', unique=True),
    )


class MessagingTemplate(Base):
    """
    Message templates with variable substitution.
    Templates use {{variable}} syntax for dynamic content.
    """
    __tablename__ = "messaging_templates"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_id = Column(Integer, ForeignKey("messaging_channels.id", ondelete="SET NULL"), nullable=True, index=True)
    slug = Column(String(100), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    subject = Column(String(500), nullable=True)  # For email templates
    channel_type = Column(Enum(ChannelType, create_type=False), nullable=True, default=ChannelType.email)
    from_email = Column(String(255), nullable=True)
    from_name = Column(String(255), nullable=True)
    reply_to = Column(String(255), nullable=True)
    folder = Column(String(100), nullable=True)
    media_url = Column(String(1000), nullable=True)
    body_format = Column(String(10), nullable=True, server_default="html")
    body = Column(Text, nullable=False)  # Template with {{variables}}
    template_metadata = Column(JSON, nullable=True)  # Provider-specific (e.g., provider_template_id)
    trigger_events = Column(ARRAY(String), nullable=True)  # Events that auto-trigger this template
    is_active = Column(Boolean, default=True, nullable=False)
    # Content availability and automation activation are deliberately separate:
    # importing/authoring a usable template must never start an event journey.
    automation_enabled = Column(
        Boolean, default=False, server_default=text("false"), nullable=False, index=True,
    )
    purpose_key = Column(String(120), nullable=True, index=True)
    attention_policy = Column(JSONB, nullable=True)

    # i18n — template family: rows share (project_id, slug), differ by locale.
    locale = Column(String(10), nullable=True)  # BCP-47, e.g. pt-BR, es-ES
    source_locale = Column(String(10), nullable=True)  # locale this variant was authored/translated from
    translation_status = Column(String(20), nullable=False, server_default="source")  # source|machine|reviewed
    translated_at = Column(DateTime, nullable=True)

    # WhatsApp Cloud API first-class fields (populated for channel_type='whatsapp' with Meta-approved templates)
    meta_template_name = Column(String(200), nullable=True)
    meta_language = Column(String(20), nullable=True)  # e.g., pt_BR, en_US
    meta_components = Column(JSON, nullable=True)  # Component layout as returned by Meta Graph API
    whatsapp_instance_id = Column(Integer, ForeignKey("whatsapp_instances.id", ondelete="SET NULL"), nullable=True, index=True)

    # External origin tracking (for imported templates)
    external_source = Column(String(20), nullable=True)  # e.g., 'meta_cloud'
    external_last_synced_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="messaging_templates")
    channel = relationship("MessagingChannel", back_populates="templates")
    whatsapp_instance = relationship("WhatsAppInstance", foreign_keys=[whatsapp_instance_id])

    __table_args__ = (
        # Template family: one row per (project, slug, locale)
        Index('ix_msg_tpl_proj_slug_locale', 'project_id', 'slug', 'locale', unique=True),
    )


class MessagingUser(Base):
    """
    Identified contacts/users for messaging.
    Users are identified via external_id from the client's system.
    """
    __tablename__ = "messaging_users"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id = Column(String(255), nullable=False, index=True)  # Client's user ID
    email = Column(String(255), nullable=True, index=True)
    phone = Column(String(50), nullable=True)
    name = Column(String(255), nullable=True)
    properties = Column(JSON, nullable=True)  # Additional user properties
    is_subscribed = Column(Boolean, default=True, nullable=False)
    first_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Normalized phone (digits-only E.164, e.g. "5511999999999")
    phone_e164 = Column(String(30), nullable=True)
    phone_norm_status = Column(String(20), nullable=True)  # valid_e164, inferred, fallback, missing, invalid

    # WhatsApp number validation
    whatsapp_status = Column(String(20), nullable=True)  # valid, invalid, unverified, checking
    whatsapp_checked_at = Column(DateTime, nullable=True)

    # PII hashes for identity resolution (GDPR compliant)
    email_hash = Column(String(64), nullable=True, index=True)
    phone_hash = Column(String(64), nullable=True, index=True)

    # Consent tracking (GDPR/LGPD)
    consent_marketing = Column(Boolean, default=False, nullable=True)
    consent_analytics = Column(Boolean, default=False, nullable=True)
    consent_given_at = Column(DateTime, nullable=True)
    consent_ip = Column(String(45), nullable=True)  # IPv6 compatible
    # Tenant-owned policy identifiers are stable contract keys, not short
    # semantic versions (for example ``tabloide-subscriber-optin-v1``).
    # Keep this aligned with permission-evidence.policy_version.
    consent_version = Column(String(100), nullable=True)

    # Opt-out tracking
    opted_out_channels = Column(JSON, nullable=True)  # ["whatsapp", "email", ...] per-channel opt-out
    global_opt_out = Column(Boolean, default=False, nullable=False)  # STOP all channels

    # Segment classification
    segment_rule_id = Column(Integer, ForeignKey("segment_rules.id", ondelete="SET NULL"), nullable=True)
    segment_name = Column(String(255), nullable=True)
    segment_updated_at = Column(DateTime, nullable=True)

    # Identity resolution fields
    status = Column(String(20), nullable=False, server_default='active')  # active, merged, deleted
    merged_into = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True)
    lifecycle_stage = Column(String(30), nullable=True)  # lead, prospect, customer, churned
    # i18n — resolved at ingestion (LocaleResolver); null ⇒ project.default_locale/default_timezone
    locale = Column(String(10), nullable=True)  # BCP-47, e.g. pt-BR, es-ES
    timezone = Column(String(40), nullable=True)  # IANA, e.g. America/Sao_Paulo
    tags = Column(JSON, nullable=True)  # String array
    primary_channel = Column(JSON, nullable=True)  # {channel_type, channel_instance_id}
    created_via = Column(String(30), nullable=False, server_default='api')  # snippet, inbound_message, api, manual
    consent_channels = Column(JSONB, nullable=True)  # Per-channel: {"email": {"granted": true, ...}}
    # Best-time-to-send overrides: {"_default": {...}, "<channel>": {...}} —
    # contact tier of the inheritance chain; null = inherit project config.
    send_windows = Column(JSONB, nullable=True)

    # Sandbox flag — sandbox contacts are used for funnel testing only
    is_sandbox = Column(Boolean, default=False, server_default=text("false"), nullable=False)

    # Block tracking
    is_blocked = Column(Boolean, default=False, server_default=text("false"), nullable=False)
    blocked_at = Column(DateTime, nullable=True)
    blocked_reason = Column(String(255), nullable=True)

    # Automation pause — suppress automated messages (funnels, event actions,
    # chatbot/agent replies) while an operator handles the contact manually.
    # Manual sends and inbound processing are unaffected (contrast with block).
    automations_paused = Column(Boolean, default=False, server_default=text("false"), nullable=False)
    automations_paused_at = Column(DateTime, nullable=True)
    automations_paused_reason = Column(String(255), nullable=True)
    automations_pause_mode = Column(String(10), nullable=True)  # 'hold' | 'skip'; null when not paused

    # Relationships
    project = relationship("Project", back_populates="messaging_users")
    events = relationship("MessagingEvent", back_populates="user")
    segment_rule = relationship("SegmentRule", foreign_keys=[segment_rule_id])
    identities = relationship("ContactIdentity", back_populates="user", cascade="all, delete-orphan")
    account_associations = relationship("ContactAccountAssociation", back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_messaging_users_project_external', 'project_id', 'external_id', unique=True),
        Index('ix_msg_users_project_segment', 'project_id', 'segment_rule_id'),
        Index('ix_messaging_users_status', 'project_id', 'status'),
        Index('ix_msg_users_project_sandbox', 'project_id', 'is_sandbox'),
        Index('ix_msg_users_project_blocked', 'project_id', 'is_blocked'),
        Index('ix_msg_users_project_auto_paused', 'project_id', 'automations_paused'),
    )


# ============================================================================
# Contact Verification Models
# ============================================================================

class WorkspaceVerificationEntitlement(Base):
    """Commercial access boundary for platform-funded contact verification."""
    __tablename__ = "workspace_verification_entitlements"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    access_mode = Column(String(20), nullable=False, server_default="disabled")  # disabled | unmetered | credits
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    granted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    notes = Column(Text, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ProjectVerificationSettings(Base):
    """Per-project provider, automation, freshness, and send-policy settings."""
    __tablename__ = "project_verification_settings"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    email_provider = Column(String(50), nullable=False, server_default="millionverifier")
    whatsapp_provider = Column(String(50), nullable=False, server_default="evolution_api")
    whatsapp_instance_id = Column(Integer, ForeignKey("whatsapp_instances.id", ondelete="SET NULL"), nullable=True)
    email_auto_verify = Column(Boolean, nullable=False, server_default=text("false"))
    whatsapp_auto_verify = Column(Boolean, nullable=False, server_default=text("false"))
    # Explicit automation semantics.  The legacy *_auto_verify columns remain
    # for backward compatibility with existing clients and workers.
    email_verify_on_first_seen = Column(Boolean, nullable=False, server_default=text("false"))
    email_recheck_enabled = Column(Boolean, nullable=False, server_default=text("false"))
    whatsapp_verify_on_first_seen = Column(Boolean, nullable=False, server_default=text("false"))
    whatsapp_recheck_enabled = Column(Boolean, nullable=False, server_default=text("false"))
    email_recheck_days = Column(Integer, nullable=False, server_default="60")
    whatsapp_recheck_days = Column(Integer, nullable=False, server_default="60")
    email_send_policy = Column(String(30), nullable=False, server_default="block_invalid")
    whatsapp_send_policy = Column(String(30), nullable=False, server_default="block_invalid")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ContactVerificationState(Base):
    """Latest conclusive result for one contact identifier type."""
    __tablename__ = "contact_verification_states"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id = Column(BigInteger, ForeignKey("contact_endpoints.id", ondelete="SET NULL"), nullable=True, index=True)
    verification_type = Column(String(30), nullable=False, index=True)  # email | whatsapp | future types
    identifier_hash = Column(String(64), nullable=False)
    provider = Column(String(50), nullable=False)
    provider_key_source = Column(String(20), nullable=False, server_default="platform")
    provider_version = Column(String(30), nullable=True)
    canonical_status = Column(String(20), nullable=False, index=True)  # valid | invalid | risky
    provider_status = Column(String(50), nullable=True)
    provider_metadata = Column(JSONB, nullable=True)
    checked_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    last_attempt_status = Column(String(20), nullable=False, server_default="succeeded")
    last_attempt_at = Column(DateTime, nullable=False)
    last_error_code = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        # Endpoint-aware uniqueness allows a contact to retain independent
        # results for multiple email/phone endpoints.  The partial legacy key
        # keeps hash-only rows unique until an endpoint can be reconciled.
        Index(
            "uq_contact_verify_state_ep",
            "project_id",
            "endpoint_id",
            "verification_type",
            unique=True,
            postgresql_where=text("endpoint_id IS NOT NULL"),
        ),
        Index(
            "uq_contact_verify_state_legacy",
            "project_id",
            "user_id",
            "verification_type",
            unique=True,
            postgresql_where=text("endpoint_id IS NULL"),
        ),
        Index("ix_contact_verify_state_status", "project_id", "verification_type", "canonical_status"),
    )


class ContactVerificationJob(Base):
    """Durable manual, individual, or automatic verification run."""
    __tablename__ = "contact_verification_jobs"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id = Column(BigInteger, ForeignKey("contact_endpoints.id", ondelete="SET NULL"), nullable=True, index=True)
    verification_type = Column(String(30), nullable=False, index=True)
    provider = Column(String(50), nullable=False)
    source_type = Column(String(30), nullable=False, server_default="project_contacts")
    trigger_type = Column(String(30), nullable=False, server_default="manual")
    status = Column(String(30), nullable=False, server_default="queued", index=True)
    selection = Column(JSONB, nullable=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    candidate_count = Column(Integer, nullable=False, server_default="0")
    unique_count = Column(Integer, nullable=False, server_default="0")
    processed_count = Column(Integer, nullable=False, server_default="0")
    valid_count = Column(Integer, nullable=False, server_default="0")
    invalid_count = Column(Integer, nullable=False, server_default="0")
    risky_count = Column(Integer, nullable=False, server_default="0")
    skipped_count = Column(Integer, nullable=False, server_default="0")
    failed_count = Column(Integer, nullable=False, server_default="0")
    provider_units = Column(Integer, nullable=False, server_default="0")
    billing_disposition = Column(String(20), nullable=False, server_default="waived")
    billing_reservation_id = Column(String(100), nullable=True)
    cancel_requested = Column(Boolean, nullable=False, server_default=text("false"))
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    items = relationship("ContactVerificationItem", back_populates="job", cascade="all, delete-orphan")
    operations = relationship("ContactVerificationOperation", back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_contact_verify_job_project_created", "project_id", "created_at"),
    )

    @property
    def provider_progress(self):
        """Weighted provider progress for bulk operations, when available."""
        weighted_progress = 0.0
        weighted_items = 0
        for operation in self.operations or []:
            summary = operation.response_summary or {}
            percent = summary.get("percent")
            if percent is None:
                continue
            try:
                normalized = max(0.0, min(100.0, float(percent)))
            except (TypeError, ValueError):
                continue
            item_count = max(0, int(operation.item_count or 0))
            if item_count:
                weighted_progress += normalized * item_count
                weighted_items += item_count
        if not weighted_items:
            return None
        return int(round(weighted_progress / weighted_items))


class ContactVerificationItem(Base):
    """PII-safe per-contact work item and immutable attempt result."""
    __tablename__ = "contact_verification_items"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(BigInteger, ForeignKey("contact_verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=True, index=True)
    endpoint_id = Column(BigInteger, ForeignKey("contact_endpoints.id", ondelete="SET NULL"), nullable=True, index=True)
    source_row_key = Column(String(100), nullable=True)
    verification_type = Column(String(30), nullable=False)
    identifier_hash = Column(String(64), nullable=False, index=True)
    status = Column(String(30), nullable=False, server_default="queued", index=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    canonical_status = Column(String(20), nullable=True)
    provider_status = Column(String(50), nullable=True)
    provider_metadata = Column(JSONB, nullable=True)
    error_code = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    job = relationship("ContactVerificationJob", back_populates="items")

    __table_args__ = (
        UniqueConstraint("job_id", "user_id", "verification_type", name="uq_contact_verify_job_user"),
        Index("ix_contact_verify_item_job_status", "job_id", "status"),
    )


class ContactVerificationOperation(Base):
    """One external provider call/file with usage and billing audit fields."""
    __tablename__ = "contact_verification_operations"

    id = Column(BigInteger, primary_key=True)
    usage_key = Column(String(64), nullable=False, unique=True, default=lambda: uuid.uuid4().hex)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(BigInteger, ForeignKey("contact_verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id = Column(BigInteger, ForeignKey("contact_endpoints.id", ondelete="SET NULL"), nullable=True, index=True)
    provider = Column(String(50), nullable=False, index=True)
    provider_key_source = Column(String(20), nullable=False, server_default="platform")
    operation_type = Column(String(30), nullable=False)  # single | bulk_file | whatsapp_lookup
    provider_operation_id = Column(String(255), nullable=True, index=True)
    status = Column(String(30), nullable=False, server_default="queued", index=True)
    item_count = Column(Integer, nullable=False, server_default="0")
    provider_units = Column(Integer, nullable=False, server_default="0")
    customer_units = Column(Integer, nullable=False, server_default="0")
    billing_disposition = Column(String(20), nullable=False, server_default="waived")
    request_summary = Column(JSONB, nullable=True)
    response_summary = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    job = relationship("ContactVerificationJob", back_populates="operations")


class MessagingEvent(Base):
    """
    Recorded events from frontend SDK or backend API.
    Events can trigger template-based messages.
    """
    __tablename__ = "messaging_events"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)
    anonymous_id = Column(String(100), nullable=True, index=True)  # For anonymous users
    event_name = Column(String(255), nullable=False, index=True)
    properties = Column(JSONB, nullable=True)  # Event data (JSONB for GIN indexing)
    # Server-owned provenance; ingestion paths must set this explicitly.
    # TrackRequest and BackendEventRequest do not expose this field.
    source = Column(String(20), nullable=False)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    processed = Column(Boolean, default=False, nullable=False, index=True)
    processing_notes = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    session_id = Column(String(100), nullable=True)
    client_ts = Column(DateTime(timezone=True), nullable=True)
    external_event_id = Column(String(255), nullable=True, index=True)
    # Historical Project Import events are persisted with their business time
    # and an explicit processing mode. Only ``live`` events may trigger side
    # effects in the event drain.
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    import_id = Column(String(36), ForeignKey("project_imports.id", ondelete="SET NULL"), nullable=True, index=True)
    processing_mode = Column(String(20), nullable=False, server_default="live", index=True)
    campaign_origin = Column(String(50), nullable=True, index=True)
    attribution = Column(JSONB, nullable=True)

    # Relationships
    project = relationship("Project", back_populates="messaging_events")
    user = relationship("MessagingUser", back_populates="events")
    destination_deliveries = relationship("MessagingDestinationDelivery", back_populates="event", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_msg_events_proj_name', 'project_id', 'event_name'),
        Index('ix_msg_events_proj_created', 'project_id', 'created_at'),
        Index('ix_msg_events_proj_session', 'project_id', 'session_id'),
        Index('ix_msg_events_proj_user_created', 'project_id', 'user_id', 'created_at'),
        Index('ix_msg_events_proj_anon_created', 'project_id', 'anonymous_id', 'created_at'),
        CheckConstraint(
            "processing_mode IN ('live','derive_only','audit_only')",
            name="ck_msg_event_processing_mode",
        ),
    )


class MessagingLog(Base):
    """
    Message delivery log with status tracking.
    Stores rendered content and provider responses.
    """
    __tablename__ = "messaging_logs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    template_id = Column(Integer, ForeignKey("messaging_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    channel_id = Column(Integer, ForeignKey("messaging_channels.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("messaging_events.id", ondelete="SET NULL"), nullable=True, index=True)
    template_slug = Column(String(100), nullable=True)  # Denormalized for querying
    channel_type = Column(String(50), nullable=True)  # Denormalized for querying
    recipient = Column(String(255), nullable=False)  # Email, phone, etc.
    rendered_subject = Column(String(500), nullable=True)
    rendered_body = Column(Text, nullable=True)
    status = Column(Enum(MessageStatus), default=MessageStatus.pending, nullable=False, index=True)
    provider_message_id = Column(String(255), nullable=True)
    provider_response = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    attempt_count = Column(Integer, default=0, nullable=False)
    last_attempt_at = Column(DateTime, nullable=True)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    sent_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    # Relationships
    project = relationship("Project", back_populates="messaging_logs")
    template = relationship("MessagingTemplate")
    channel = relationship("MessagingChannel")
    user = relationship("MessagingUser")
    event = relationship("MessagingEvent")


class MessagingEventLock(Base):
    """
    Event deduplication to prevent duplicate messages.
    Tracks which events have triggered which templates for each user.
    """
    __tablename__ = "messaging_event_locks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    event_name = Column(String(255), nullable=False)
    template_id = Column(Integer, ForeignKey("messaging_templates.id", ondelete="CASCADE"), nullable=False)
    triggered_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index('ix_event_lock_unique', 'project_id', 'user_id', 'event_name', 'template_id', unique=True),
    )


class MessagingAnonymousProfile(Base):
    """
    Tracks anonymous users before they are identified.
    Used for identity resolution: merge anonymous activity to known users.
    """
    __tablename__ = "messaging_anonymous_profiles"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    anonymous_id = Column(String(100), nullable=False, index=True)
    first_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    merged_to_user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True)
    merged_at = Column(DateTime, nullable=True)
    properties = Column(JSON, nullable=True)  # Collected properties before identification
    device_fingerprint = Column(String(20), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="messaging_anonymous_profiles")
    merged_to_user = relationship("MessagingUser")

    __table_args__ = (
        Index('ix_msg_anon_proj_anon_unique', 'project_id', 'anonymous_id', unique=True),
        Index('ix_msg_anon_proj_devfp', 'project_id', 'device_fingerprint'),
    )


class DSARRequestType(str, enum.Enum):
    """DSAR request types"""
    export = "export"
    delete = "delete"
    rectify = "rectify"
    withdraw_consent = "withdraw_consent"


class DSARRequestStatus(str, enum.Enum):
    """DSAR request status"""
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class MessagingDSARRequest(Base):
    """
    Data Subject Access Request (DSAR) tracking.
    Supports export, delete, rectify, and consent withdrawal requests.
    """
    __tablename__ = "messaging_dsar_requests"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    request_type = Column(Enum(DSARRequestType), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)
    external_id = Column(String(255), nullable=True)  # For lookup if user_id not available
    email_hash = Column(String(64), nullable=True, index=True)  # For lookup by email
    status = Column(Enum(DSARRequestStatus), default=DSARRequestStatus.pending, nullable=False, index=True)
    requested_at = Column(DateTime, server_default=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    result_url = Column(String(500), nullable=True)  # Download URL for export
    result_data = Column(JSON, nullable=True)  # Stored result data
    error_message = Column(Text, nullable=True)
    requested_by_ip = Column(String(45), nullable=True)  # IPv6 compatible
    processed_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    user = relationship("MessagingUser")
    processed_by = relationship("User")


class MessagingEventSchema(Base):
    """
    Defines event schema/structure for validation and documentation.
    Supports JSON Schema format for property definitions.
    """
    __tablename__ = "messaging_event_schemas"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    event_name = Column(String(255), nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True, index=True)  # e.g., "ecommerce", "engagement", "lifecycle"
    properties_schema = Column(JSON, nullable=True)  # JSON Schema format
    required_properties = Column(ARRAY(String), nullable=True)
    is_standard = Column(Boolean, default=False, nullable=False)  # GA4 standard events
    is_active = Column(Boolean, default=True, nullable=False)
    semantic_kind = Column(String(30), nullable=False, server_default="fact")
    contract_status = Column(String(30), nullable=False, server_default="active")
    replacement_event_name = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")

    __table_args__ = (
        Index('ix_event_schemas_proj_name_unique', 'project_id', 'event_name', unique=True),
        CheckConstraint(
            "semantic_kind IN ('fact','decision','outcome','system')",
            name="ck_event_schema_semantic_kind",
        ),
        CheckConstraint(
            "contract_status IN ('active','legacy','deprecated')",
            name="ck_event_schema_contract_status",
        ),
    )


class DestinationType(str, enum.Enum):
    """Analytics destination types"""
    ga4 = "ga4"
    meta_pixel = "meta_pixel"
    google_ads = "google_ads"
    linkedin = "linkedin"
    tiktok = "tiktok"
    custom = "custom"


class MessagingDestination(Base):
    """
    Analytics destination configuration.
    Stores encrypted credentials for destinations like GA4, Meta Pixel, etc.
    """
    __tablename__ = "messaging_destinations"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    destination_type = Column(Enum(DestinationType), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    config_encrypted = Column(Text, nullable=True)  # Fernet encrypted JSON
    is_active = Column(Boolean, default=True, nullable=False)
    consent_required = Column(ARRAY(String), nullable=True)  # e.g., ['analytics', 'marketing']
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    event_mappings = relationship("MessagingEventMapping", back_populates="destination", cascade="all, delete-orphan")
    deliveries = relationship("MessagingDestinationDelivery", back_populates="destination", cascade="all, delete-orphan")


class MessagingEventMapping(Base):
    """
    Maps Versya events to destination-specific events.
    Allows property transformations and renaming.
    """
    __tablename__ = "messaging_event_mappings"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    event_schema_id = Column(Integer, ForeignKey("messaging_event_schemas.id", ondelete="CASCADE"), nullable=False, index=True)
    destination_id = Column(Integer, ForeignKey("messaging_destinations.id", ondelete="CASCADE"), nullable=False, index=True)
    destination_event_name = Column(String(255), nullable=False)  # Event name in destination
    property_mappings = Column(JSON, nullable=True)  # Map source props to dest props
    provider_settings = Column(JSONB, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    event_schema = relationship("MessagingEventSchema")
    destination = relationship("MessagingDestination", back_populates="event_mappings")

    __table_args__ = (
        Index('ix_event_mappings_schema_dest_unique', 'event_schema_id', 'destination_id', unique=True),
    )


class MessagingDestinationDelivery(Base):
    """
    Delivery log for server-side analytics/ad destinations.

    One row is created for each mapped destination attempt so the UI can explain
    what happened to an event: queued, sent, failed, retrying, or explicitly
    skipped due to consent, missing click IDs, credentials, or invalid payloads.
    """
    __tablename__ = "messaging_destination_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    destination_id = Column(Integer, ForeignKey("messaging_destinations.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("messaging_events.id", ondelete="CASCADE"), nullable=False, index=True)
    event_mapping_id = Column(Integer, ForeignKey("messaging_event_mappings.id", ondelete="SET NULL"), nullable=True, index=True)
    destination_type = Column(String(50), nullable=False, index=True)
    provider_event_name = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, server_default="queued", index=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="3")
    dedupe_key = Column(String(1024), nullable=False, unique=True, index=True)
    request_summary = Column(JSONB, nullable=True)
    attribution_resolution = Column(JSONB, nullable=True)
    response_summary = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    sent_at = Column(DateTime, nullable=True)
    skipped_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    destination = relationship("MessagingDestination", back_populates="deliveries")
    event = relationship("MessagingEvent", back_populates="destination_deliveries")
    event_mapping = relationship("MessagingEventMapping")

    __table_args__ = (
        Index('ix_dest_delivery_project_status_due', 'project_id', 'status', 'next_retry_at'),
        Index('ix_dest_delivery_event_dest', 'event_id', 'destination_id'),
    )


class MessagingAttributionTouch(Base):
    """Durable paid-click touch eligible for destination-time attribution."""
    __tablename__ = "messaging_attribution_touches"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)
    anonymous_id = Column(String(100), nullable=True, index=True)
    provider = Column(String(30), nullable=False, index=True)
    identifier_type = Column(String(30), nullable=False)
    identifier_value = Column(Text, nullable=False)
    identifier_hash = Column(String(64), nullable=False, index=True)
    source_event_id = Column(Integer, ForeignKey("messaging_events.id", ondelete="SET NULL"), nullable=True, index=True)
    capture_source = Column(String(30), nullable=False, server_default="legacy")
    tracking_domain_id = Column(Integer, ForeignKey("messaging_tracking_domains.id", ondelete="SET NULL"), nullable=True)
    page_url = Column(String(1000), nullable=True)
    utm = Column(JSONB, nullable=True)
    provenance = Column(JSONB, nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    project = relationship("Project")
    user = relationship("MessagingUser")
    source_event = relationship("MessagingEvent")
    tracking_domain = relationship("MessagingTrackingDomain")

    __table_args__ = (
        UniqueConstraint(
            "project_id", "provider", "identifier_type", "identifier_hash",
            name="uq_attr_touch_proj_provider_identifier",
        ),
        Index("ix_attr_touch_user_provider_time", "project_id", "user_id", "provider", "captured_at"),
        Index("ix_attr_touch_anon_provider_time", "project_id", "anonymous_id", "provider", "captured_at"),
    )

class MessagingTrackingDomain(Base):
    """
    First-party tracking host used by the sellable server-side tracking layer.

    MessagingDomain remains the SDK/write-key ownership record. This table
    tracks the managed CNAME/Cloudflare state for a customer-owned hostname
    such as track.customer.com.
    """
    __tablename__ = "messaging_tracking_domains"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    domain_id = Column(Integer, ForeignKey("messaging_domains.id", ondelete="SET NULL"), nullable=True, index=True)
    hostname = Column(String(255), nullable=False, unique=True, index=True)
    mode = Column(String(50), nullable=False, server_default="managed_cname")
    cname_target = Column(String(255), nullable=False)
    verification_token = Column(String(100), nullable=False, index=True)
    cloudflare_custom_hostname_id = Column(String(255), nullable=True, index=True)
    dns_status = Column(String(50), nullable=False, server_default="pending", index=True)
    ssl_status = Column(String(50), nullable=False, server_default="pending", index=True)
    proxy_status = Column(String(50), nullable=False, server_default="pending", index=True)
    cookie_keeper_enabled = Column(Boolean, nullable=False, server_default=text("true"))
    last_seen_at = Column(DateTime, nullable=True)
    config_version = Column(Integer, nullable=False, server_default="1")
    status_details = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    domain = relationship("MessagingDomain", back_populates="tracking_domains")
    proxy_logs = relationship("MessagingProxyRequestLog", back_populates="tracking_domain", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_tracking_domains_project_hostname', 'project_id', 'hostname', unique=True),
    )


class MessagingProxyRequestLog(Base):
    """
    Lightweight sampled log from the tracking-domain Worker.

    These rows explain whether the first-party proxy saw click IDs, refreshed
    cookies, and forwarded the request before the event becomes a destination
    delivery.
    """
    __tablename__ = "messaging_proxy_request_logs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    tracking_domain_id = Column(Integer, ForeignKey("messaging_tracking_domains.id", ondelete="SET NULL"), nullable=True, index=True)
    hostname = Column(String(255), nullable=False, index=True)
    method = Column(String(12), nullable=False)
    path = Column(String(500), nullable=False)
    action = Column(String(50), nullable=True, index=True)
    event_id = Column(String(255), nullable=True, index=True)
    anonymous_id = Column(String(100), nullable=True, index=True)
    status_code = Column(Integer, nullable=True)
    click_ids = Column(JSONB, nullable=True)
    cookies_refreshed = Column(ARRAY(String), nullable=True)
    request_summary = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    project = relationship("Project")
    tracking_domain = relationship("MessagingTrackingDomain", back_populates="proxy_logs")

    __table_args__ = (
        Index('ix_proxy_logs_project_created', 'project_id', 'created_at'),
        Index('ix_proxy_logs_tracking_created', 'tracking_domain_id', 'created_at'),
    )


class MessagingGTMContainer(Base):
    """
    GTM container generation configuration.
    Stores generated container JSON for import into GTM.
    """
    __tablename__ = "messaging_gtm_containers"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    domain_id = Column(Integer, ForeignKey("messaging_domains.id", ondelete="SET NULL"), nullable=True, index=True)
    container_name = Column(String(255), nullable=False)
    gtm_container_id = Column(String(50), nullable=True)  # GTM-XXXXX
    last_generated_at = Column(DateTime, nullable=True)
    generated_json = Column(JSON, nullable=True)  # Cached generated container
    version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    domain = relationship("MessagingDomain")


class MessagingAdsConsentEvidence(Base):
    """
    Audit evidence for platform-agnostic ads consent.

    This stores the customer's consent assertion without coupling it to a
    provider. Provider-specific consent fields are derived at sync time.
    """
    __tablename__ = "messaging_ads_consent_evidence"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False, index=True)
    scope = Column(String(50), nullable=False, index=True)  # ad_user_data, ad_personalization
    status = Column(String(20), nullable=False, index=True)  # granted, denied, withdrawn
    source = Column(String(100), nullable=True)
    policy_version = Column(String(100), nullable=True)
    evidence_id = Column(String(255), nullable=True, index=True)
    captured_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    page_url = Column(String(1000), nullable=True)
    evidence_metadata = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("Project")
    user = relationship("MessagingUser")

    __table_args__ = (
        Index('ix_ads_consent_project_user_scope', 'project_id', 'user_id', 'scope'),
    )


class MessagingAudience(Base):
    """
    A sellable ads audience definition built from Versya user/event data.
    """
    __tablename__ = "messaging_audiences"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    source_type = Column(String(30), nullable=False, server_default="dynamic_rule", index=True)  # csv_import, project_copy, dynamic_rule
    source_config = Column(JSONB, nullable=True)
    rule_config = Column(JSONB, nullable=True)
    status = Column(String(20), nullable=False, server_default="active", index=True)
    refresh_mode = Column(String(20), nullable=False, server_default="manual")
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_evaluated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    created_by = relationship("User")
    destinations = relationship("MessagingAudienceDestination", back_populates="audience", cascade="all, delete-orphan")
    memberships = relationship("MessagingAudienceMembership", back_populates="audience", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_audiences_project_status', 'project_id', 'status'),
    )


class MessagingAudienceDestination(Base):
    """
    Provider connection for an audience.

    destination_id links to the existing ads destination credential record;
    external_audience_id points to the provider-side customer list/custom
    audience.
    """
    __tablename__ = "messaging_audience_destinations"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    audience_id = Column(Integer, ForeignKey("messaging_audiences.id", ondelete="CASCADE"), nullable=False, index=True)
    destination_id = Column(Integer, ForeignKey("messaging_destinations.id", ondelete="SET NULL"), nullable=True, index=True)
    provider_type = Column(String(50), nullable=False, index=True)  # meta_pixel, google_ads, tiktok
    external_audience_id = Column(String(255), nullable=True)
    external_audience_name = Column(String(255), nullable=True)
    sync_mode = Column(String(20), nullable=False, server_default="add_remove")
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    last_sync_status = Column(String(30), nullable=True)
    last_sync_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    config = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    audience = relationship("MessagingAudience", back_populates="destinations")
    destination = relationship("MessagingDestination")
    sync_jobs = relationship("MessagingAudienceSyncJob", back_populates="audience_destination")

    __table_args__ = (
        Index('ix_audience_dest_project_provider', 'project_id', 'provider_type'),
    )


class MessagingAudienceMembership(Base):
    """
    Evaluated membership state for one user in one audience.
    """
    __tablename__ = "messaging_audience_memberships"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    audience_id = Column(Integer, ForeignKey("messaging_audiences.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False, index=True)
    state = Column(String(20), nullable=False, server_default="excluded", index=True)  # desired state: eligible, excluded
    eligibility_reason = Column(String(100), nullable=True, index=True)
    eligibility_details = Column(JSONB, nullable=True)
    identifiers_present = Column(JSONB, nullable=True)
    last_evaluated_at = Column(DateTime, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    provider_status = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    audience = relationship("MessagingAudience", back_populates="memberships")
    user = relationship("MessagingUser")

    __table_args__ = (
        UniqueConstraint('audience_id', 'user_id', name='uq_audience_membership_user'),
        Index('ix_audience_membership_project_state', 'project_id', 'state'),
    )


class MessagingAudienceSyncJob(Base):
    """
    A manual or scheduled sync run for one audience-provider pair.
    """
    __tablename__ = "messaging_audience_sync_jobs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    audience_id = Column(Integer, ForeignKey("messaging_audiences.id", ondelete="CASCADE"), nullable=False, index=True)
    audience_destination_id = Column(Integer, ForeignKey("messaging_audience_destinations.id", ondelete="SET NULL"), nullable=True, index=True)
    provider_type = Column(String(50), nullable=False, index=True)
    operation = Column(String(20), nullable=False, server_default="sync")
    status = Column(String(30), nullable=False, server_default="queued", index=True)
    total_count = Column(Integer, nullable=False, server_default="0")
    eligible_count = Column(Integer, nullable=False, server_default="0")
    add_count = Column(Integer, nullable=False, server_default="0")
    remove_count = Column(Integer, nullable=False, server_default="0")
    failed_count = Column(Integer, nullable=False, server_default="0")
    dry_run = Column(Boolean, nullable=False, server_default=text("false"))
    request_summary = Column(JSONB, nullable=True)
    response_summary = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    project = relationship("Project")
    audience = relationship("MessagingAudience")
    audience_destination = relationship("MessagingAudienceDestination", back_populates="sync_jobs")
    items = relationship("MessagingAudienceSyncItem", back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_audience_sync_jobs_project_created', 'project_id', 'created_at'),
    )


class MessagingAudienceSyncItem(Base):
    """
    Log-safe per-user sync item. No raw PII is stored here.
    """
    __tablename__ = "messaging_audience_sync_items"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("messaging_audience_sync_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)
    operation = Column(String(20), nullable=False)
    status = Column(String(30), nullable=False, server_default="queued", index=True)
    identifiers_present = Column(JSONB, nullable=True)
    provider_response = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("Project")
    job = relationship("MessagingAudienceSyncJob", back_populates="items")
    user = relationship("MessagingUser")

    __table_args__ = (
        Index('ix_audience_sync_items_job_status', 'job_id', 'status'),
    )


class MessagingAudienceWebhookEndpoint(Base):
    """
    Project-level webhook endpoint for audience sync/job state changes.

    The secret is used only for HMAC signing and is not returned by the API.
    """
    __tablename__ = "messaging_audience_webhook_endpoints"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    url = Column(String(1000), nullable=False)
    secret = Column(Text, nullable=True)
    events = Column(JSONB, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("true"), index=True)
    last_delivery_status = Column(String(30), nullable=True)
    last_delivered_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    deliveries = relationship("MessagingAudienceWebhookDelivery", back_populates="endpoint", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_audience_webhook_endpoints_project_active', 'project_id', 'is_active'),
    )


class MessagingAudienceWebhookDelivery(Base):
    """
    Log-safe webhook delivery queue. Payloads contain counts and provider IDs,
    never raw audience member PII.
    """
    __tablename__ = "messaging_audience_webhook_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    webhook_endpoint_id = Column(Integer, ForeignKey("messaging_audience_webhook_endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    audience_id = Column(Integer, ForeignKey("messaging_audiences.id", ondelete="SET NULL"), nullable=True, index=True)
    audience_destination_id = Column(Integer, ForeignKey("messaging_audience_destinations.id", ondelete="SET NULL"), nullable=True, index=True)
    sync_job_id = Column(Integer, ForeignKey("messaging_audience_sync_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    event_name = Column(String(100), nullable=False, index=True)
    status = Column(String(30), nullable=False, server_default="queued", index=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    next_retry_at = Column(DateTime, nullable=True, index=True)
    delivered_at = Column(DateTime, nullable=True)
    response_status = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    payload_summary = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    endpoint = relationship("MessagingAudienceWebhookEndpoint", back_populates="deliveries")
    audience = relationship("MessagingAudience")
    audience_destination = relationship("MessagingAudienceDestination")
    sync_job = relationship("MessagingAudienceSyncJob")

    __table_args__ = (
        Index('ix_audience_webhook_deliveries_due', 'status', 'next_retry_at'),
        Index('ix_audience_webhook_deliveries_project_created', 'project_id', 'created_at'),
    )


# ============================================================================
# Identity Resolution Models
# ============================================================================

class ContactIdentity(Base):
    """
    Stores all known identities for a contact (email, phone, anonymous_id, channel).
    Enables identity resolution and contact merging.
    """
    __tablename__ = "contact_identities"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    identity_type = Column(String(30), nullable=False)  # anonymous_id, contact_id, email, phone, channel
    identity_value = Column(String(500), nullable=False)
    channel_instance_id = Column(Integer, nullable=True)
    verified = Column(Boolean, default=False, nullable=False)
    source = Column(String(30), nullable=False)  # snippet, identify, inbound_message, manual, api
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    user = relationship("MessagingUser", back_populates="identities")

    __table_args__ = (
        UniqueConstraint('project_id', 'identity_type', 'identity_value',
                         name='uq_contact_identity_type_value'),
        Index('ix_contact_identities_user', 'project_id', 'user_id'),
    )


class ContactMergeLog(Base):
    """
    Audit log for contact merges. Stores snapshots of both contacts pre-merge.
    """
    __tablename__ = "contact_merge_logs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    winner_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True)
    loser_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True)
    triggered_by = Column(String(30), nullable=False)  # identify, system_auto, manual, api
    triggered_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    snapshot_winner = Column(JSONB, nullable=False)
    snapshot_loser = Column(JSONB, nullable=False)
    identities_transferred = Column(JSONB, nullable=True)
    properties_resolved = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    undone_at = Column(DateTime, nullable=True)

    # Relationships
    winner = relationship("MessagingUser", foreign_keys=[winner_id])
    loser = relationship("MessagingUser", foreign_keys=[loser_id])

    __table_args__ = (
        Index('ix_merge_logs_project_date', 'project_id', 'created_at'),
    )


class Account(Base):
    """
    Account entity for B2B grouping of contacts.
    """
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=True)
    properties = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="accounts")
    contact_associations = relationship("ContactAccountAssociation", back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint('project_id', 'external_id', name='uq_account_project_external'),
    )


class ContactAccountAssociation(Base):
    """
    Many-to-many between contacts and accounts.
    """
    __tablename__ = "contact_account_assoc"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(50), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    user = relationship("MessagingUser", back_populates="account_associations")
    account = relationship("Account", back_populates="contact_associations")

    __table_args__ = (
        UniqueConstraint('project_id', 'user_id', 'account_id', name='uq_contact_account_assoc'),
    )


class MergeSuggestion(Base):
    """
    Suggested contact merge for review. Created when phone-only matches are found.
    """
    __tablename__ = "merge_suggestions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    contact_a_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    contact_b_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    match_reason = Column(String(100), nullable=False)  # phone_match, manual_review, funnel_conflict
    match_confidence = Column(String(20), nullable=False)  # high, medium, low
    status = Column(String(20), default='pending', nullable=False)  # pending, accepted, rejected, expired
    reviewed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    contact_a = relationship("MessagingUser", foreign_keys=[contact_a_id])
    contact_b = relationship("MessagingUser", foreign_keys=[contact_b_id])
    reviewer = relationship("User", foreign_keys=[reviewed_by])

    __table_args__ = (
        Index('ix_merge_suggestions_status', 'project_id', 'status'),
    )
