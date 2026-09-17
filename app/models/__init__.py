import uuid as _uuid

from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Date, Text, Boolean, ForeignKey, JSON, CheckConstraint, ARRAY, Float, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
from app.models.journey import JourneyNode, JourneyEdge, JourneyIntervention, JourneySnapshot, JourneyBackfillJob  # noqa: F401

# WhatsApp Instance Models will be defined below

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    keycloak_id = Column(String(100), unique=True, index=True, nullable=True)
    role = Column(String(50), default="user", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    social_provider = Column(String(50), nullable=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", use_alter=True, name="fk_user_workspace"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    last_login = Column(DateTime, nullable=True)
    
    # Relationships
    workspace = relationship("Workspace", foreign_keys=[workspace_id], back_populates="users")
    whatsapp_instances = relationship("WhatsAppInstance", back_populates="user")
    email_instances = relationship("EmailInstance", back_populates="user")
    project_memberships = relationship("ProjectMember", foreign_keys="ProjectMember.user_id", back_populates="user", cascade="all, delete-orphan")
    push_subscriptions = relationship("PushSubscription", back_populates="user", cascade="all, delete-orphan")
    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")

class Workspace(Base):
    __tablename__ = "workspaces"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    users = relationship("User", foreign_keys="User.workspace_id", back_populates="workspace")
    owner = relationship("User", foreign_keys=[owner_id])
    projects = relationship("Project", back_populates="workspace")
    whatsapp_instances = relationship("WhatsAppInstance", back_populates="workspace")
    email_instances = relationship("EmailInstance", back_populates="workspace")

class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    pii_salt = Column(String(64), nullable=True)  # GDPR: Project-specific salt for PII hashing
    inbox_ttl_waiting_agent = Column(Integer, nullable=True)  # Minutes before unassigned ticket expires
    inbox_ttl_waiting_customer = Column(Integer, nullable=True)  # Minutes before waiting_customer ticket expires
    goal_event = Column(String(255), nullable=True)  # Event graph conversion goal
    # i18n / multi-market — locale is a content-variant axis (see docs/versya-i18n-multimarket-PLAN.md)
    default_locale = Column(String(10), nullable=False, server_default="pt-BR")
    supported_locales = Column(JSON, nullable=True)  # array; null ⇒ [default_locale]
    default_timezone = Column(String(40), nullable=False, server_default="America/Sao_Paulo")
    market_config = Column(JSONB, nullable=True)  # explicit markets; locale remains a content axis
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    workspace = relationship("Workspace", back_populates="projects")
    smtp_configs = relationship("CustomerSMTPConfig", back_populates="project")
    whatsapp_configs = relationship("CustomerWhatsAppConfig", back_populates="project")
    whatsapp_instances = relationship("WhatsAppInstance", back_populates="project")
    email_instances = relationship("EmailInstance", back_populates="project")
    funnels = relationship("Funnel", back_populates="project", cascade="all, delete-orphan")
    postforme_credential = relationship("PostForMeCredential", back_populates="project", uselist=False)
    postforme_social_accounts = relationship("PostForMeSocialAccount", back_populates="project")
    postforme_posts = relationship("PostForMePost", back_populates="project")
    postforme_media = relationship("PostForMeMedia", back_populates="project")
    postforme_webhooks = relationship("PostForMeWebhook", back_populates="project")
    chatbots = relationship("Chatbot", back_populates="project")
    agent_teams = relationship("AgentTeam", back_populates="project", cascade="all, delete-orphan")
    llm_configs = relationship("ProjectLLMConfig", back_populates="project", cascade="all, delete-orphan")
    api_connections = relationship("ProjectApiConnection", back_populates="project", cascade="all, delete-orphan")
    score_definitions = relationship("ScoreDefinition", back_populates="project", cascade="all, delete-orphan")
    policies = relationship("ProjectPolicy", back_populates="project", uselist=False, cascade="all, delete-orphan")
    personalization_config = relationship("ProjectPersonalizationConfig", back_populates="project", uselist=False, cascade="all, delete-orphan")
    segment_rules = relationship("SegmentRule", back_populates="project", cascade="all, delete-orphan", order_by="SegmentRule.priority")
    routing_states = relationship("ContactRoutingState", back_populates="project", cascade="all, delete-orphan")
    inbox_rules = relationship("InboxAssignmentRule", back_populates="project", cascade="all, delete-orphan", order_by="InboxAssignmentRule.priority")
    media_assets = relationship("ProjectMediaAsset", back_populates="project", cascade="all, delete-orphan")
    knowledge_assets = relationship("KnowledgeAsset", back_populates="project", cascade="all, delete-orphan")
    knowledge_collections = relationship("KnowledgeCollection", back_populates="project", cascade="all, delete-orphan")
    accounts = relationship("Account", back_populates="project", cascade="all, delete-orphan")
    webhook_sources = relationship("WebhookSource", back_populates="project", cascade="all, delete-orphan")
    markets = relationship(
        "ProjectMarket",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectMarket.key",
    )

    # Messaging Middleware relationships
    messaging_domains = relationship("MessagingDomain", back_populates="project", cascade="all, delete-orphan")
    messaging_api_keys = relationship("MessagingApiKey", back_populates="project", cascade="all, delete-orphan")
    messaging_channels = relationship("MessagingChannel", back_populates="project", cascade="all, delete-orphan")
    messaging_templates = relationship("MessagingTemplate", back_populates="project", cascade="all, delete-orphan")
    messaging_users = relationship("MessagingUser", back_populates="project", cascade="all, delete-orphan")
    messaging_events = relationship("MessagingEvent", back_populates="project", cascade="all, delete-orphan")
    messaging_logs = relationship("MessagingLog", back_populates="project", cascade="all, delete-orphan")
    messaging_anonymous_profiles = relationship("MessagingAnonymousProfile", back_populates="project", cascade="all, delete-orphan")
    send_config = relationship("ProjectSendConfig", back_populates="project", uselist=False, cascade="all, delete-orphan")
    send_logs = relationship("SendLog", back_populates="project", cascade="all, delete-orphan")
    members = relationship("ProjectMember", back_populates="project", cascade="all, delete-orphan")
    channel_configs = relationship("ProjectChannelConfig", back_populates="project", cascade="all, delete-orphan")
    web_widget_configs = relationship("ChatWidgetConfig", back_populates="project", cascade="all, delete-orphan")
    nocode_mappings = relationship("NoCodeMapping", back_populates="project", cascade="all, delete-orphan")
    nocode_config_snapshots = relationship("NoCodeConfigSnapshot", back_populates="project", cascade="all, delete-orphan")
    variables = relationship("ProjectVariable", back_populates="project", cascade="all, delete-orphan")
    mes_scores = relationship("MessageEffectivenessScore", back_populates="project", cascade="all, delete-orphan")
    mes_config = relationship("MESConfig", back_populates="project", uselist=False, cascade="all, delete-orphan")
    journey_snapshots = relationship("JourneySnapshot", back_populates="project", cascade="all, delete-orphan")
    locale_channel_maps = relationship("LocaleChannelMap", back_populates="project", cascade="all, delete-orphan")
    locale_policy_overrides = relationship("LocalePolicyOverride", back_populates="project", cascade="all, delete-orphan")
    setup_plans = relationship("ProjectSetupPlan", cascade="all, delete-orphan")


class LocalePolicyOverride(Base):
    """i18n — sparse per-locale Guardian override. Unset fields inherit the project's
    base ProjectPolicy. Quiet hours are evaluated in the contact's timezone."""
    __tablename__ = "locale_policy_overrides"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    locale = Column(String(10), nullable=False)        # BCP-47
    quiet_hours = Column(JSON, nullable=True)          # {enabled,start,end,channels,timezone}
    timezone = Column(String(40), nullable=True)
    contact_caps = Column(JSON, nullable=True)
    channel_cooldowns = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("Project", back_populates="locale_policy_overrides")

    __table_args__ = (
        UniqueConstraint("project_id", "locale", name="uq_loc_policy_proj_locale"),
    )


class LocaleChannelMap(Base):
    """i18n — optional per-locale sender routing. Maps (locale, channel_type) → the
    channel-specific instance to send from. Unmapped ⇒ caller's default instance."""
    __tablename__ = "locale_channel_map"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    locale = Column(String(10), nullable=False)        # BCP-47
    channel_type = Column(String(20), nullable=False)  # whatsapp|email|sms|...
    instance_id = Column(Integer, nullable=False)      # id in the channel-specific instance table
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("Project", back_populates="locale_channel_maps")

    __table_args__ = (
        UniqueConstraint("project_id", "locale", "channel_type", name="uq_loc_chan_proj_loc_chan"),
    )


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    phone = Column(String(20), nullable=True)
    address = Column(String(500), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

class CustomerSMTPConfig(Base):
    __tablename__ = "customer_smtp_configs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    customer_id = Column(Integer, nullable=True)  # Made nullable for project-based configs
    smtp_server = Column(String(255), nullable=False)
    smtp_port = Column(Integer, nullable=False)
    smtp_username = Column(String(255), nullable=False)
    smtp_password = Column(Text, nullable=False)  # encrypted
    smtp_use_tls = Column(Boolean, default=True, nullable=False)
    smtp_use_ssl = Column(Boolean, default=False, nullable=False)
    from_email = Column(String(255), nullable=False)
    from_name = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    # Inbound email fields
    inbound_enabled = Column(Boolean, default=False, nullable=False)
    inbound_provider = Column(String(30), nullable=True)  # ses_inbound | mailgun_inbound | sendgrid_inbound | imap_poll | cloudflare_email_workers
    inbound_webhook_secret = Column(Text, nullable=True)  # encrypted
    mx_record_status = Column(String(20), nullable=True)  # pending | verified | failed
    mx_record_verified_at = Column(DateTime, nullable=True)
    # Delivery feedback webhook
    feedback_webhook_secret = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="smtp_configs")
    inbound_addresses = relationship("EmailInboundAddress", back_populates="smtp_config", cascade="all, delete-orphan")

    @property
    def is_bidirectional(self):
        return self.inbound_enabled and self.mx_record_status == "verified"


class EmailInboundAddress(Base):
    __tablename__ = "email_inbound_addresses"

    id = Column(Integer, primary_key=True, index=True)
    instance_id = Column(Integer, ForeignKey("customer_smtp_configs.id", ondelete="CASCADE"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    address = Column(String(255), nullable=False)
    label = Column(String(100), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    default_handler_type = Column(String(50), nullable=True)
    default_handler_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("instance_id", "address", name="uq_inb_addr_inst_addr"),
        Index("ix_email_inb_addr_project", "project_id"),
    )

    # Relationships
    smtp_config = relationship("CustomerSMTPConfig", back_populates="inbound_addresses")
    project = relationship("Project")

class CustomerWhatsAppConfig(Base):
    __tablename__ = "customer_whatsapp_configs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    customer_id = Column(Integer, nullable=True)  # Made nullable for project-based configs
    instance_name = Column(String(100), nullable=False)
    evolution_instance_id = Column(String(100), nullable=True)
    connection_status = Column(String(50), default="disconnected", nullable=False)
    qr_code = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    project = relationship("Project", back_populates="whatsapp_configs")


class WhatsAppInstance(Base):
    __tablename__ = "whatsapp_instances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)

    # Provider discriminator
    provider_type = Column(String(30), default="evolution_api", nullable=False)  # evolution_api | meta_cloud_api

    # Instance details (Evolution API)
    instance_name = Column(String(100), nullable=False, unique=True, index=True)
    instance_key = Column(String(255), nullable=True)  # Evolution API key stored securely
    evolution_instance_id = Column(String(100), nullable=True)  # Evolution internal ID

    # Meta Cloud API fields
    meta_phone_number_id = Column(String(100), nullable=True)
    meta_waba_id = Column(String(100), nullable=True)
    meta_access_token_enc = Column(Text, nullable=True)  # Fernet-encrypted
    meta_app_secret_enc = Column(Text, nullable=True)  # Fernet-encrypted
    meta_webhook_verify_token = Column(String(255), nullable=True)
    meta_business_name = Column(String(255), nullable=True)

    # Phone normalization
    default_country_code = Column(String(5), nullable=True)  # e.g. "55" for Brazil

    # Connection status
    connection_status = Column(String(20), default="disconnected")  # disconnected, connecting, connected
    phone_number = Column(String(20), nullable=True)  # WhatsApp phone number when connected

    # QR Code data
    qr_code_data = Column(Text, nullable=True)  # Base64 QR code image
    qr_expires_at = Column(DateTime, nullable=True)

    # Configuration
    is_active = Column(Boolean, default=True)
    webhook_url = Column(String(255), nullable=True)

    # Metadata
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    last_connected_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="whatsapp_instances")
    workspace = relationship("Workspace", back_populates="whatsapp_instances")
    project = relationship("Project", back_populates="whatsapp_instances")
    contact_windows = relationship("WhatsAppContactWindow", back_populates="instance", cascade="all, delete-orphan")


class EmailInstance(Base):
    __tablename__ = "email_instances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)

    # Provider discriminator
    provider_type = Column(String(30), default="smtp", nullable=False)  # smtp | api

    instance_name = Column(String(100), nullable=False, unique=True)
    from_email = Column(String(255), nullable=False)
    from_name = Column(String(100), nullable=True)

    # SMTP columns
    smtp_server = Column(String(255), nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_username = Column(String(255), nullable=True)
    smtp_password_enc = Column(Text, nullable=True)  # Fernet-encrypted
    smtp_use_tls = Column(Boolean, default=True)
    smtp_use_ssl = Column(Boolean, default=False)

    # API columns
    api_key_enc = Column(Text, nullable=True)  # Fernet-encrypted
    api_config = Column(JSON, default=dict)  # Full API configuration

    # Feedback webhook
    feedback_webhook_secret = Column(String(64), nullable=True)

    # Lifecycle
    connection_status = Column(String(20), default="pending")  # pending | verified | failed
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    last_verified_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="email_instances")
    workspace = relationship("Workspace", back_populates="email_instances")
    project = relationship("Project", back_populates="email_instances")


class WhatsAppContactWindow(Base):
    __tablename__ = "whatsapp_contact_windows"
    __table_args__ = (
        UniqueConstraint("instance_id", "contact_phone", name="uq_instance_contact"),
        Index("ix_window_expires", "window_expires_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    instance_id = Column(Integer, ForeignKey("whatsapp_instances.id", ondelete="CASCADE"), nullable=False)
    contact_phone = Column(String(50), nullable=False)
    window_opens_at = Column(DateTime, nullable=False)
    window_expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    instance = relationship("WhatsAppInstance", back_populates="contact_windows")


class MetaPageConnection(Base):
    """A Facebook Page connected via Facebook Login for Business.

    One row per (project, page). Serves BOTH the `messenger` channel and,
    when a professional Instagram account is linked to the page, the
    `instagram` channel — the two derived channel instances share this
    row's id as their instance_id.
    """
    __tablename__ = "meta_page_connections"
    __table_args__ = (
        UniqueConstraint("project_id", "page_id", name="uq_meta_page_conn_proj_page"),
        Index("ix_meta_page_conn_page_id", "page_id"),
        Index("ix_meta_page_conn_ig_id", "ig_account_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    page_id = Column(String(64), nullable=False)
    page_name = Column(String(255), nullable=True)
    page_access_token_enc = Column(Text, nullable=True)  # Fernet-encrypted

    # Linked Instagram professional account (nullable when page has none)
    ig_account_id = Column(String(64), nullable=True)
    ig_username = Column(String(255), nullable=True)

    subscribed_fields = Column(JSON, nullable=True)

    messenger_enabled = Column(Boolean, default=True, nullable=False)
    instagram_enabled = Column(Boolean, default=False, nullable=False)
    fb_comments_enabled = Column(Boolean, default=False, nullable=False)
    ig_comments_enabled = Column(Boolean, default=False, nullable=False)

    status = Column(String(30), default="connected", nullable=False)  # connected | error | disconnected
    last_error = Column(Text, nullable=True)

    connected_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    messaging_windows = relationship(
        "MetaMessagingWindow", back_populates="connection", cascade="all, delete-orphan"
    )


class MetaOAuthSession(Base):
    """Short-lived server-side state for the Facebook Login for Business popup flow."""
    __tablename__ = "meta_oauth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    state = Column(String(64), nullable=False, unique=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), default="pending", nullable=False)  # pending | authorized | completed | error
    user_token_enc = Column(Text, nullable=True)  # Fernet-encrypted long-lived user token; purged on finalize
    pages_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    expires_at = Column(DateTime, nullable=False)


class MetaMessagingWindow(Base):
    """24-hour customer-service window per (connection, platform, PSID/IGSID)."""
    __tablename__ = "meta_messaging_windows"
    __table_args__ = (
        UniqueConstraint("connection_id", "platform", "contact_id", name="uq_meta_window_conn_plat_contact"),
        Index("ix_meta_window_expires", "window_expires_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    connection_id = Column(Integer, ForeignKey("meta_page_connections.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String(20), nullable=False)  # messenger | instagram
    contact_id = Column(String(64), nullable=False)  # PSID / IGSID
    window_opens_at = Column(DateTime, nullable=False)
    window_expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    connection = relationship("MetaPageConnection", back_populates="messaging_windows")


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"
    
    id = Column(Integer, primary_key=True, index=True)
    instance_id = Column(Integer, ForeignKey("whatsapp_instances.id"), nullable=False)
    
    # Message details
    message_id = Column(String(100), nullable=False, index=True)  # WhatsApp message ID
    from_number = Column(String(20), nullable=False)
    to_number = Column(String(20), nullable=False)
    message_type = Column(String(20), nullable=False)  # text, image, document, etc.
    content = Column(Text, nullable=True)  # Message content
    media_url = Column(String(500), nullable=True)  # Media file URL if applicable
    
    # Status
    status = Column(String(20), default="sent")  # sent, delivered, read, failed
    direction = Column(String(10), nullable=False)  # inbound, outbound
    
    # Timestamps
    sent_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    
    # Relationships
    instance = relationship("WhatsAppInstance")


class Funnel(Base):
    """Multi-step, multi-channel automated communication funnel."""
    __tablename__ = "funnels"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), default="draft", nullable=False, index=True)  # draft, active, paused, archived
    trigger_type = Column(String(20), nullable=False, index=True)  # event, segment
    trigger_config = Column(JSON, default={}, nullable=True)
    global_exit_config = Column(JSON, default={}, nullable=True)
    # Optional business purpose governed by ProjectLifecycleCutover. Funnels
    # without a purpose keep their historical behavior; purpose-aware funnels
    # may execute only while Versya owns that purpose.
    purpose_key = Column(String(120), nullable=True, index=True)
    # Explicit episode-entry semantics shared with campaigns/Event Actions.
    # Priority decides attention; this policy decides whether existing
    # episodes remain, exit, suspend, or reject the new entry.
    attention_policy = Column(JSON, nullable=True)
    debug_mode = Column(Boolean, default=False, nullable=False)  # When True, outbound messages require inbox approval
    source = Column(String(50), default="user", server_default="user", nullable=False, index=True)  # 'user' or 'event_action'
    is_system = Column(Boolean, default=False, server_default="false", nullable=False)
    event_action_id = Column(Integer, ForeignKey("event_actions.id", ondelete="CASCADE"), nullable=True, index=True)
    cooldown_seconds = Column(Integer, nullable=True)
    react_to_delivery = Column(Boolean, default=False, server_default="false", nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="funnels")
    creator = relationship("User")
    steps = relationship("FunnelStep", back_populates="funnel", cascade="all, delete-orphan", order_by="FunnelStep.position")
    enrollments = relationship("FunnelEnrollment", back_populates="funnel", cascade="all, delete-orphan")
    event_action = relationship("EventAction", back_populates="system_funnel", foreign_keys=[event_action_id])

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name='valid_funnel_status'
        ),
        CheckConstraint(
            "trigger_type IN ('event', 'segment')",
            name='valid_funnel_trigger_type'
        ),
    )


class FunnelStep(Base):
    """Step within a funnel journey."""
    __tablename__ = "funnel_steps"

    id = Column(Integer, primary_key=True, index=True)
    funnel_id = Column(Integer, ForeignKey("funnels.id", ondelete="CASCADE"), nullable=False, index=True)
    step_type = Column(String(20), nullable=False, index=True)  # wait, condition, action, exit, wait_for_reply, wait_until, fork, send_message
    step_config = Column(JSON, default={}, nullable=True)
    position = Column(Integer, default=0, nullable=False)
    parent_step_id = Column(Integer, ForeignKey("funnel_steps.id", ondelete="SET NULL"), nullable=True, index=True)
    branch = Column(String(20), default="main", nullable=False)  # main, yes, no, path_0..4, exit_0..4
    # Stable authored identity (slot-ID contract, ruling 14). Survives recompile
    # so learning/arm identity keyed on it is never orphaned. For user-authored
    # steps the default mints it once at creation (the row persists across edits);
    # for compiled steps the compiler PROPAGATES it from the authored object.
    slot_id = Column(String(36), nullable=True, index=True, default=lambda: str(_uuid.uuid4()))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    funnel = relationship("Funnel", back_populates="steps")
    parent_step = relationship("FunnelStep", remote_side="FunnelStep.id", backref="children_steps")

    __table_args__ = (
        Index('ix_funnel_steps_funnel_position', 'funnel_id', 'position'),
        CheckConstraint(
            "step_type IN ('wait', 'condition', 'action', 'exit', 'wait_for_reply', 'wait_until', 'fork', 'send_message')",
            name='valid_funnel_step_type'
        ),
        CheckConstraint(
            "branch ~ '^(main|yes|no|path_[0-9]+|exit_[0-9]+)$'",
            name='valid_funnel_step_branch'
        ),
    )


class FunnelEnrollment(Base):
    """User enrollment in a funnel."""
    __tablename__ = "funnel_enrollments"

    id = Column(Integer, primary_key=True, index=True)
    funnel_id = Column(Integer, ForeignKey("funnels.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), default="active", nullable=False, index=True)  # active, completed, exited
    current_step_id = Column(Integer, ForeignKey("funnel_steps.id", ondelete="SET NULL"), nullable=True, index=True)
    current_branch = Column(String(20), default="main", nullable=False)  # main, yes, no, path_0..4, exit_0..4
    messages_sent = Column(Integer, default=0, nullable=False)
    thread_count = Column(Integer, nullable=True)
    exit_reason = Column(String(30), nullable=True)
    exited_at = Column(DateTime, nullable=True)
    enrollment_metadata = Column(JSON, default={}, nullable=True)
    enrolled_at = Column(DateTime, server_default=func.now(), nullable=False)
    entered_step_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    funnel = relationship("Funnel", back_populates="enrollments")
    user = relationship("MessagingUser")
    current_step = relationship("FunnelStep")
    logs = relationship("FunnelEnrollmentLog", back_populates="enrollment", cascade="all, delete-orphan")
    threads = relationship("FunnelEnrollmentThread", back_populates="enrollment", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_funnel_enrollments_funnel_user', 'funnel_id', 'user_id'),
        Index('ix_funnel_enrollments_enrolled_at', 'enrolled_at'),
        CheckConstraint(
            "status IN ('active', 'completed', 'exited', 'merged_out')",
            name='valid_enrollment_status'
        ),
    )


class FunnelEnrollmentThread(Base):
    """Thread within a forked enrollment — each runs a path independently."""
    __tablename__ = "funnel_enrollment_threads"

    id = Column(Integer, primary_key=True, index=True)
    enrollment_id = Column(Integer, ForeignKey("funnel_enrollments.id", ondelete="CASCADE"), nullable=False, index=True)
    thread_index = Column(Integer, nullable=False)
    fork_step_id = Column(Integer, ForeignKey("funnel_steps.id", ondelete="SET NULL"), nullable=True, index=True)
    parent_thread_id = Column(Integer, ForeignKey("funnel_enrollment_threads.id", ondelete="SET NULL"), nullable=True)
    label = Column(String(100), nullable=True)
    status = Column(String(20), default="active", nullable=False)  # active, completed, exited, forked
    current_step_id = Column(Integer, ForeignKey("funnel_steps.id", ondelete="SET NULL"), nullable=True)
    current_branch = Column(String(20), default="main", nullable=False)
    entered_step_at = Column(DateTime, server_default=func.now())
    created_at = Column(DateTime, server_default=func.now())

    # Relationships
    enrollment = relationship("FunnelEnrollment", back_populates="threads")
    current_step = relationship("FunnelStep", foreign_keys=[current_step_id])
    fork_step = relationship("FunnelStep", foreign_keys=[fork_step_id])
    parent_thread = relationship("FunnelEnrollmentThread", remote_side="FunnelEnrollmentThread.id")

    __table_args__ = (
        UniqueConstraint('enrollment_id', 'thread_index', name='uq_enrollment_thread_idx'),
        CheckConstraint(
            "status IN ('active', 'completed', 'exited', 'forked')",
            name='valid_thread_status'
        ),
    )


class FunnelEnrollmentLog(Base):
    """Audit log for enrollment progression."""
    __tablename__ = "funnel_enrollment_logs"

    id = Column(Integer, primary_key=True, index=True)
    enrollment_id = Column(Integer, ForeignKey("funnel_enrollments.id", ondelete="CASCADE"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("funnel_steps.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(50), nullable=False)  # entered, advanced, action_executed, action_failed, exited
    details = Column(JSON, default={}, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    enrollment = relationship("FunnelEnrollment", back_populates="logs")
    step = relationship("FunnelStep")


# ==========================================
# Post for Me Integration Models
# ==========================================

class PostForMeCredential(Base):
    """Post for Me API credentials (project-level)"""
    __tablename__ = "postforme_credentials"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True)

    # Encrypted API key
    api_key_encrypted = Column(Text, nullable=False)

    # Account info from Post for Me
    postforme_user_id = Column(String(255), nullable=True)
    account_email = Column(String(255), nullable=True)

    # Status
    is_active = Column(Boolean, default=True, nullable=False)
    last_validated_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationships
    project = relationship("Project", back_populates="postforme_credential")
    social_accounts = relationship("PostForMeSocialAccount", back_populates="credential", cascade="all, delete-orphan")
    webhooks = relationship("PostForMeWebhook", back_populates="credential", cascade="all, delete-orphan")
    creator = relationship("User")


class PostForMeSocialAccount(Base):
    """Connected social media accounts via Post for Me"""
    __tablename__ = "postforme_social_accounts"

    id = Column(Integer, primary_key=True, index=True)
    credential_id = Column(Integer, ForeignKey("postforme_credentials.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # Post for Me account details
    postforme_account_id = Column(String(255), nullable=False, unique=True, index=True)

    # Platform info
    platform = Column(String(50), nullable=False, index=True)
    account_name = Column(String(255), nullable=True)
    account_username = Column(String(255), nullable=True)
    account_profile_url = Column(Text, nullable=True)

    # Connection status
    is_connected = Column(Boolean, default=True, nullable=False)
    last_sync_at = Column(DateTime, nullable=True)
    connection_status = Column(String(50), default='active', nullable=False)

    # Platform-specific metadata
    platform_metadata = Column(JSON, default={}, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    credential = relationship("PostForMeCredential", back_populates="social_accounts")
    project = relationship("Project", back_populates="postforme_social_accounts")
    post_results = relationship("PostForMePostResult", back_populates="social_account")

    __table_args__ = (
        CheckConstraint(
            platform.in_([
                'facebook', 'instagram', 'twitter', 'tiktok',
                'youtube', 'pinterest', 'linkedin', 'bluesky', 'threads'
            ]),
            name='valid_platform'
        ),
    )


class PostForMePost(Base):
    """Social media posts created via Post for Me"""
    __tablename__ = "postforme_posts"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # Post for Me reference
    postforme_post_id = Column(String(255), unique=True, nullable=True, index=True)

    # Content (JSONB with platform-specific overrides)
    content = Column(JSON, nullable=False)

    # Target accounts (array of postforme_account_ids)
    target_account_ids = Column(ARRAY(String), nullable=False)

    # Scheduling
    status = Column(String(50), default='draft', nullable=False, index=True)
    scheduled_time = Column(DateTime, nullable=True, index=True)
    published_at = Column(DateTime, nullable=True)

    # External reference (for workflow integration)
    external_id = Column(String(255), nullable=True, index=True)

    # Metadata
    tags = Column(ARRAY(String), nullable=True)
    post_metadata = Column(JSON, default={}, nullable=True)

    # Audit
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="postforme_posts")
    creator = relationship("User")
    results = relationship("PostForMePostResult", back_populates="post", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            status.in_(['draft', 'scheduled', 'publishing', 'published', 'failed', 'cancelled']),
            name='valid_post_status'
        ),
    )


class PostForMePostResult(Base):
    """Platform-specific publishing results for posts"""
    __tablename__ = "postforme_post_results"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("postforme_posts.id", ondelete="CASCADE"), nullable=False, index=True)

    # Post for Me reference
    postforme_result_id = Column(String(255), unique=True, nullable=True, index=True)

    # Platform details
    social_account_id = Column(Integer, ForeignKey("postforme_social_accounts.id"), nullable=False, index=True)
    platform = Column(String(50), nullable=False)

    # Publishing status
    status = Column(String(50), nullable=False, index=True)

    # Platform post URL and ID
    platform_post_id = Column(String(255), nullable=True)
    platform_post_url = Column(Text, nullable=True)

    # Error details (if failed)
    error_message = Column(Text, nullable=True)
    error_code = Column(String(50), nullable=True)

    # Platform-specific response
    platform_response = Column(JSON, default={}, nullable=True)

    # Timestamps
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    post = relationship("PostForMePost", back_populates="results")
    social_account = relationship("PostForMeSocialAccount", back_populates="post_results")

    __table_args__ = (
        CheckConstraint(
            status.in_(['pending', 'published', 'failed']),
            name='valid_result_status'
        ),
    )


class PostForMeMedia(Base):
    """Media assets for Post for Me posts"""
    __tablename__ = "postforme_media"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # Post for Me media reference
    postforme_media_id = Column(String(255), unique=True, nullable=True, index=True)

    # File details
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(50), nullable=True)  # 'image', 'video', 'gif'
    file_size = Column(Integer, nullable=True)  # bytes
    mime_type = Column(String(100), nullable=True)

    # URLs
    upload_url = Column(Text, nullable=True)  # Signed URL for uploading (temporary)
    permanent_url = Column(Text, nullable=True)  # URL after upload

    # Upload status
    status = Column(String(50), default='pending', nullable=False, index=True)
    upload_expires_at = Column(DateTime, nullable=True)

    # Usage tracking
    used_in_posts = Column(ARRAY(Integer), nullable=True)  # Array of post IDs

    # Metadata
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    duration = Column(Integer, nullable=True)  # for videos (seconds)
    media_metadata = Column(JSON, default={}, nullable=True)

    # Audit
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="postforme_media")
    uploader = relationship("User")

    __table_args__ = (
        CheckConstraint(
            status.in_(['pending', 'uploaded', 'processing', 'ready', 'failed']),
            name='valid_media_status'
        ),
    )


class PostForMeWebhook(Base):
    """Webhook configurations for Post for Me"""
    __tablename__ = "postforme_webhooks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    credential_id = Column(Integer, ForeignKey("postforme_credentials.id", ondelete="CASCADE"), nullable=False)

    # Post for Me webhook reference
    postforme_webhook_id = Column(String(255), unique=True, nullable=True, index=True)

    # Webhook details
    url = Column(Text, nullable=False)
    events = Column(ARRAY(String), nullable=False)  # ['social-post.created', 'social-post.updated', etc.]

    # Security
    secret = Column(String(255), nullable=True)  # For signature verification

    # Status
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    last_triggered_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="postforme_webhooks")
    credential = relationship("PostForMeCredential", back_populates="webhooks")
    events_log = relationship("PostForMeWebhookEvent", back_populates="webhook", cascade="all, delete-orphan")


class PostForMeWebhookEvent(Base):
    """Log of webhook events received from Post for Me"""
    __tablename__ = "postforme_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    webhook_id = Column(Integer, ForeignKey("postforme_webhooks.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # Event details
    event_type = Column(String(100), nullable=False, index=True)
    payload = Column(JSON, nullable=False)

    # Processing
    processed = Column(Boolean, default=False, nullable=False, index=True)
    processed_at = Column(DateTime, nullable=True)
    processing_error = Column(Text, nullable=True)

    # Timestamps
    received_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    webhook = relationship("PostForMeWebhook", back_populates="events_log")
    project = relationship("Project")


# ============================================================================
# Chatbot Models
# ============================================================================

class Chatbot(Base):
    """
    Chatbot/Agent configuration for a project.
    Each project can have multiple chatbots with different purposes.
    """
    __tablename__ = "chatbots"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)

    # Basic info
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    intention = Column(String(500), nullable=True)  # Purpose: "onboard agent", "FAQ bot", etc.

    # Configuration
    model_provider = Column(String(50), default="openai", nullable=False)  # openai, google, etc.
    model_name = Column(String(100), default="gpt-4o-mini", nullable=False)
    temperature = Column(Float, default=0.7)
    max_tokens = Column(Integer, default=2000)
    system_prompt = Column(Text, nullable=True)

    # Embedding model for vector store (tracks which model was used)
    embedding_model = Column(String(100), nullable=True)

    # Integrations
    whatsapp_instance_id = Column(Integer, ForeignKey("whatsapp_instances.id"), nullable=True, index=True)
    auto_respond_whatsapp = Column(Boolean, default=False)

    # Status
    status = Column(String(50), default="active", nullable=False, index=True)  # active, paused, archived
    is_public = Column(Boolean, default=False)  # Public chatbot accessible without auth

    # Storage paths
    vector_store_path = Column(String(500), nullable=True)
    knowledge_base_path = Column(String(500), nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="chatbots")
    whatsapp_instance = relationship("WhatsAppInstance", foreign_keys=[whatsapp_instance_id])
    sessions = relationship("ChatSession", back_populates="chatbot", cascade="all, delete-orphan")
    knowledge_documents = relationship("KnowledgeDocument", back_populates="chatbot", cascade="all, delete-orphan")
    agent_routing = relationship("ChatbotAgentRouting", back_populates="chatbot", uselist=False, cascade="all, delete-orphan")
    widget_config = relationship("ChatWidgetConfig", back_populates="chatbot", uselist=False, cascade="all, delete-orphan")
    media_assets = relationship("ProjectMediaAsset", back_populates="chatbot", cascade="all, delete-orphan")


class ChatSession(Base):
    """
    Conversation session with a chatbot.
    Each user interaction creates a session that contains multiple messages.
    """
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    chatbot_id = Column(Integer, ForeignKey("chatbots.id"), nullable=False, index=True)

    # User identification
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # Null for anonymous/WhatsApp
    user_identifier = Column(String(200), nullable=False, index=True)  # Email, phone, or anonymous ID

    # Channel info
    channel = Column(String(50), default="web", nullable=False, index=True)  # web, whatsapp, api, sms
    whatsapp_message_id = Column(Integer, ForeignKey("whatsapp_messages.id"), nullable=True)

    # Session context
    context_data = Column(JSON, default={}, nullable=True)  # Store user context, metadata
    session_metadata = Column(JSON, default={}, nullable=True)  # Additional session info

    # Status
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    ended_at = Column(DateTime, nullable=True)

    # Human takeover support
    support_ticket_id = Column(Integer, nullable=True)  # FK added after SupportTicket creation
    human_takeover = Column(Boolean, default=False, nullable=False)
    human_takeover_at = Column(DateTime, nullable=True)
    bot_resumed_at = Column(DateTime, nullable=True)

    # Messaging provider support
    provider_id = Column(Integer, ForeignKey("messaging_providers.id", ondelete="SET NULL"), nullable=True)
    external_conversation_id = Column(String(255), nullable=True, index=True)  # External provider's conversation ID

    # Agent Teams support
    team_id = Column(Integer, ForeignKey("agent_teams.id", ondelete="SET NULL"), nullable=True, index=True)
    current_agent_id = Column(Integer, ForeignKey("specialist_agents.id", ondelete="SET NULL"), nullable=True, index=True)
    session_state = Column(String(30), nullable=True, index=True)  # INITIALIZED, ACTIVE, ROUTING, ESCALATED, RESOLVED, TIMED_OUT, CLOSED
    context_summary = Column(Text, nullable=True)  # Rolling conversation summary
    extracted_entities = Column(JSON, nullable=True)  # Extracted entities from conversation
    routing_history = Column(JSON, nullable=True)  # Array of routing decisions

    # Inbound router support
    handler_type = Column(String(50), nullable=True)  # mirrors ContactRoutingState handler_type
    thread_timeout_minutes = Column(Integer, nullable=True)
    last_inbound_at = Column(DateTime, nullable=True)

    # Contact bridge (cross-channel identity)
    contact_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)

    # Timestamps
    started_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    last_interaction_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    chatbot = relationship("Chatbot", back_populates="sessions")
    user = relationship("User")
    whatsapp_message = relationship("WhatsAppMessage", foreign_keys=[whatsapp_message_id])
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    provider = relationship("MessagingProvider", back_populates="sessions")
    support_ticket = relationship("SupportTicket", back_populates="session", uselist=False, foreign_keys="SupportTicket.session_id", passive_deletes=True)
    feedback = relationship("ConversationFeedback", back_populates="session", cascade="all, delete-orphan")
    team = relationship("AgentTeam", foreign_keys=[team_id])
    current_agent = relationship("SpecialistAgent", foreign_keys=[current_agent_id])
    contact = relationship("MessagingUser", foreign_keys=[contact_id])


class ChatMessage(Base):
    """
    Individual messages within a chat session.
    Stores both user queries and bot responses.
    """
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=False, index=True)

    # Message content
    role = Column(String(20), nullable=False, index=True)  # user, assistant, system
    content = Column(Text, nullable=False)

    # Sender tracking (for human intervention)
    sender_type = Column(String(20), nullable=True, index=True)  # bot, human_agent, customer
    sent_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)  # For agent messages

    # External message tracking
    external_message_id = Column(String(255), nullable=True, index=True)  # Provider's message ID
    delivery_status = Column(String(20), nullable=True)  # pending, sent, delivered, read, failed

    # Attachments
    images = Column(JSON, default=[], nullable=True)  # Array of image URLs/paths
    attachments = Column(JSON, default=[], nullable=True)  # Other file attachments

    # RAG context
    retrieved_documents = Column(JSON, default=[], nullable=True)  # Document chunks used for this response
    retrieval_metadata = Column(JSON, default={}, nullable=True)  # Scores, sources, etc.

    # Token usage tracking
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)

    # Metadata
    message_metadata = Column(JSON, default={}, nullable=True)

    # Agent Teams support
    agent_id = Column(Integer, ForeignKey("specialist_agents.id", ondelete="SET NULL"), nullable=True, index=True)
    routing_decision = Column(JSON, nullable=True)  # Router decision metadata for this message

    # Inbound router support
    content_pieces = Column(JSON, nullable=True)  # [{type, body, media_url, media_mime, resolved_text}]
    resolved_text = Column(Text, nullable=True)  # Full assembled text after media processing

    # Channel tracking (per-message, for omnichannel timelines)
    channel = Column(String(50), nullable=True, index=True)

    # Timestamps
    timestamp = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    # Relationships
    session = relationship("ChatSession", back_populates="messages")
    sent_by = relationship("User")
    feedback = relationship("ConversationFeedback", back_populates="message")
    agent = relationship("SpecialistAgent", foreign_keys=[agent_id])


class KnowledgeDocument(Base):
    """
    Documents in a chatbot's knowledge base.
    Supports files (markdown, PDF, txt) and URLs.
    """
    __tablename__ = "knowledge_documents"

    id = Column(Integer, primary_key=True, index=True)
    chatbot_id = Column(Integer, ForeignKey("chatbots.id"), nullable=False, index=True)

    # Source info
    source_type = Column(String(50), nullable=False, index=True)  # file, url, text
    source_url = Column(String(1000), nullable=True)  # Original URL if scraped
    file_path = Column(String(500), nullable=True)  # Local file path
    file_name = Column(String(200), nullable=True)
    file_type = Column(String(50), nullable=True)  # markdown, pdf, txt, html

    # Content
    content = Column(Text, nullable=True)  # Extracted text content
    content_hash = Column(String(64), nullable=True, index=True)  # SHA256 hash for deduplication

    # Processing
    chunk_count = Column(Integer, default=0)  # Number of chunks created
    embedding_model = Column(String(100), nullable=True)
    processed = Column(Boolean, default=False, nullable=False, index=True)
    processing_error = Column(Text, nullable=True)

    # Metadata
    document_metadata = Column(JSON, default={}, nullable=True)  # Title, author, images, etc.

    # Timestamps
    uploaded_at = Column(DateTime, server_default=func.now(), nullable=False)
    processed_at = Column(DateTime, nullable=True)
    last_updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    chatbot = relationship("Chatbot", back_populates="knowledge_documents")


# ============================================================================
# Whitelist Signup Model (for static website)
# ============================================================================

class WhitelistSignup(Base):
    """
    Stores whitelist signup data from the static marketing website.
    Collects email, phone, country, and other relevant information
    from users interested in the Versya platform.
    """
    __tablename__ = "whitelist_signups"

    id = Column(Integer, primary_key=True, index=True)

    # Contact information
    email = Column(String(255), nullable=False, index=True)
    phone = Column(String(50), nullable=True)
    country = Column(String(100), nullable=True)

    # Additional fields
    name = Column(String(200), nullable=True)
    company = Column(String(200), nullable=True)
    suggestion = Column(Text, nullable=True)  # Feature suggestions or comments

    # Tracking
    ip_address = Column(String(45), nullable=True)  # IPv6 compatible
    user_agent = Column(String(500), nullable=True)
    referrer = Column(String(500), nullable=True)
    utm_source = Column(String(100), nullable=True)
    utm_medium = Column(String(100), nullable=True)
    utm_campaign = Column(String(100), nullable=True)

    # Status
    is_verified = Column(Boolean, default=False, nullable=False)
    verified_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ============================================================================
# Unified Chat System Models
# ============================================================================

class MessagingProvider(Base):
    """
    Abstraction for messaging providers (Twilio SMS, Twilio WhatsApp, Evolution API).
    Stores encrypted credentials and configuration for each provider.
    """
    __tablename__ = "messaging_providers"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    chatbot_id = Column(Integer, ForeignKey("chatbots.id", ondelete="SET NULL"), nullable=True, index=True)

    # Provider type and identification
    provider_type = Column(String(50), nullable=False, index=True)  # evolution_api, twilio_sms, twilio_whatsapp
    name = Column(String(200), nullable=False)

    # Encrypted credentials (Fernet encrypted JSON)
    # For Twilio: {"account_sid": "...", "auth_token": "...", "phone_number": "..."}
    # For Evolution: {"instance_name": "...", "api_key": "..."}
    credentials_encrypted = Column(Text, nullable=True)

    # Phone number for display
    phone_number = Column(String(50), nullable=True)

    # Webhook configuration
    webhook_url = Column(String(500), nullable=True)
    webhook_secret = Column(String(255), nullable=True)

    # Status
    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)

    # Additional configuration
    provider_metadata = Column(JSON, default={}, nullable=True)
    last_used_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    chatbot = relationship("Chatbot")
    sessions = relationship("ChatSession", back_populates="provider")

    __table_args__ = (
        CheckConstraint(
            "provider_type IN ('evolution_api', 'twilio_sms', 'twilio_whatsapp')",
            name='valid_provider_type'
        ),
    )


class SupportTicket(Base):
    """
    Support ticket for human intervention in chat conversations.
    Tracks status, assignment, and metadata for support workflow.
    """
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    chatbot_id = Column(Integer, ForeignKey("chatbots.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)

    # Ticket identification
    ticket_number = Column(String(50), nullable=False, unique=True, index=True)  # PROJ-001 format

    # Status and priority
    status = Column(String(30), default="open", nullable=False, index=True)  # open, in_progress, waiting_customer, resolved, closed
    priority = Column(String(20), default="medium", nullable=False)  # low, medium, high, urgent

    # Assignment
    assigned_to_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # Takeover flags
    human_takeover = Column(Boolean, default=False, nullable=False)
    bot_can_resume = Column(Boolean, default=True, nullable=False)

    # Customer information
    customer_identifier = Column(String(255), nullable=False)  # phone, email, etc.
    customer_name = Column(String(200), nullable=True)
    customer_phone = Column(String(50), nullable=True)  # Phone number for WhatsApp replies (when identifier is LID)
    channel = Column(String(50), nullable=False)  # web, whatsapp, sms

    # Contact bridge (cross-channel identity)
    contact_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)

    # Tags and metadata
    tags = Column(ARRAY(String), nullable=True)
    ticket_metadata = Column(JSON, default={}, nullable=True)
    internal_notes = Column(Text, nullable=True)
    escalation_reason = Column(String(500), nullable=True)
    escalation_origin = Column(JSON, nullable=True)  # {"type": "funnel"|"chatbot"|"agent_team"|"router_default", "ref_id": int, "step_id": int|null, "thread_index": int|null, "return_action": "resume"|"exit"|"ignore"}

    # Timestamps
    first_response_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    chatbot = relationship("Chatbot")
    session = relationship("ChatSession", back_populates="support_ticket")
    assigned_to = relationship("User")
    contact = relationship("MessagingUser", foreign_keys=[contact_id])

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting_customer', 'resolved', 'closed', 'expired')",
            name='valid_ticket_status'
        ),
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name='valid_ticket_priority'
        ),
    )


class HandlerChannelLink(Base):
    """
    Generic handler-to-channel linkage.
    Replaces chatbot.whatsapp_instance_id and agent_team.whatsapp_instance_id
    with a flexible, multi-channel approach.
    """
    __tablename__ = "handler_channel_links"

    id = Column(Integer, primary_key=True, index=True)
    handler_type = Column(String(50), nullable=False)  # "chatbot" | "agent_team"
    handler_id = Column(Integer, nullable=False)
    channel = Column(String(50), nullable=False)  # "whatsapp" | "sms" | "email" | "web"
    instance_id = Column(Integer, nullable=True)  # FK to channel-specific instance
    config = Column(JSON, nullable=True)  # channel-specific config
    is_primary = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('handler_type', 'handler_id', 'channel', name='uq_handler_channel'),
        Index('ix_handler_channel_links_lookup', 'handler_type', 'handler_id'),
    )


class ChatbotAgentRouting(Base):
    """
    Agent routing configuration for a chatbot.
    Defines how conversations are routed between bot and human agents.
    """
    __tablename__ = "chatbot_agent_routing"

    id = Column(Integer, primary_key=True, index=True)
    chatbot_id = Column(Integer, ForeignKey("chatbots.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    # Routing mode
    routing_mode = Column(String(30), default="bot_then_human", nullable=False)  # human_only, bot_only, bot_then_human, custom

    # Escalation settings
    fallback_to_human = Column(Boolean, default=True, nullable=False)
    escalation_keywords = Column(ARRAY(String), default=[], nullable=True)  # ["talk to human", "agent please"]
    confidence_threshold = Column(Float, default=0.6, nullable=True)  # Bot confidence threshold
    max_bot_turns = Column(Integer, default=10, nullable=True)  # Max turns before auto-escalate

    # Additional configuration
    routing_metadata = Column(JSON, default={}, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    chatbot = relationship("Chatbot", back_populates="agent_routing")
    agent_configs = relationship("AgentConfig", back_populates="routing", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "routing_mode IN ('human_only', 'bot_only', 'bot_then_human', 'custom')",
            name='valid_routing_mode'
        ),
    )


class AgentConfig(Base):
    """
    Individual agent configuration within a routing setup.
    Used for custom routing to define the order and settings for each agent.
    """
    __tablename__ = "agent_configs"

    id = Column(Integer, primary_key=True, index=True)
    routing_id = Column(Integer, ForeignKey("chatbot_agent_routing.id", ondelete="CASCADE"), nullable=False, index=True)

    # Agent type and reference
    agent_type = Column(String(30), nullable=False)  # llm_chatbot, human_agent
    agent_id = Column(Integer, ForeignKey("chatbots.id", ondelete="SET NULL"), nullable=True, index=True)  # chatbot_id if llm_chatbot
    agent_name = Column(String(200), nullable=True)  # Display name

    # Routing settings
    priority_order = Column(Integer, nullable=False)  # 1, 2, 3...
    max_attempts = Column(Integer, default=3, nullable=False)  # Max attempts before escalating
    is_active = Column(Boolean, default=True, nullable=False)

    # Agent-specific config
    agent_config = Column(JSON, default={}, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    routing = relationship("ChatbotAgentRouting", back_populates="agent_configs")
    chatbot = relationship("Chatbot")

    __table_args__ = (
        CheckConstraint(
            "agent_type IN ('llm_chatbot', 'human_agent')",
            name='valid_agent_type'
        ),
    )


class ChatWidgetConfig(Base):
    """
    Configuration for embeddable chat widget.
    Each chatbot can have one widget configuration.
    """
    __tablename__ = "chat_widget_configs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    chatbot_id = Column(Integer, ForeignKey("chatbots.id", ondelete="SET NULL"), nullable=True, index=True)

    # Handler-agnostic ownership
    handler_type = Column(String(50), nullable=True)  # 'chatbot' | 'agent_team' | None
    handler_id = Column(Integer, nullable=True)

    # Widget identification
    widget_key = Column(String(100), nullable=False, unique=True, index=True)  # Public embed key (wk_xxx)

    # Theme settings
    theme = Column(JSON, default={}, nullable=True)  # {primary_color, secondary_color, position, size, etc.}

    # Welcome and branding
    welcome_message = Column(String(500), nullable=True)
    placeholder_text = Column(String(200), nullable=True)
    bot_name = Column(String(100), nullable=True)
    bot_avatar_url = Column(String(500), nullable=True)

    # Feature toggles
    enable_webchat = Column(Boolean, default=True, nullable=False)
    enable_whatsapp_button = Column(Boolean, default=False, nullable=False)
    whatsapp_number = Column(String(50), nullable=True)
    enable_file_upload = Column(Boolean, default=False, nullable=False)
    enable_feedback = Column(Boolean, default=True, nullable=False)
    enable_sound = Column(Boolean, default=True, nullable=False)
    enable_typing_indicator = Column(Boolean, default=True, nullable=False)

    # Security
    allowed_domains = Column(ARRAY(String), default=[], nullable=True)  # Domain whitelist
    rate_limit_per_minute = Column(Integer, default=20, nullable=False)
    require_email = Column(Boolean, default=False, nullable=False)

    # Advanced settings
    auto_open_delay = Column(Integer, nullable=True)  # Milliseconds before auto-opening
    greeting_delay = Column(Integer, default=1000, nullable=True)  # Delay before showing greeting
    offline_message = Column(String(500), nullable=True)
    custom_css = Column(Text, nullable=True)
    custom_launcher_icon = Column(String(500), nullable=True)

    # Analytics
    track_page_views = Column(Boolean, default=False, nullable=False)

    # Status
    is_active = Column(Boolean, default=True, nullable=False)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="web_widget_configs")
    chatbot = relationship("Chatbot", back_populates="widget_config")


class ConversationFeedback(Base):
    """
    Feedback on chat conversations for training and FAQ extraction.
    """
    __tablename__ = "conversation_feedback"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    message_id = Column(Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True)

    # Rating
    rating = Column(String(20), nullable=True)  # thumbs_up, thumbs_down
    rating_score = Column(Integer, nullable=True)  # 1-5 scale
    feedback_text = Column(Text, nullable=True)  # Optional text feedback

    # Training flags
    is_helpful = Column(Boolean, nullable=True)
    should_be_faq = Column(Boolean, default=False, nullable=False)  # Mark for FAQ extraction

    # FAQ extraction fields
    extracted_question = Column(Text, nullable=True)
    extracted_answer = Column(Text, nullable=True)
    faq_category = Column(String(100), nullable=True)

    # Review status
    reviewed = Column(Boolean, default=False, nullable=False, index=True)
    reviewed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)

    # Submitter info
    submitted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    submitter_identifier = Column(String(255), nullable=True)  # For anonymous submissions

    # Metadata
    feedback_metadata = Column(JSON, default={}, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    session = relationship("ChatSession", back_populates="feedback")
    message = relationship("ChatMessage", back_populates="feedback")
    reviewer = relationship("User", foreign_keys=[reviewed_by])
    submitter = relationship("User", foreign_keys=[submitted_by_user_id])


# ============================================================================
# Event Actions System Models
# ============================================================================

class EventAction(Base):
    """
    Event-driven automation rule that triggers actions based on MessagingEvents.
    Replaces N8N workflows with a native, LangGraph-powered automation engine.
    """
    __tablename__ = "event_actions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # Rule definition
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    trigger_event = Column(String(200), nullable=False, index=True)  # Event name to match
    purpose_key = Column(String(120), nullable=True, index=True)
    attention_policy = Column(JSON, nullable=True)

    # Conditions (JSON array)
    # [{"field": "cart_value", "operator": ">=", "value": 100}]
    conditions = Column(JSON, default=[], nullable=True)

    # Actions (JSON array with order)
    # [{"type": "send_template", "config": {...}, "delay_seconds": 0}]
    actions = Column(JSON, nullable=False)

    # Stop conditions (cancel delayed actions)
    # [{"event": "purchase", "within_seconds": 86400}]
    stop_conditions = Column(JSON, default=[], nullable=True)

    # Control
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    priority = Column(Integer, default=0, nullable=False)  # Higher = runs first
    cooldown_seconds = Column(Integer, default=86400, nullable=False)  # Deduplication
    react_to_delivery = Column(Boolean, default=False, server_default="false", nullable=False)
    # Declared outbound intent for sends produced by this automation. Event
    # Actions are a mechanism, not an intent: a welcome/service notification
    # can be transactional while a nurture action is promotional.
    lane = Column(String(30), nullable=False, server_default="promotional")

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    executions = relationship("EventActionExecution", back_populates="event_action", cascade="all, delete-orphan")
    scheduled_actions = relationship("ScheduledEventAction", back_populates="event_action", cascade="all, delete-orphan")
    system_funnel = relationship(
        "Funnel",
        back_populates="event_action",
        foreign_keys="Funnel.event_action_id",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index('ix_event_actions_proj_trigger', 'project_id', 'trigger_event'),
        CheckConstraint(
            "lane IN ('transactional', 'conversational', 'manual', 'promotional')",
            name="ck_event_actions_lane",
        ),
    )


class ScheduledEventAction(Base):
    """
    Delayed/scheduled action queue for Event Actions.
    Actions can be scheduled to run after a delay, and cancelled if stop conditions are met.
    """
    __tablename__ = "scheduled_event_actions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    event_action_id = Column(Integer, ForeignKey("event_actions.id", ondelete="CASCADE"), nullable=False, index=True)

    # Context
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("messaging_events.id", ondelete="SET NULL"), nullable=True, index=True)

    # Action to execute
    action_index = Column(Integer, nullable=False)  # Index in actions array
    action_config = Column(JSON, nullable=False)  # Snapshot of action config
    variables = Column(JSON, default={}, nullable=True)  # Rendered variables

    # Scheduling
    scheduled_for = Column(DateTime, nullable=False, index=True)
    status = Column(String(20), default="pending", nullable=False, index=True)  # pending, executed, cancelled

    # Execution tracking
    executed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String(200), nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    event_action = relationship("EventAction", back_populates="scheduled_actions")
    user = relationship("MessagingUser")
    event = relationship("MessagingEvent")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'executed', 'cancelled')",
            name='valid_sched_action_status'
        ),
    )


class EventActionExecution(Base):
    """
    Audit log for Event Action executions.
    Tracks which actions ran, their results, and timing.
    """
    __tablename__ = "event_action_executions"

    id = Column(Integer, primary_key=True, index=True)
    event_action_id = Column(Integer, ForeignKey("event_actions.id", ondelete="SET NULL"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("messaging_events.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)

    # Execution details
    actions_executed = Column(JSON, default=[], nullable=True)  # Which actions ran
    status = Column(String(20), default="success", nullable=False, index=True)  # success, partial, failed
    error_message = Column(Text, nullable=True)

    # Timing
    started_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Relationships
    event_action = relationship("EventAction", back_populates="executions")
    event = relationship("MessagingEvent")
    user = relationship("MessagingUser")

    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'partial', 'failed')",
            name='valid_exec_status'
        ),
    )


class EventActionCooldown(Base):
    """
    Tracks cooldown periods to prevent duplicate action executions.
    Used for deduplication based on user + event_action + trigger_event.
    """
    __tablename__ = "event_action_cooldowns"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    event_action_id = Column(Integer, ForeignKey("event_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False, index=True)
    last_triggered_at = Column(DateTime, server_default=func.now(), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)

    # Relationships
    project = relationship("Project")
    event_action = relationship("EventAction")
    user = relationship("MessagingUser")

    __table_args__ = (
        Index('ix_cooldown_unique', 'event_action_id', 'user_id', unique=True),
    )


# ============================================================================
# Project LLM Configuration
# ============================================================================

class ProjectLLMConfig(Base):
    """
    Per-project, per-provider LLM configuration.
    One row per (project, provider). A project can have up to 3 configs:
    one for OpenAI, one for Anthropic, one for Google.
    If a row exists for a provider, the project uses its own key for that provider.
    If no row exists, the system key is used.
    """
    __tablename__ = "project_llm_configs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False)  # openai, anthropic, google
    api_key_encrypted = Column(Text, nullable=False)  # Fernet-encrypted
    preferred_model = Column(String(100), nullable=True)  # e.g. gpt-4o-mini
    temperature = Column(Float, nullable=True)  # 0.0-1.0

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="llm_configs")

    __table_args__ = (
        UniqueConstraint("project_id", "provider", name="uq_project_llm_project_provider"),
    )


# ============================================================================
# Agent Teams & Orchestration Models
# ============================================================================

class AgentTeam(Base):
    """
    Agent Team configuration for multi-agent orchestration.
    Each project can have multiple teams, each with a router and specialist agents.
    """
    __tablename__ = "agent_teams"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # Basic info
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), default="draft", nullable=False, index=True)  # draft, active, paused, archived
    deployment_channels = Column(JSON, default=[], nullable=True)  # ["web", "whatsapp", "sms"]
    initial_message = Column(Text, nullable=True)

    # Integrations
    whatsapp_instance_id = Column(Integer, ForeignKey("whatsapp_instances.id", ondelete="SET NULL"), nullable=True)
    auto_respond_whatsapp = Column(Boolean, default=False, nullable=False)
    is_public = Column(Boolean, default=False, nullable=False)

    # Migration tracking
    migrated_from_chatbot_id = Column(Integer, ForeignKey("chatbots.id", ondelete="SET NULL"), nullable=True, unique=True)

    # Context enrichment
    context_sources = Column(JSON, default=[], nullable=True)  # API connections to call before responding

    # Metadata
    team_metadata = Column(JSON, default={}, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="agent_teams")
    whatsapp_instance = relationship("WhatsAppInstance", foreign_keys=[whatsapp_instance_id])
    migrated_from_chatbot = relationship("Chatbot", foreign_keys=[migrated_from_chatbot_id])
    creator = relationship("User", foreign_keys=[created_by])
    specialists = relationship("SpecialistAgent", back_populates="team", cascade="all, delete-orphan")
    router_config = relationship("RouterConfig", back_populates="team", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name='valid_team_status'
        ),
    )


class SpecialistAgent(Base):
    """
    Specialist agent within an Agent Team.
    Each specialist has its own persona, knowledge base, and configuration.
    """
    __tablename__ = "specialist_agents"

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey("agent_teams.id", ondelete="CASCADE"), nullable=False, index=True)

    # Basic info
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String(10), nullable=True)  # Emoji icon
    is_default = Column(Boolean, default=False, nullable=False)

    # Canvas position
    position_x = Column(Float, default=0.0, nullable=False)
    position_y = Column(Float, default=0.0, nullable=False)

    # Persona configuration
    system_prompt = Column(Text, nullable=True)
    model_provider = Column(String(50), default="openai", nullable=False)
    model_name = Column(String(100), default="gpt-4o-mini", nullable=False)
    temperature = Column(Float, default=0.7)
    max_tokens = Column(Integer, default=2000)
    tone = Column(String(30), default="friendly", nullable=True)  # formal, friendly, direct, empathetic
    language = Column(String(10), nullable=True)  # en, pt, es, etc.

    # Guardrails
    max_turns = Column(Integer, nullable=True)  # Max turns before escalation
    frustration_action = Column(String(20), default="escalate", nullable=True)  # escalate, retry_once, log_only
    blocked_topics = Column(ARRAY(String), default=[], nullable=True)
    operating_hours = Column(JSON, nullable=True)  # {"mon": {"start": "09:00", "end": "18:00"}, ...}
    escalation_config = Column(JSON, nullable=True)  # {"channel": "whatsapp", "target": "..."}

    # Storage paths
    vector_store_path = Column(String(500), nullable=True)
    knowledge_base_path = Column(String(500), nullable=True)

    # Status and ordering
    status = Column(String(20), default="active", nullable=False, index=True)
    agent_order = Column(Integer, default=0, nullable=False)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    team = relationship("AgentTeam", back_populates="specialists")
    knowledge_sources = relationship("SpecialistKnowledgeSource", back_populates="specialist", cascade="all, delete-orphan")
    routing_rules = relationship("RoutingRule", back_populates="specialist", cascade="all, delete-orphan")
    tools = relationship("SpecialistTool", back_populates="specialist", cascade="all, delete-orphan")
    media_assets = relationship("ProjectMediaAsset", back_populates="specialist", cascade="all, delete-orphan")


class SpecialistKnowledgeSource(Base):
    """
    Knowledge source for a specialist agent.
    Supports various source types: PDF, images, URLs, FAQs, text.
    """
    __tablename__ = "specialist_knowledge_sources"

    id = Column(Integer, primary_key=True, index=True)
    specialist_id = Column(Integer, ForeignKey("specialist_agents.id", ondelete="CASCADE"), nullable=False, index=True)

    # Source info
    source_type = Column(String(50), nullable=False, index=True)  # pdf, image, video_url, website_url, faq_url, internal_doc, text
    source_url = Column(String(1000), nullable=True)
    file_path = Column(String(500), nullable=True)
    file_name = Column(String(200), nullable=True)
    file_type = Column(String(50), nullable=True)

    # Content
    content = Column(Text, nullable=True)
    content_hash = Column(String(64), nullable=True, index=True)

    # Processing
    chunk_count = Column(Integer, default=0)
    embedding_model = Column(String(100), nullable=True)
    expose_to_user = Column(Boolean, default=False, nullable=False)  # Show source to end user
    label = Column(String(200), nullable=True)  # Display label
    processed = Column(Boolean, default=False, nullable=False, index=True)
    processing_error = Column(Text, nullable=True)

    # Metadata
    source_metadata = Column(JSON, default={}, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    specialist = relationship("SpecialistAgent", back_populates="knowledge_sources")


class RouterConfig(Base):
    """
    Router configuration for an Agent Team.
    Defines how incoming messages are routed to specialist agents.
    """
    __tablename__ = "router_configs"

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey("agent_teams.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    # Routing configuration
    routing_mode = Column(String(20), default="auto", nullable=False)  # auto, rules, hybrid
    model_provider = Column(String(50), default="openai", nullable=False)
    model_name = Column(String(100), default="gpt-4o-mini", nullable=False)
    default_agent_id = Column(Integer, ForeignKey("specialist_agents.id", ondelete="SET NULL"), nullable=True)

    # Behavior
    allow_mid_convo_switch = Column(Boolean, default=True, nullable=False)
    switch_notification = Column(String(20), default="seamless", nullable=False)  # seamless, notify_user, disabled
    initial_message = Column(Text, nullable=True)

    # Canvas position
    position_x = Column(Float, default=300.0, nullable=False)
    position_y = Column(Float, default=200.0, nullable=False)

    # Metadata
    router_metadata = Column(JSON, default={}, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    team = relationship("AgentTeam", back_populates="router_config")
    default_agent = relationship("SpecialistAgent", foreign_keys=[default_agent_id])
    routing_rules = relationship("RoutingRule", back_populates="router", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "routing_mode IN ('auto', 'rules', 'hybrid')",
            name='valid_routing_mode'
        ),
    )


class RoutingRule(Base):
    """
    Routing rule for directing messages to specific specialists.
    Used in rules and hybrid routing modes.
    """
    __tablename__ = "routing_rules"

    id = Column(Integer, primary_key=True, index=True)
    router_id = Column(Integer, ForeignKey("router_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    specialist_id = Column(Integer, ForeignKey("specialist_agents.id", ondelete="CASCADE"), nullable=False, index=True)

    # Rule definition
    description = Column(Text, nullable=False)  # Natural language routing instruction
    priority = Column(Integer, default=0, nullable=False)
    keyword_hints = Column(ARRAY(String), nullable=True)  # Optional keyword hints for faster matching
    is_active = Column(Boolean, default=True, nullable=False)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    router = relationship("RouterConfig", back_populates="routing_rules")
    specialist = relationship("SpecialistAgent", back_populates="routing_rules")

    __table_args__ = (
        UniqueConstraint('router_id', 'specialist_id', name='uq_routing_rule_router_specialist'),
    )


class TeamSessionEvent(Base):
    """
    Events that occur during an agent team session.
    Tracks agent switches, escalations, timeouts, guardrail triggers, etc.
    """
    __tablename__ = "team_session_events"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)

    # Event details
    event_type = Column(String(50), nullable=False, index=True)  # agent_switch, escalation, timeout, guardrail_triggered, state_change
    event_data = Column(JSON, default={}, nullable=True)

    # Related agent (optional)
    agent_id = Column(Integer, ForeignKey("specialist_agents.id", ondelete="SET NULL"), nullable=True, index=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    # Relationships
    session = relationship("ChatSession")
    agent = relationship("SpecialistAgent")


class SpecialistTool(Base):
    """
    Tool available to a specialist agent.
    Supports webhook calls, MCP server tools, and catalog integrations.
    """
    __tablename__ = "specialist_tools"

    id = Column(Integer, primary_key=True, index=True)
    specialist_id = Column(Integer, ForeignKey("specialist_agents.id", ondelete="CASCADE"), nullable=False, index=True)

    # Tool definition
    tool_type = Column(String(30), nullable=False, index=True)  # webhook, mcp_server, catalog_integration
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    when_to_use = Column(Text, nullable=True)  # Natural language instruction for when the LLM should use this tool

    # Configuration (varies by tool_type)
    # webhook: {method, url, headers, body_template, response_example}
    # mcp_server: {server_url, protocol, enabled_tools: string[]}
    # catalog_integration: {integration_id}
    config = Column(JSON, default={}, nullable=True)

    # Encrypted auth configuration (Fernet)
    auth_config_encrypted = Column(Text, nullable=True)

    # Settings
    is_active = Column(Boolean, default=True, nullable=False)
    timeout_ms = Column(Integer, default=10000, nullable=False)

    # Usage tracking
    last_used_at = Column(DateTime, nullable=True)

    # Metadata
    tool_metadata = Column(JSON, default={}, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    specialist = relationship("SpecialistAgent", back_populates="tools")
    executions = relationship("ToolExecution", back_populates="tool", cascade="all, delete-orphan")


class ToolExecution(Base):
    """
    Log of a tool execution within a chat session.
    """
    __tablename__ = "tool_executions"

    id = Column(Integer, primary_key=True, index=True)
    tool_id = Column(Integer, ForeignKey("specialist_tools.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    message_id = Column(Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True)

    # Execution details
    status = Column(String(20), nullable=False, index=True)  # pending, success, error, timeout
    request_data = Column(JSON, default={}, nullable=True)
    response_data = Column(JSON, default={}, nullable=True)
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    # Relationships
    tool = relationship("SpecialistTool", back_populates="executions")
    session = relationship("ChatSession")
    message = relationship("ChatMessage")


# ============================================================================
# Phase 4 — Playbooks & Analytics
# ============================================================================

class TeamPlaybook(Base):
    """
    Pre-built team templates (SaaS B2B, E-commerce, etc.)
    that users can instantiate as a new AgentTeam.
    """
    __tablename__ = "team_playbooks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    category = Column(String(50), nullable=False, index=True)  # saas, ecommerce, healthcare, education, general
    icon = Column(String(10), nullable=True)
    template_data = Column(JSON, nullable=False, default={})  # Full team config template
    agent_count = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=False, default=0)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class TeamAnalyticsSnapshot(Base):
    """
    Periodic analytics aggregate per agent team.
    """
    __tablename__ = "team_analytics_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey("agent_teams.id", ondelete="CASCADE"), nullable=False, index=True)

    # Period
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)

    # Session metrics
    total_sessions = Column(Integer, nullable=False, default=0)
    resolved_sessions = Column(Integer, nullable=False, default=0)
    escalated_sessions = Column(Integer, nullable=False, default=0)
    avg_turns_to_resolve = Column(Float, nullable=True)

    # Agent distribution
    specialist_distribution = Column(JSON, default={}, nullable=True)  # {agent_id: count}

    # Routing
    routing_accuracy = Column(Float, nullable=True)  # 0.0-1.0

    # Cost
    total_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost = Column(Float, nullable=False, default=0.0)

    # Tools
    tool_call_count = Column(Integer, nullable=False, default=0)
    tool_success_rate = Column(Float, nullable=True)

    # Performance
    avg_response_time_ms = Column(Integer, nullable=True)

    # Metadata
    snapshot_metadata = Column(JSON, default={}, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    team = relationship("AgentTeam")


# ============================================================================
# Project API Connections
# ============================================================================

class ProjectApiConnection(Base):
    """
    External API connection stored at the project level.
    Stores credentials, base URL, and parsed API documentation.
    """
    __tablename__ = "project_api_connections"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # Basic info
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), default="active", nullable=False, index=True)  # active, inactive

    # Connection config
    base_url = Column(String(500), nullable=False)
    auth_type = Column(String(30), default="none", nullable=False)  # api_key_header, bearer_token, basic_auth, custom_header, none
    auth_config_encrypted = Column(Text, nullable=True)  # Fernet-encrypted secret
    auth_header_name = Column(String(100), nullable=True)  # e.g. "X-API-Key"
    auth_header_prefix = Column(String(50), nullable=True)  # e.g. "Bearer"
    default_headers = Column(JSON, default={}, nullable=True)

    # API documentation
    api_documentation = Column(Text, nullable=True)  # Raw markdown
    parsed_spec = Column(JSON, default={}, nullable=True)  # LLM-parsed endpoint suggestions

    # Limits
    timeout_ms = Column(Integer, default=30000, nullable=False)
    rate_limit_rpm = Column(Integer, nullable=True)

    # Metadata
    connection_metadata = Column(JSON, default={}, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="api_connections")
    creator = relationship("User", foreign_keys=[created_by])
    endpoints = relationship("ApiConnectionEndpoint", back_populates="connection", cascade="all, delete-orphan")
    executions = relationship("ApiConnectionExecution", back_populates="connection", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'inactive')",
            name='valid_api_conn_status'
        ),
        CheckConstraint(
            "auth_type IN ('api_key_header', 'bearer_token', 'basic_auth', 'custom_header', 'none')",
            name='valid_api_conn_auth_type'
        ),
    )


class ApiConnectionEndpoint(Base):
    """
    Individual endpoint within an API connection.
    Defines the method, path, parameters, and usage hints.
    """
    __tablename__ = "api_conn_endpoints"

    id = Column(Integer, primary_key=True, index=True)
    connection_id = Column(Integer, ForeignKey("project_api_connections.id", ondelete="CASCADE"), nullable=False, index=True)

    # Endpoint definition
    name = Column(String(200), nullable=False)
    slug = Column(String(100), nullable=False)  # unique per connection
    method = Column(String(10), nullable=False)  # GET, POST, PUT, PATCH, DELETE
    path = Column(String(500), nullable=False)  # relative to base_url

    # Documentation
    description = Column(Text, nullable=True)
    parameters_schema = Column(JSON, default={}, nullable=True)  # JSON Schema for path/query params
    request_body_schema = Column(JSON, default={}, nullable=True)  # JSON Schema for request body
    response_example = Column(JSON, default={}, nullable=True)

    # LLM hints
    when_to_use = Column(Text, nullable=True)  # Natural language for tool selection

    # State
    is_active = Column(Boolean, default=True, nullable=False)
    endpoint_metadata = Column(JSON, default={}, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    connection = relationship("ProjectApiConnection", back_populates="endpoints")

    __table_args__ = (
        UniqueConstraint("connection_id", "slug", name="uq_api_conn_endpoint_slug"),
    )


class ApiConnectionExecution(Base):
    """
    Log of an API connection endpoint call.
    """
    __tablename__ = "api_conn_executions"

    id = Column(Integer, primary_key=True, index=True)
    connection_id = Column(Integer, ForeignKey("project_api_connections.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id = Column(Integer, ForeignKey("api_conn_endpoints.id", ondelete="SET NULL"), nullable=True, index=True)

    # Trigger info
    trigger_source = Column(String(30), nullable=False, index=True)  # event_action, funnel_step, specialist_tool, context_enrichment, manual_test
    trigger_source_id = Column(String(100), nullable=True)  # ID of the source entity
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True, index=True)

    # Execution details
    status = Column(String(20), nullable=False, index=True)  # success, error, timeout
    request_data = Column(JSON, default={}, nullable=True)
    response_data = Column(JSON, default={}, nullable=True)
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    # Relationships
    connection = relationship("ProjectApiConnection", back_populates="executions")
    endpoint = relationship("ApiConnectionEndpoint")
    session = relationship("ChatSession")


# ============================================================================
# Intent Scoring
# ============================================================================

class ScoreDefinition(Base):
    """
    Per-project scoring configuration (intent, friction, churn_risk, custom).
    """
    __tablename__ = "score_definitions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(200), nullable=False)
    slug = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    score_type = Column(String(30), nullable=False)  # intent, friction, churn_risk, custom
    version = Column(Integer, default=1, nullable=False)
    status = Column(String(20), default="draft", nullable=False, index=True)  # draft, active, archived

    # Scoring configuration
    signals = Column(JSON, default=[], nullable=False)  # Array of signal configs
    decay_config = Column(JSON, nullable=True)  # {type: exponential|linear, half_life_days: 7}
    thresholds = Column(JSON, nullable=True)  # {hot: 70, warm: 40, cold: 0}
    normalization_max = Column(Float, default=100, nullable=False)

    # Recalculation settings
    recalc_on_event = Column(Boolean, default=True, nullable=False)
    recalc_interval_minutes = Column(Integer, default=0, nullable=False)
    last_recalc_at = Column(DateTime, nullable=True)

    # Metadata
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="score_definitions")
    creator = relationship("User", foreign_keys=[created_by])
    snapshots = relationship("UserScoreSnapshot", back_populates="score_definition", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_score_def_project_slug"),
        CheckConstraint(
            "score_type IN ('intent', 'friction', 'churn_risk', 'custom')",
            name="valid_score_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="valid_score_def_status",
        ),
    )


class UserFeatureStore(Base):
    """
    Cached event aggregates per user — incremental, never scans event table.
    """
    __tablename__ = "user_feature_store"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    event_name = Column(String(200), nullable=False, index=True)

    count_total = Column(Integer, default=0, nullable=False)
    count_1d = Column(Integer, default=0, nullable=False)
    count_7d = Column(Integer, default=0, nullable=False)
    count_30d = Column(Integer, default=0, nullable=False)
    sum_value = Column(Float, default=0, nullable=False)
    last_value = Column(JSON, nullable=True)

    first_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", "event_name", name="uq_feature_store_user_event"),
        Index("ix_user_feature_store_project_user", "project_id", "user_id"),
    )


class UserScoreSnapshot(Base):
    """
    Cached computed score per user per definition.
    """
    __tablename__ = "user_score_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    score_definition_id = Column(Integer, ForeignKey("score_definitions.id", ondelete="CASCADE"), nullable=False, index=True)

    score = Column(Float, nullable=False)
    tier = Column(String(20), nullable=False, index=True)  # hot, warm, cold
    explanation = Column(JSON, nullable=True)  # [{signal, raw_value, weighted, contribution}]
    previous_score = Column(Float, nullable=True)
    score_delta = Column(Float, nullable=True)

    calculated_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    score_definition = relationship("ScoreDefinition", back_populates="snapshots")

    __table_args__ = (
        UniqueConstraint("user_id", "score_definition_id", name="uq_score_snapshot_user_def"),
        Index("ix_user_score_snapshots_project_user", "project_id", "user_id"),
    )


# ============================================================================
# Policy Layer
# ============================================================================

class ProjectPolicy(Base):
    """
    Per-project automation policies (contact caps, cooldowns, quiet hours).
    """
    __tablename__ = "project_policies"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    contact_caps = Column(JSON, nullable=True)
    channel_cooldowns = Column(JSON, nullable=True)
    quiet_hours = Column(JSON, nullable=True)
    suppression_config = Column(JSON, nullable=True)
    priority_rules = Column(JSON, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="policies")

    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_policy"),
    )


class ContactLedger(Base):
    """
    Per-user contact log for cap enforcement.
    """
    __tablename__ = "contact_ledger"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(30), nullable=False)  # email, whatsapp, sms, push
    source = Column(String(30), nullable=False)  # event_action, funnel, agent_team, template
    source_id = Column(String(100), nullable=True)
    sent_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index("ix_contact_ledger_lookup", "project_id", "user_id", "channel", "sent_at"),
    )


class ProjectPersonalizationConfig(Base):
    """
    Per-project configuration for the visitor data / personalization API.
    Controls which user traits and scores are exposed via the public SDK endpoint.
    """
    __tablename__ = "project_personalization_configs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    enabled = Column(Boolean, default=False, nullable=False)
    exposed_traits = Column(JSON, nullable=True)  # array of field names from user.properties
    expose_scores = Column(Boolean, default=False, nullable=False)
    expose_name = Column(Boolean, default=False, nullable=False)
    expose_email = Column(Boolean, default=False, nullable=False)
    cache_ttl_seconds = Column(Integer, default=300, nullable=False)
    require_analytics_consent = Column(Boolean, default=True, nullable=False)
    auto_track_spa_pages = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="personalization_config")

    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_perso_cfg"),
    )


# ============================================================================
# Segment Rules
# ============================================================================

class SegmentRule(Base):
    """
    A named segment with priority-ordered conditions.
    Contacts are classified into the first matching segment (by priority).
    """
    __tablename__ = "segment_rules"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    priority = Column(Integer, nullable=False, default=0)
    conditions = Column(JSON, nullable=True)  # [{field, operator, value}, ...]
    match_mode = Column(String(10), nullable=False, default="all")  # all (AND), any (OR)
    is_catch_all = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="segment_rules")

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_segment_rule_project_name"),
        Index("ix_segment_rules_project_priority", "project_id", "priority"),
    )


# ============================================================================
# Inbound Message Router
# ============================================================================

class ContactRoutingState(Base):
    """
    Per-contact routing state: which handler currently owns this conversation.
    Priority determines override behaviour (human=100 > funnel_wait=80 > agent_team=50 > chatbot=10 > idle=0).

    Multi-row model:
    - Non-funnel handlers (chatbot, agent_team, human): at most one row per contact+channel
    - Funnel handlers (funnel_wait, funnel_send): one row per enrollment_id
    """
    __tablename__ = "contact_routing_states"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    contact_identifier = Column(String(255), nullable=False)
    channel = Column(String(50), nullable=False)
    handler_type = Column(String(50), nullable=False, default="idle")  # idle, chatbot, agent_team, human, funnel_wait, funnel_send
    handler_id = Column(Integer, nullable=True)
    handler_priority = Column(Integer, nullable=False, default=0)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True)
    enrollment_id = Column(Integer, ForeignKey("funnel_enrollments.id", ondelete="CASCADE"), nullable=True)
    assigned_at = Column(DateTime, server_default=func.now(), nullable=False)
    expires_at = Column(DateTime, nullable=True)
    routing_metadata = Column("metadata", JSON, nullable=True)

    # Relationships
    project = relationship("Project", back_populates="routing_states")
    session = relationship("ChatSession", foreign_keys=[session_id])
    enrollment = relationship("FunnelEnrollment", foreign_keys=[enrollment_id])

    __table_args__ = (
        Index("ix_routing_state_project_handler", "project_id", "handler_type"),
        Index("ix_routing_contact_all", "project_id", "contact_identifier", "channel"),
    )


class InboxAssignmentRule(Base):
    """
    Priority-ordered rules that determine how new inbound messages are routed.
    Evaluated in priority order; first match wins.
    """
    __tablename__ = "inbox_assignment_rules"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    priority = Column(Integer, nullable=False, default=0)
    conditions = Column(JSON, nullable=True)  # [{field, operator, value}, ...]
    match_mode = Column(String(10), nullable=False, default="all")  # all (AND), any (OR)
    destination_type = Column(String(50), nullable=False)  # chatbot, agent_team, human_queue
    destination_id = Column(Integer, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    is_silent = Column(Boolean, nullable=False, default=False)  # Silent rules execute actions without stopping routing
    silent_actions = Column(JSON, nullable=True)  # [{action_type, config}, ...] executed before routing continues

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="inbox_rules")

    __table_args__ = (
        Index("ix_inbox_rules_project_prio", "project_id", "priority"),
    )


class ContactRateWindow(Base):
    """Rolling rate-limit window for outbound messages per contact per channel."""
    __tablename__ = "contact_rate_windows"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    contact_identifier = Column(String(255), nullable=False)
    channel = Column(String(50), nullable=False)
    window_start = Column(DateTime, server_default=func.now(), nullable=False)
    message_count = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index("ix_rate_window_lookup", "project_id", "contact_identifier", "channel", "window_start"),
    )


class WebhookSource(Base):
    """Third-party webhook source configuration per project."""
    __tablename__ = "webhook_sources"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_slug = Column(String(100), nullable=False)
    display_name = Column(String(200), nullable=False)
    source_type = Column(String(20), nullable=False, server_default="custom")  # built_in / custom
    status = Column(String(20), nullable=False, server_default="active")  # active / paused / disabled
    secret_enc = Column(Text, nullable=True)  # Fernet-encrypted signing secret
    transformer_config = Column(JSON, nullable=True)  # JSONPath mapping for custom sources
    rate_limit_per_minute = Column(Integer, nullable=True)  # null = default 100
    last_received_at = Column(DateTime, nullable=True)
    total_received = Column(Integer, nullable=False, server_default="0")
    total_failed = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="webhook_sources")
    ingests = relationship("WebhookIngest", back_populates="source", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("project_id", "source_slug", name="uq_webhook_src_proj_slug"),
    )


class WebhookIngest(Base):
    """Individual webhook delivery record."""
    __tablename__ = "webhook_ingests"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id = Column(Integer, ForeignKey("webhook_sources.id", ondelete="CASCADE"), nullable=False)
    source_slug = Column(String(100), nullable=False)  # denormalized for fast queries
    received_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    headers = Column(JSON, nullable=True)
    raw_payload = Column(JSON, nullable=True)  # JSONB in migration
    signature_valid = Column(Boolean, nullable=True)
    idempotency_key = Column(String(255), nullable=True)
    processing_status = Column(String(20), nullable=False, server_default="queued")  # queued/processing/completed/failed/skipped
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, server_default="0")
    next_retry_at = Column(DateTime, nullable=True)
    resulting_event_id = Column(Integer, ForeignKey("messaging_events.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    source = relationship("WebhookSource", back_populates="ingests")

    __table_args__ = (
        Index("ix_webhook_ingests_dedup", "project_id", "source_slug", "idempotency_key"),
        Index("ix_webhook_ingests_status", "processing_status"),
    )


# ── Send Layer ────────────────────────────────────────────────────────────

class SendLog(Base):
    """Audit trail for every outbound message attempt through the Send Layer."""
    __tablename__ = "send_logs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True)
    channel = Column(String(50), nullable=False)
    recipient = Column(String(255), nullable=False)
    content_type = Column(String(30), nullable=False)
    content_summary = Column(String(500), nullable=True)
    content_payload = Column(JSON, nullable=True)
    template_id = Column(Integer, ForeignKey("messaging_templates.id", ondelete="SET NULL"), nullable=True)
    source_type = Column(String(50), nullable=False)
    source_id = Column(Integer, nullable=True)
    decision_trace = Column(JSON, nullable=True)
    preferred_channel = Column(String(50), nullable=True)
    resolved_channel = Column(String(50), nullable=True)
    fallback_order = Column(JSON, nullable=True)
    fallback_attempt = Column(Integer, default=0, nullable=False)
    status = Column(String(30), default="queued", nullable=False)
    provider_message_id = Column(String(255), nullable=True, index=True)
    provider_response = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    queued_at = Column(DateTime, server_default=func.now(), nullable=False)
    sent_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    render_context = Column(JSON, nullable=True)
    deferred_source_enrollment_id = Column(Integer, ForeignKey("funnel_enrollments.id", ondelete="SET NULL"), nullable=True)
    # Project Import provenance. Imported rows are terminal history and are
    # never eligible for dispatch/selection workers.
    import_id = Column(String(36), ForeignKey("project_imports.id", ondelete="SET NULL"), nullable=True, index=True)
    external_message_id = Column(String(255), nullable=True, index=True)
    is_historical = Column(Boolean, nullable=False, server_default=text("false"))

    # Email open tracking
    tracking_token = Column(String(36), nullable=True, unique=True, index=True)
    opened_at = Column(DateTime, nullable=True)
    open_count = Column(Integer, default=0, nullable=False)

    # Email click tracking
    click_count = Column(Integer, default=0, nullable=False, server_default=text("0"))
    first_click_at = Column(DateTime, nullable=True)

    # Inbound reply attribution (stamped by InboundRouter within the
    # MES attribution window; strongest engagement signal on chat channels)
    replied_at = Column(DateTime, nullable=True)
    reply_count = Column(Integer, default=0, nullable=False, server_default=text("0"))

    # Instance tracking (for per-instance health aggregation)
    instance_id = Column(Integer, nullable=True, index=True)

    # Resolved sending domain (from the send's from_email) — the pace/reputation
    # key. Lets DeliveryFeedback bounces map back to a domain via send_log_id.
    sending_domain = Column(String(255), nullable=True, index=True)

    # Priority for dispatch ordering (0=normal, 10=high, 20=urgent)
    priority = Column(Integer, default=0, nullable=False, server_default=text("0"))

    # Retry tracking
    retry_of_id = Column(Integer, ForeignKey("send_logs.id", ondelete="SET NULL"), nullable=True, index=True)

    # Intent classification (Phase 1 — shadow dual-write; not yet authoritative).
    # intent_class mirrors the outbound Lane (transactional/conversational/
    # manual/promotional); intent_tier is the declared importance ordinal.
    intent_tier = Column(Integer, nullable=True)
    intent_class = Column(String(30), nullable=True)
    # Authored arbitration identity. Legacy/null rows fall back to their lane
    # scope; new funnel/EventAction/campaign rows copy the owning policy.
    attention_scope = Column(String(120), nullable=True, index=True)
    purpose_key = Column(String(120), nullable=True, index=True)
    # Immutable-at-creation policy evidence used by future planning and the
    # final attention gate.  The owning definition may be edited later; a
    # pending intent must remain explainable under the policy that created it.
    attention_policy_snapshot = Column(JSON, nullable=True)
    planning_status = Column(String(30), nullable=True, index=True)
    planning_evaluated_at = Column(DateTime, nullable=True)
    planning_horizon_end = Column(DateTime, nullable=True)
    planning_snapshot = Column(JSON, nullable=True)

    # Supersession (Phase 1). A pending deferred/delayed row flips to status
    # 'superseded' when its owning episode dies for a relevance reason.
    superseded_at = Column(DateTime, nullable=True)
    superseded_reason = Column(String(255), nullable=True)

    # Authored slot identity (Phase 3/4) — links a send back to its authored
    # object so the bandit can attribute outcomes to a stable arm.
    slot_id = Column(String(36), nullable=True, index=True)

    # Relationships
    project = relationship("Project", back_populates="send_logs")
    delivery_events = relationship("DeliveryStatusEvent", back_populates="send_log", cascade="all, delete-orphan")
    clicks = relationship("SendLogClick", back_populates="send_log", cascade="all, delete-orphan")
    effectiveness_score = relationship("MessageEffectivenessScore", back_populates="send_log", uselist=False, cascade="all, delete-orphan")
    retry_of = relationship("SendLog", remote_side="SendLog.id", foreign_keys=[retry_of_id])

    __table_args__ = (
        Index("ix_send_logs_proj_status", "project_id", "status"),
        Index("ix_send_logs_proj_user", "project_id", "user_id"),
        Index("ix_send_logs_source", "source_type", "source_id"),
        Index("ix_send_logs_deferred_prio", "status", priority.desc(), "scheduled_at", postgresql_where=text("status IN ('deferred', 'delayed')")),
    )


class ProjectSendConfig(Base):
    """Per-project send pipeline configuration (quiet hours, fallback, rate limits)."""
    __tablename__ = "project_send_configs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    default_strategy = Column(String(30), default="try_fallback", nullable=False)
    default_fallback_order = Column(JSON, default=["whatsapp", "email", "sms"], nullable=False)
    rate_limit_messages = Column(Integer, default=10, nullable=False)
    rate_limit_window_minutes = Column(Integer, default=5, nullable=False)
    quiet_hours_enabled = Column(Boolean, default=False, nullable=False)
    quiet_hours_start = Column(String(5), nullable=True)
    quiet_hours_end = Column(String(5), nullable=True)
    quiet_hours_timezone = Column(String(50), default="UTC", nullable=False)
    quiet_hours_action = Column(String(20), default="delay", nullable=False)
    use_send_layer = Column(Boolean, default=True, nullable=False, server_default=text("true"))
    soft_bounce_threshold = Column(Integer, default=3, nullable=False, server_default=text("3"))
    soft_bounce_window_days = Column(Integer, default=7, nullable=False, server_default=text("7"))
    # Per-project regex marking synthetic/placeholder recipient addresses that
    # must never be sent to (e.g. Tabloide's guest_<uuid>@guest.tabloide.pro).
    # NULL = the tenant has no placeholder convention → nothing is blocked as a
    # placeholder. Kept per-project so the send layer stays tenant-agnostic.
    placeholder_email_pattern = Column(String(500), nullable=True)
    # Project-level outbound pace fallback (between per-domain policy and global
    # default). NULL = no project override; the pace governor falls back to the
    # global env defaults. See SendingDomain and SendPaceService.
    max_per_minute = Column(Integer, nullable=True)
    max_per_day = Column(Integer, nullable=True)
    # Best-time-to-send windows: {"_default": {enabled, windows: [{days?, start, end}]},
    # "<channel>": {...}}. Project tier of the inheritance chain
    # (contact+channel > contact > project channel > project default).
    send_windows = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="send_config")

    __table_args__ = (
        UniqueConstraint("project_id", name="uq_send_cfg_project"),
    )


class SendingDomain(Base):
    """First-class sending domain — the reputation unit receivers (Gmail etc.)
    actually judge. May be dedicated to one project or shared across a whole
    workspace/tenant. Carries the pace policy, warm-up ramp, and live
    reputation status consulted by SendPaceService. Auto-created on first sight
    of an unknown sending domain so new tenants need zero setup.
    """
    __tablename__ = "sending_domains"

    id = Column(Integer, primary_key=True, index=True)
    domain = Column(String(255), nullable=False, unique=True, index=True)
    workspace_id = Column(Integer, nullable=True, index=True)
    # NULL project_id = shared across the workspace/tenant, not tied to one project.
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)

    # Rate policy — NULL means "fall back" (project config → global default).
    max_per_minute = Column(Integer, nullable=True)
    max_per_day = Column(Integer, nullable=True)
    # Fraction of the normal per-minute rate used while status='throttled'.
    throttle_factor = Column(Float, default=0.3, nullable=False, server_default=text("0.3"))

    # Warm-up ramp. warmup_enabled=False → skip the ramping daily cap entirely
    # (domain is already warm); per-minute pacing + reputation guards still apply.
    warmup_enabled = Column(Boolean, default=True, nullable=False, server_default=text("true"))
    warmup_started_at = Column(Date, nullable=True)
    # JSON list, index = day-since-start, value = max sends that day.
    warmup_schedule = Column(JSON, nullable=True)

    # Reputation, refreshed by sending_reputation_worker.
    status = Column(String(20), default="active", nullable=False, server_default="active")  # active|throttled|paused
    bounce_rate_24h = Column(Float, nullable=True)
    complaint_rate_24h = Column(Float, nullable=True)
    reputation_updated_at = Column(DateTime, nullable=True)
    auto_throttle_bounce_pct = Column(Float, default=2.0, nullable=False, server_default=text("2.0"))
    auto_pause_bounce_pct = Column(Float, default=5.0, nullable=False, server_default=text("5.0"))
    auto_pause_complaint_pct = Column(Float, default=0.3, nullable=False, server_default=text("0.3"))

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class SendingRateState(Base):
    """Leaky-bucket cursor + rolling daily counter for one sending domain.
    Mutated only under pg_advisory_xact_lock(hashtext('sendpace:'||scope_key))
    so the check-and-advance is atomic across all API workers + the leader.
    """
    __tablename__ = "sending_rate_state"

    id = Column(Integer, primary_key=True, index=True)
    scope_key = Column(String(255), nullable=False, unique=True, index=True)  # the sending domain
    # Earliest instant the next send may go out (leaky-bucket cursor).
    next_slot_at = Column(DateTime, nullable=True)
    # Daily cap counter (rolls when `day` changes).
    day = Column(Date, nullable=True)
    day_count = Column(Integer, default=0, nullable=False, server_default=text("0"))
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ProjectChannelConfig(Base):
    """Per-project channel enable/disable toggle with optional config."""
    __tablename__ = "project_channel_configs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(50), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    config = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "channel", name="uq_proj_ch_cfg_proj_channel"),
        Index("ix_proj_ch_cfg_project_id", "project_id"),
    )

    # Relationships
    project = relationship("Project", back_populates="channel_configs")


class ChannelCapability(Base):
    """Static capability profile for each supported channel."""
    __tablename__ = "channel_capabilities"

    id = Column(Integer, primary_key=True, index=True)
    channel = Column(String(50), nullable=False, unique=True)
    display_name = Column(String(100), nullable=False)
    max_text_length = Column(Integer, nullable=True)
    supports_media = Column(Boolean, default=False, nullable=False)
    supported_media_types = Column(JSON, nullable=True)
    max_media_size_mb = Column(Integer, nullable=True)
    supports_buttons = Column(Boolean, default=False, nullable=False)
    max_buttons = Column(Integer, nullable=True)
    supports_templates = Column(Boolean, default=False, nullable=False)
    supports_rich_text = Column(Boolean, default=False, nullable=False)
    supports_reactions = Column(Boolean, default=False, nullable=False)
    has_session_window = Column(Boolean, default=False, nullable=False)
    session_window_hours = Column(Integer, nullable=True)
    requires_opt_in = Column(Boolean, default=False, nullable=False)
    supports_read_receipts = Column(Boolean, default=False, nullable=False)
    supported_statuses = Column(JSONB, nullable=True)
    icon_hint = Column(String(50), nullable=True)
    is_inbound_capable = Column(Boolean, default=False, nullable=False)
    inbound_requires_setup = Column(Boolean, default=False, nullable=False)
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("channel", name="uq_chan_cap_channel"),
    )


class DeliveryStatusEvent(Base):
    """Individual status event for a send_log entry (queued → sent → delivered → read)."""
    __tablename__ = "delivery_status_events"

    id = Column(Integer, primary_key=True, index=True)
    send_log_id = Column(Integer, ForeignKey("send_logs.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(30), nullable=False)
    provider_status = Column(String(100), nullable=True)
    provider_timestamp = Column(DateTime, nullable=True)
    error_code = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)
    raw_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    send_log = relationship("SendLog", back_populates="delivery_events")


class SendLogClick(Base):
    """Per-link click tracking for email messages."""
    __tablename__ = "send_log_clicks"

    id = Column(Integer, primary_key=True, index=True)
    send_log_id = Column(Integer, ForeignKey("send_logs.id", ondelete="CASCADE"), nullable=False)
    link_index = Column(Integer, nullable=False)
    original_url = Column(Text, nullable=False)
    click_count = Column(Integer, default=0, nullable=False)
    first_click_at = Column(DateTime, nullable=True)
    last_click_at = Column(DateTime, nullable=True)
    user_agent = Column(String(500), nullable=True)

    send_log = relationship("SendLog", back_populates="clicks")

    __table_args__ = (
        UniqueConstraint("send_log_id", "link_index", name="uq_slc_log_link"),
    )


class DeliveryFeedback(Base):
    """Channel-agnostic record of bounces, complaints, unreachable contacts."""
    __tablename__ = "delivery_feedback"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True)
    channel = Column(String(50), nullable=False)
    recipient = Column(String(255), nullable=False)
    feedback_type = Column(String(30), nullable=False)  # hard_bounce, soft_bounce, complaint, unreachable, blocked, expired
    reason = Column(String(500), nullable=True)
    provider = Column(String(50), nullable=True)  # ses, mailgun, sendgrid, evolution, meta, twilio
    provider_code = Column(String(50), nullable=True)
    provider_detail = Column(Text, nullable=True)
    raw_payload = Column(JSONB, nullable=True)
    send_log_id = Column(Integer, ForeignKey("send_logs.id", ondelete="SET NULL"), nullable=True, index=True)
    action_taken = Column(String(50), nullable=True)  # opted_out, flagged, ignored, retry_scheduled
    # Sending domain the bounce/complaint is attributed to (for reputation).
    # Stamped at record time from the related send (send_log_id or recent send
    # to this recipient), since send_log_id is often absent on provider webhooks.
    sending_domain = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_dlvry_fdbk_proj_ch_rcpt", "project_id", "channel", "recipient"),
        Index("ix_dlvry_fdbk_proj_user", "project_id", "user_id"),
        Index("ix_dlvry_fdbk_domain", "sending_domain", "feedback_type", "created_at"),
    )


class MessageEffectivenessScore(Base):
    """Per-message effectiveness score (reach + re-engagement)."""
    __tablename__ = "message_effectiveness_scores"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    send_log_id = Column(Integer, ForeignKey("send_logs.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True)
    channel = Column(String(50), nullable=False)
    template_id = Column(Integer, ForeignKey("messaging_templates.id", ondelete="SET NULL"), nullable=True)
    source_type = Column(String(50), nullable=False)
    source_id = Column(Integer, nullable=True)
    funnel_id = Column(Integer, ForeignKey("funnels.id", ondelete="SET NULL"), nullable=True)
    funnel_step_id = Column(Integer, ForeignKey("funnel_steps.id", ondelete="SET NULL"), nullable=True)

    # Reach score — confidence the message was seen
    reach_score = Column(Float, nullable=False, server_default=text("0"))
    reach_signals = Column(JSONB, nullable=True)

    # Re-engagement score — did message bring user back
    reengagement_score = Column(Float, nullable=False, server_default=text("0"))
    reengagement_signals = Column(JSONB, nullable=True)

    # Combined
    combined_score = Column(Float, nullable=False, server_default=text("0"))
    score_grade = Column(String(20), nullable=False, server_default="pending")  # high/medium/low/pending/failed

    # Computation metadata
    algorithm_version = Column(Integer, nullable=False, server_default=text("1"))
    attribution_window_h = Column(Integer, nullable=False, server_default=text("24"))
    computed_at = Column(DateTime, nullable=True)
    stale = Column(Boolean, nullable=False, server_default=text("true"))
    # settled = attribution window closed (or terminal failure); score is final
    settled = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    send_log = relationship("SendLog", back_populates="effectiveness_score")
    project = relationship("Project", back_populates="mes_scores")

    __table_args__ = (
        UniqueConstraint("send_log_id", name="uq_mes_send_log"),
        Index("ix_mes_proj_tpl", "project_id", "template_id"),
        Index("ix_mes_proj_channel", "project_id", "channel"),
        Index("ix_mes_proj_source", "project_id", "source_type"),
        Index("ix_mes_stale", "stale", "computed_at"),
        Index("ix_mes_proj_fnl_step", "project_id", "funnel_step_id"),
        Index("ix_mes_proj_grade", "project_id", "score_grade"),
        Index("ix_mes_proj_user", "project_id", "user_id"),
        Index("ix_mes_unsettled", "project_id", "settled", postgresql_where=text("settled = false")),
    )


class MESConfig(Base):
    """Per-project MES configuration."""
    __tablename__ = "mes_config"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    enabled = Column(Boolean, nullable=False, server_default=text("true"))
    attribution_window_hours = Column(Integer, nullable=False, server_default=text("24"))
    pre_session_window_min = Column(Integer, nullable=False, server_default=text("15"))
    reach_weight = Column(Float, nullable=False, server_default=text("0.5"))
    reengagement_weight = Column(Float, nullable=False, server_default=text("0.5"))
    grade_thresholds = Column(JSONB, nullable=True)
    reengagement_event_weights = Column(JSONB, nullable=True)
    exclude_event_patterns = Column(JSONB, nullable=True)
    speed_decay_rate = Column(Float, nullable=False, server_default=text("0.08"))
    event_decay_rate = Column(Float, nullable=False, server_default=text("0.1"))
    # Channels with outbound link rewriting for click tracking.
    # Email is always tracked; add "whatsapp"/"sms" to opt in (null = email only).
    link_tracking_channels = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="mes_config")

    __table_args__ = (
        UniqueConstraint("project_id", name="uq_mes_config_project"),
    )


class WorkerState(Base):
    """Persistent key-value state for background workers (watermarks, cursors)."""
    __tablename__ = "worker_state"

    key = Column(String(100), primary_key=True)
    value = Column(JSONB, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ProjectMember(Base):
    """Per-project user membership with role-based access."""
    __tablename__ = "project_members"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(30), nullable=False, default="viewer")
    is_active = Column(Boolean, default=True, nullable=False)
    invited_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_proj_member_proj_user"),
        Index("ix_proj_mbr_proj_role", "project_id", "role"),
        Index("ix_proj_mbr_user_active", "user_id", "is_active"),
    )

    # Relationships
    project = relationship("Project", back_populates="members")
    user = relationship("User", foreign_keys=[user_id], back_populates="project_memberships")
    invited_by = relationship("User", foreign_keys=[invited_by_id])


class PushSubscription(Base):
    """Web Push subscription for a user."""
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    endpoint = Column(Text, unique=True, nullable=False)
    p256dh_key = Column(Text, nullable=False)
    auth_key = Column(Text, nullable=False)
    device_label = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_push_sub_user_active", "user_id", "is_active"),
    )

    # Relationships
    user = relationship("User", back_populates="push_subscriptions")


class RefreshToken(Base):
    """Server-side refresh token for JWT token rotation."""
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)  # SHA-256 hex digest
    expires_at = Column(DateTime, nullable=False)
    is_revoked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    device_info = Column(String(200), nullable=True)

    __table_args__ = (
        Index("ix_refresh_tkn_hash", "token_hash"),
        Index("ix_refresh_tkn_user", "user_id"),
    )

    user = relationship("User", back_populates="refresh_tokens")


# ============================================================================
# Knowledge Asset Library
# ============================================================================

class KnowledgeAsset(Base):
    """A knowledge or media asset stored at the project level."""
    __tablename__ = "knowledge_assets"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    asset_type = Column(String(20), nullable=False)  # document/url/faq/snippet/text/image/video/audio
    name = Column(String(300), nullable=False)
    description = Column(Text, nullable=True)
    tags = Column(ARRAY(String), default=[], nullable=False)
    language = Column(String(10), nullable=True)
    source_data = Column(JSONB, nullable=False, default={})

    # Processing
    processing_status = Column(String(20), default="pending", nullable=False)  # pending/processing/ready/failed/skipped
    processing_error = Column(Text, nullable=True)
    chunk_count = Column(Integer, default=0, nullable=False)
    embedding_model = Column(String(100), nullable=True)
    last_processed_at = Column(DateTime, nullable=True)
    extracted_text = Column(Text, nullable=True)
    content_hash = Column(String(64), nullable=True)

    # Versioning
    version = Column(Integer, default=1, nullable=False)
    previous_version_id = Column(Integer, ForeignKey("knowledge_assets.id", ondelete="SET NULL"), nullable=True)

    status = Column(String(20), default="active", nullable=False)  # active/archived/draft
    created_by = Column(String(200), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Usage mode: rag (embed only), direct (share as-is), both
    usage_mode = Column(String(10), nullable=False, server_default='rag')
    slug = Column(String(100), nullable=True)

    # Storage
    storage_key = Column(String(500), nullable=True)
    file_size = Column(BigInteger, nullable=True)

    # Relationships
    project = relationship("Project", back_populates="knowledge_assets")
    previous_version = relationship("KnowledgeAsset", remote_side=[id], foreign_keys=[previous_version_id])
    collections = relationship("CollectionAsset", back_populates="asset", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_ka_proj_status", "project_id", "status"),
        Index("ix_ka_proj_type", "project_id", "asset_type"),
        UniqueConstraint('project_id', 'slug', name='uq_ka_project_slug'),
    )


class KnowledgeCollection(Base):
    """A named grouping of knowledge assets within a project."""
    __tablename__ = "knowledge_collections"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    visibility = Column(String(20), default="selective", nullable=False)  # all/selective

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="knowledge_collections")
    assets = relationship("CollectionAsset", back_populates="collection", cascade="all, delete-orphan")
    bindings = relationship("ConsumerKnowledgeBinding", back_populates="collection", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_kc_project_name"),
    )


class CollectionAsset(Base):
    """M2M join between collections and assets."""
    __tablename__ = "collection_assets"

    id = Column(Integer, primary_key=True, index=True)
    collection_id = Column(Integer, ForeignKey("knowledge_collections.id", ondelete="CASCADE"), nullable=False, index=True)
    asset_id = Column(Integer, ForeignKey("knowledge_assets.id", ondelete="CASCADE"), nullable=False, index=True)
    added_at = Column(DateTime, server_default=func.now(), nullable=False)
    added_by = Column(String(200), nullable=True)

    # Relationships
    collection = relationship("KnowledgeCollection", back_populates="assets")
    asset = relationship("KnowledgeAsset", back_populates="collections")

    __table_args__ = (
        UniqueConstraint("collection_id", "asset_id", name="uq_ca_coll_asset"),
    )


class ConsumerKnowledgeBinding(Base):
    """Links a consumer (chatbot, agent_team, specialist, inbox) to a knowledge collection."""
    __tablename__ = "consumer_knowledge_bindings"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    consumer_type = Column(String(30), nullable=False)  # chatbot/agent_team/specialist/inbox
    consumer_id = Column(Integer, nullable=False)
    collection_id = Column(Integer, ForeignKey("knowledge_collections.id", ondelete="CASCADE"), nullable=False)
    permission = Column(String(20), default="rag", nullable=False)  # rag/suggest/browse

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    collection = relationship("KnowledgeCollection", back_populates="bindings")

    __table_args__ = (
        UniqueConstraint("consumer_type", "consumer_id", "collection_id", name="uq_ckb_consumer_coll"),
        Index("ix_ckb_consumer", "consumer_type", "consumer_id"),
    )



class NoCodeMapping(Base):
    """No-code event mapping created via Chrome Extension."""
    __tablename__ = "nocode_mappings"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    mapping_uid = Column(String(36), unique=True, nullable=False)
    event_name = Column(String(200), nullable=False)
    trigger = Column(String(30), nullable=False)  # click, submit, change, focus, visibility
    vef = Column(JSONB, nullable=False)  # VEF descriptor with layers
    page_pattern = Column(String(500), nullable=True)
    page_scope_type = Column(String(20), default="glob", nullable=False)  # exact, starts_with, contains, glob
    viewport_scope = Column(String(20), default="all", nullable=False)  # all, mobile, tablet, desktop
    properties = Column(JSONB, nullable=True)  # Property extraction definitions
    status = Column(String(20), default="draft", nullable=False)  # draft, published, archived
    version = Column(Integer, default=1, nullable=False)
    display_name = Column(String(200), nullable=True)
    consent_required = Column(Boolean, default=False, nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="nocode_mappings")
    created_by = relationship("User", foreign_keys=[created_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])

    __table_args__ = (
        Index("ix_ncm_project_status", "project_id", "status"),
        Index("ix_ncm_project_event", "project_id", "event_name"),
    )


class NoCodeConfigSnapshot(Base):
    """Immutable published snapshot of no-code mappings config."""
    __tablename__ = "nocode_config_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    mappings_snapshot = Column(JSONB, nullable=False)
    checksum = Column(String(64), nullable=False)  # SHA-256
    mapping_count = Column(Integer, nullable=True)
    published_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="nocode_config_snapshots")
    published_by = relationship("User", foreign_keys=[published_by_id])

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_ncs_project_version"),
    )


class NoCodeDebugEvent(Base):
    """Debug events from Chrome Extension debug mode."""
    __tablename__ = "nocode_debug_events"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    session_id = Column(String(64), nullable=False, index=True)
    mapping_uid = Column(String(36), nullable=True)
    event_name = Column(String(200), nullable=True)
    payload = Column(JSONB, nullable=True)
    vef_resolution = Column(JSONB, nullable=True)  # Which layer matched, time, confidence
    status = Column(String(20), default="received", nullable=False)  # received, matched, unresolved, failed
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)


class ExtensionAuditLog(Base):
    """Append-only audit log for Chrome Extension actions."""
    __tablename__ = "extension_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(50), nullable=False)  # extension.auth, mappings.published, etc.
    target_type = Column(String(50), nullable=True)  # mapping, config_snapshot, token
    target_id = Column(String(50), nullable=True)
    details = Column(JSONB, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_eal_project_created", "project_id", "created_at"),
    )


class McpConnectorInstallation(Base):
    """Linked MCP connector metadata; OAuth tokens are never stored here."""
    __tablename__ = "mcp_connector_installations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    connector_type = Column(String(20), nullable=False, server_default="product")  # product, admin
    client_name = Column(String(100), nullable=True)
    scopes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    allowed_origins = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status = Column(String(20), nullable=False, server_default="active")
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    project = relationship("Project")

    __table_args__ = (
        CheckConstraint("connector_type IN ('product', 'admin')", name="ck_mcp_conn_type"),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_mcp_conn_status"),
        Index("ix_mcp_conn_user_type", "user_id", "connector_type"),
    )


class McpToolAuditLog(Base):
    """Append-only audit trail for MCP tool calls."""
    __tablename__ = "mcp_tool_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    connector_installation_id = Column(Integer, ForeignKey("mcp_connector_installations.id", ondelete="SET NULL"), nullable=True)
    server_type = Column(String(20), nullable=False)
    tool_name = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False)
    input_summary = Column(JSONB, nullable=True)
    output_summary = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    correlation_id = Column(String(64), nullable=False, index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(255), nullable=True)
    origin = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("Project")
    user = relationship("User")
    connector_installation = relationship("McpConnectorInstallation")

    __table_args__ = (
        CheckConstraint("server_type IN ('product', 'admin')", name="ck_mcp_audit_server_type"),
        CheckConstraint("status IN ('success', 'error', 'denied', 'pending_confirmation')", name="ck_mcp_audit_status"),
        Index("ix_mcp_audit_proj_created", "project_id", "created_at"),
        Index("ix_mcp_audit_tool_created", "tool_name", "created_at"),
    )


class McpPendingAction(Base):
    """Short-lived confirmation gate for risky MCP actions."""
    __tablename__ = "mcp_pending_actions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    server_type = Column(String(20), nullable=False)
    tool_name = Column(String(100), nullable=False)
    action_payload = Column(JSONB, nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    status = Column(String(20), nullable=False, server_default="pending")
    expires_at = Column(DateTime, nullable=False)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("Project")
    user = relationship("User")

    __table_args__ = (
        CheckConstraint("server_type IN ('product', 'admin')", name="ck_mcp_pending_server_type"),
        CheckConstraint("status IN ('pending', 'confirmed', 'expired', 'cancelled')", name="ck_mcp_pending_status"),
        Index("ix_mcp_pending_user_tool", "user_id", "tool_name", "status"),
    )


class ProjectVariable(Base):
    """Project-level template variables (text or URL with params)."""
    __tablename__ = "project_variables"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(100), nullable=False)
    var_type = Column(String(10), nullable=False, server_default="text")
    value = Column(Text, nullable=True)
    url_params = Column(JSONB, nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project", back_populates="variables")

    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_proj_var_key"),
        CheckConstraint("var_type IN ('text', 'url')", name="ck_proj_var_type"),
    )


class ExtensionToken(Base):
    """Scoped authentication tokens for Chrome Extension."""
    __tablename__ = "extension_tokens"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(255), nullable=False, index=True)  # SHA-256 of raw token
    token_prefix = Column(String(20), nullable=False)  # For display: ext_abc1...
    role = Column(String(20), default="editor", nullable=False)  # editor or publisher
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    revoked_at = Column(DateTime, nullable=True)

    # Relationships
    project = relationship("Project")
    user = relationship("User")


class SandboxSession(Base):
    """Sandbox testing session for funnel dry-run."""
    __tablename__ = "sandbox_sessions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    funnel_id = Column(Integer, ForeignKey("funnels.id", ondelete="CASCADE"), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    mode = Column(String(20), server_default="log_only", nullable=False)  # log_only, preview
    preview_channel = Column(String(30), nullable=True)
    preview_destination = Column(String(255), nullable=True)
    preview_instance_id = Column(Integer, nullable=True)
    status = Column(String(20), server_default="active", nullable=False)  # active, completed, reset
    enrollment_id = Column(Integer, ForeignKey("funnel_enrollments.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")
    funnel = relationship("Funnel")
    contact = relationship("MessagingUser")
    creator = relationship("User")
    enrollment = relationship("FunnelEnrollment")
    action_logs = relationship("SandboxActionLog", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_sandbox_sessions_funnel_contact', 'funnel_id', 'contact_id', 'status'),
        CheckConstraint("mode IN ('log_only', 'preview')", name="ck_sandbox_session_mode"),
        CheckConstraint("status IN ('active', 'completed', 'reset')", name="ck_sandbox_session_status"),
    )


class SandboxActionLog(Base):
    """Log of intercepted actions during sandbox funnel testing."""
    __tablename__ = "sandbox_action_logs"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sandbox_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    enrollment_id = Column(Integer, nullable=True)
    step_id = Column(Integer, nullable=True)
    action_type = Column(String(50), nullable=False)
    action_config = Column(JSON, nullable=True)
    intercepted_mode = Column(String(30), nullable=False)  # logged, preview_sent, preview_failed
    preview_result = Column(JSON, nullable=True)
    resolved_variables = Column(JSON, nullable=True)
    suppression_check = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    session = relationship("SandboxSession", back_populates="action_logs")


# ============================================================================
# System LLM Providers & Usage Tracking
# ============================================================================

class SystemLLMProvider(Base):
    """
    Platform-level system API keys for LLM providers.
    Projects without their own keys use these system credits.
    Configurable via env vars (bootstrap) and DB (runtime hot-swap).
    """
    __tablename__ = "system_llm_providers"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(50), nullable=False, unique=True)  # openai, anthropic, google
    api_key_encrypted = Column(Text, nullable=False)  # Fernet-encrypted
    is_active = Column(Boolean, server_default=text("true"), nullable=False)
    rate_limit_rpm = Column(Integer, nullable=True)  # requests/min cap
    rate_limit_tpm = Column(Integer, nullable=True)  # tokens/min cap
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class LLMUsageRecord(Base):
    """
    Per-call LLM usage tracking for billing and limits.
    One row per LLM API call with token counts and cost estimate.
    """
    __tablename__ = "llm_usage_records"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(50), nullable=False)  # openai, anthropic, google
    model = Column(String(100), nullable=False)  # gpt-4o-mini, claude-sonnet-4-5, etc.
    purpose = Column(String(50), nullable=False)  # chat, routing, guardrails, embeddings, milestone, media, spec_parse
    key_source = Column(String(20), nullable=False)  # 'project' or 'system'
    input_tokens = Column(Integer, server_default=text("0"), nullable=False)
    output_tokens = Column(Integer, server_default=text("0"), nullable=False)
    total_tokens = Column(Integer, server_default=text("0"), nullable=False)
    cost_estimate = Column(Float, server_default=text("0.0"), nullable=False)  # USD
    call_count = Column(Integer, server_default=text("1"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    project = relationship("Project")

    __table_args__ = (
        Index('ix_llm_usage_proj_date', 'project_id', 'created_at'),
        Index('ix_llm_usage_proj_provider', 'project_id', 'provider'),
    )


class ContactPosition(Base):
    """Phase 3 base machine — the unified (Type, Stage, Age) position of a
    contact. Computed in SHADOW (set-based sweep, derived from segment/
    lifecycle); not yet authoritative for behavior. One row per contact."""
    __tablename__ = "contact_positions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    lifecycle_model_id = Column(Integer, ForeignKey("lifecycle_models.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(255), nullable=False, server_default="default")
    stage = Column(String(50), nullable=True)
    age_bucket = Column(String(30), nullable=True)
    position_entered_at = Column(DateTime, nullable=True)  # Type-entry, NOT first-touch
    type_entered_at = Column(DateTime, nullable=True)
    stage_entered_at = Column(DateTime, nullable=True)
    computed_at = Column(DateTime, server_default=text("now()"), nullable=False)
    provenance = Column(JSONB, nullable=True)
    explanation = Column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", "lifecycle_model_id", name="uq_contact_position_model_user"),
        Index("ix_contact_pos_proj_type", "project_id", "lifecycle_model_id", "type", "stage", "age_bucket"),
    )


class ContactPositionTransition(Base):
    """Append-only history of contact position changes (type/stage), so a
    contact's position is queryable over time (Phase 3 exit criterion)."""
    __tablename__ = "contact_position_transitions"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)
    lifecycle_model_id = Column(Integer, ForeignKey("lifecycle_models.id", ondelete="CASCADE"), nullable=False)
    from_type = Column(String(255), nullable=True)
    to_type = Column(String(255), nullable=True)
    from_stage = Column(String(50), nullable=True)
    to_stage = Column(String(50), nullable=True)
    reason = Column(String(50), nullable=True)
    provenance = Column(JSONB, nullable=True)
    occurred_at = Column(DateTime, server_default=text("now()"), nullable=False)

    __table_args__ = (
        Index("ix_contact_pos_trans_user", "project_id", "user_id", "occurred_at"),
    )


class BaseCellMessage(Base):
    """A standing Message authored for a Position cell (Type / Stage / Age) —
    the Base layer. Carries a stable slot_id (the bandit/learning identity).
    Compiles to standing candidates (the Base->candidate compiler is the
    engine side; this is the authoring record)."""
    __tablename__ = "base_cell_messages"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    lifecycle_model_id = Column(Integer, ForeignKey("lifecycle_models.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(255), nullable=False, server_default="default")
    stage = Column(String(50), nullable=True)
    age_bucket = Column(String(30), nullable=True)
    channel = Column(String(50), nullable=True)
    content_mode = Column(String(20), nullable=False, server_default="text")  # text | template
    content_text = Column(Text, nullable=True)
    content_subject = Column(String(255), nullable=True)
    template_name = Column(String(255), nullable=True)       # Meta template (whatsapp)
    template_language = Column(String(20), nullable=True)
    locale = Column(String(10), nullable=True)               # i18n — BCP-47 content locale
    template_components = Column(JSONB, nullable=True)        # resolved Meta body params
    instance_id = Column(Integer, nullable=True)             # whatsapp instance for the template
    slot_id = Column(String(36), nullable=False, default=lambda: str(_uuid.uuid4()))
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_base_cell_proj", "project_id", "type", "stage", "age_bucket"),
    )


class ArmObservation(Base):
    """Append-once evidence ledger for the execution bandit (Phase 4).

    One row per observed send outcome for one execution decision (variant /
    channel / send_time / tie_break). Pure Bernoulli sufficient statistics —
    reward definition and gate formula are re-sweepable functions over these
    raw rows, which is what keeps the contested bandit choices reversible.
    Intent/episode selection has no arm identity (CHECK constraint), so reward
    can never reorder declared intent."""
    __tablename__ = "arm_observations"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    decision_class = Column(String(20), nullable=False)  # variant|channel|send_time|tie_break
    scope_key = Column(String(160), nullable=False)      # {decision_class}:{slot_id}
    arm_key = Column(String(160), nullable=False)        # option identity (+version)
    arm_version = Column(Integer, nullable=False, server_default=text("1"))
    send_log_id = Column(Integer, ForeignKey("send_logs.id", ondelete="SET NULL"), nullable=True)
    success = Column(Boolean, nullable=False)
    reward = Column(Float, nullable=True)  # reserved for future fractional reward
    observed_at = Column(DateTime, server_default=text("now()"), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "decision_class IN ('variant','channel','send_time','tie_break')",
            name="ck_arm_obs_decision_class",
        ),
        UniqueConstraint("send_log_id", "decision_class", name="uq_arm_obs_send_class"),
        Index("ix_arm_obs_scope", "project_id", "scope_key", "arm_key"),
        Index("ix_arm_obs_observed", "project_id", "observed_at"),
    )


# Import messaging models so SQLAlchemy mapper can resolve string references
# (e.g. Project.messaging_domains -> "MessagingDomain")
from app.models.messaging import *  # noqa: E402, F401, F403
from app.models.campaigns import *  # noqa: E402, F401, F403
from app.models.engine_control import *  # noqa: E402, F401, F403
from app.models.media_assets import ProjectMediaAsset  # noqa: E402, F401
from app.models.project_import import *  # noqa: E402, F401, F403
from app.models.project_setup import *  # noqa: E402, F401, F403
from app.models.project_markets import ProjectMarket  # noqa: E402, F401
