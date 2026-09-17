"""Provider-neutral contact and campaign persistence models.

These tables deliberately keep status/provider/channel fields as strings.  The
campaign engine is expected to validate the values it understands while
allowing new providers and channels to be introduced without PostgreSQL enum
migrations.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base


class ContactEndpoint(Base):
    """Canonical address/number owned by a contact.

    ``value`` is retained for the delivery layer, while ``value_hash`` is the
    stable lookup key used by verification and campaign snapshots.  Campaign
    work items should copy only the hash, never the raw value.
    """

    __tablename__ = "contact_endpoints"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_type = Column(String(30), nullable=False, index=True)  # email | phone | whatsapp | future
    value = Column(String(500), nullable=False)
    normalized_value = Column(String(500), nullable=True)
    value_hash = Column(String(64), nullable=False, index=True)
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    status = Column(String(30), nullable=False, server_default="active", index=True)
    source = Column(String(50), nullable=False, server_default="migration")
    endpoint_metadata = Column("metadata", JSONB, nullable=True)
    first_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    user = relationship("MessagingUser")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", "endpoint_type", "value_hash", name="uq_contact_endpoint_hash"),
        Index("ix_contact_endpoint_user_type", "project_id", "user_id", "endpoint_type", "status"),
        Index("ix_contact_endpoint_primary", "project_id", "user_id", "endpoint_type", "is_primary"),
        Index(
            "uq_contact_endpoint_one_primary",
            "project_id", "user_id", "endpoint_type",
            unique=True,
            postgresql_where=text("is_primary = true"),
        ),
    )


class ContactPermissionEvidence(Base):
    """Auditable evidence for channel/contact permission."""

    __tablename__ = "contact_permission_evidence"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id = Column(BigInteger, ForeignKey("contact_endpoints.id", ondelete="SET NULL"), nullable=True, index=True)
    channel = Column(String(50), nullable=False, index=True)
    permission_type = Column(String(50), nullable=False, server_default="marketing")
    status = Column(String(30), nullable=False, index=True)  # granted | denied | withdrawn | unknown
    source = Column(String(100), nullable=True)
    policy_version = Column(String(100), nullable=True)
    evidence_ref = Column(String(255), nullable=True, index=True)
    captured_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=True, index=True)
    evidence_metadata = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("Project")
    user = relationship("MessagingUser")
    endpoint = relationship("ContactEndpoint")

    __table_args__ = (
        Index("ix_contact_permission_user_channel", "project_id", "user_id", "channel", "permission_type", "captured_at"),
    )


class ContactGroup(Base):
    """Reusable static or rule-based group of project contacts."""

    __tablename__ = "contact_groups"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    group_type = Column(String(30), nullable=False, server_default="dynamic")  # static | dynamic | imported
    rule_config = Column(JSONB, nullable=True)
    source_ref = Column(String(255), nullable=True)
    status = Column(String(30), nullable=False, server_default="active", index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_evaluated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    created_by = relationship("User")
    memberships = relationship("ContactGroupMembership", back_populates="group", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_contact_group_project_name"),
        Index("ix_contact_group_project_status", "project_id", "status"),
    )


class ContactGroupMembership(Base):
    """Materialized membership state for a contact group."""

    __tablename__ = "contact_group_memberships"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("contact_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False, index=True)
    state = Column(String(30), nullable=False, server_default="included", index=True)  # included | excluded
    source = Column(String(50), nullable=True)
    reason = Column(String(100), nullable=True)
    evaluated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    group = relationship("ContactGroup", back_populates="memberships")
    user = relationship("MessagingUser")

    __table_args__ = (
        UniqueConstraint("project_id", "group_id", "user_id", name="uq_contact_group_member"),
        Index("ix_contact_group_membership_state", "project_id", "group_id", "state"),
    )


class Campaign(Base):
    """Reusable campaign definition; each execution is a CampaignRun."""

    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    campaign_type = Column(String(30), nullable=False, server_default="one_off")  # one_off | recurring | automation
    status = Column(String(30), nullable=False, server_default="draft", index=True)
    default_channel = Column(String(50), nullable=False, server_default="email")
    selection_config = Column(JSONB, nullable=True)
    policy_config = Column(JSONB, nullable=True)
    recurrence_config = Column(JSONB, nullable=True)
    # Structured business context used by the project-wide attention arbiter.
    # This describes the opportunity; it does not authorize or dispatch a send.
    opportunity_config = Column(JSONB, nullable=True)
    timezone = Column(String(64), nullable=True)
    starts_at = Column(DateTime, nullable=True)
    target_at = Column(DateTime, nullable=True)
    external_key = Column(String(255), nullable=True)
    # Business intent used by the project-scoped orchestration cutover gate.
    # Null preserves the behavior of campaigns created before this contract.
    purpose_key = Column(String(120), nullable=True, index=True)
    attention_policy = Column(JSONB, nullable=True)
    version = Column(Integer, nullable=False, server_default="1")
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    created_by = relationship("User")
    actions = relationship("CampaignAction", back_populates="campaign", cascade="all, delete-orphan", order_by="CampaignAction.position")
    variants = relationship("CampaignVariant", back_populates="campaign", cascade="all, delete-orphan")
    runs = relationship("CampaignRun", back_populates="campaign", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_campaign_project_name"),
        UniqueConstraint("project_id", "external_key", name="uq_campaign_project_external"),
        Index("ix_campaign_project_status", "project_id", "status"),
    )


class CommercialOpportunity(Base):
    """A tenant-owned, market-scoped commercial calendar occurrence.

    Predictable recurring windows (weekday/month boundary) can be generated in
    preview, while externally sourced holidays and local moments are persisted
    as explicit occurrences.  Campaigns reference their semantic context; the
    Selection layer remains the only component allowed to choose attention.
    """

    __tablename__ = "commercial_opportunities"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    external_key = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    opportunity_type = Column(String(80), nullable=False, index=True)
    status = Column(String(30), nullable=False, server_default="draft", index=True)
    country_code = Column(String(2), nullable=True, index=True)
    region_code = Column(String(80), nullable=True, index=True)
    timezone = Column(String(64), nullable=False, server_default="UTC")
    starts_at = Column(DateTime, nullable=False, index=True)
    peak_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    priority = Column(Integer, nullable=False, server_default="0")
    priority_source = Column(String(40), nullable=False, server_default="tie_requires_decision")
    priority_reason = Column(Text, nullable=True)
    purpose_keys = Column(JSONB, nullable=True)
    context = Column(JSONB, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    created_by = relationship("User")

    __table_args__ = (
        UniqueConstraint("project_id", "external_key", name="uq_commercial_opportunity_key"),
        Index(
            "ix_commercial_opportunity_window",
            "project_id", "status", "starts_at", "expires_at",
        ),
    )


class CampaignRecipe(Base):
    """Persistent tenant policy that plans recurring campaign episodes.

    A recipe may materialize planning records and draft campaigns, but it is
    deliberately not executable.  Campaign activation and run authorization
    remain separate, impact-gated operations.
    """

    __tablename__ = "campaign_recipes"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    lifecycle_model_id = Column(Integer, ForeignKey("lifecycle_models.id", ondelete="RESTRICT"), nullable=False, index=True)
    external_key = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default="draft", index=True)
    purpose_key = Column(String(120), nullable=False, index=True)
    timezone = Column(String(64), nullable=False, server_default="UTC")
    country_code = Column(String(2), nullable=True)
    region_code = Column(String(80), nullable=True)
    default_channel = Column(String(50), nullable=False, server_default="email")
    selection_config = Column(JSONB, nullable=False)
    policy_config = Column(JSONB, nullable=True)
    attention_policy = Column(JSONB, nullable=False)
    schedule_rules = Column(JSONB, nullable=False)
    include_persisted_opportunities = Column(Boolean, nullable=False, server_default=text("true"))
    content_mode = Column(String(30), nullable=False, server_default="agent_draft")
    content_brief = Column(JSONB, nullable=False)
    fixed_actions = Column(JSONB, nullable=True)
    autonomy_policy = Column(JSONB, nullable=False)
    planning_horizon_days = Column(Integer, nullable=False, server_default="45")
    decision_lead_hours = Column(Integer, nullable=False, server_default="168")
    version = Column(Integer, nullable=False, server_default="1")
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_materialized_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    lifecycle_model = relationship("LifecycleModel")
    created_by = relationship("User")
    episodes = relationship(
        "CampaignRecipeEpisode",
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="CampaignRecipeEpisode.starts_at",
    )

    __table_args__ = (
        UniqueConstraint("project_id", "external_key", name="uq_campaign_recipe_external"),
        CheckConstraint("status IN ('draft','active','paused','archived')", name="ck_campaign_recipe_status"),
        CheckConstraint(
            "content_mode IN ('fixed','agent_draft','bounded_autonomy')",
            name="ck_campaign_recipe_content_mode",
        ),
        Index("ix_campaign_recipe_project_status", "project_id", "status"),
    )


class CampaignRecipeEpisode(Base):
    """One deterministic occurrence planned from a CampaignRecipe."""

    __tablename__ = "campaign_recipe_episodes"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    recipe_id = Column(Integer, ForeignKey("campaign_recipes.id", ondelete="CASCADE"), nullable=False, index=True)
    recipe_version = Column(Integer, nullable=False)
    occurrence_key = Column(String(255), nullable=False)
    status = Column(String(30), nullable=False, server_default="awaiting_copy", index=True)
    opportunity_snapshot = Column(JSONB, nullable=False)
    collision_snapshot = Column(JSONB, nullable=True)
    collision_resolution = Column(JSONB, nullable=True)
    content_brief_snapshot = Column(JSONB, nullable=False)
    decision_due_at = Column(DateTime, nullable=False, index=True)
    starts_at = Column(DateTime, nullable=False, index=True)
    target_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id = Column(BigInteger, ForeignKey("campaign_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    authored_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    materialized_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    recipe = relationship("CampaignRecipe", back_populates="episodes")
    campaign = relationship("Campaign")
    run = relationship("CampaignRun")
    authored_by = relationship("User")

    __table_args__ = (
        UniqueConstraint("recipe_id", "occurrence_key", name="uq_campaign_recipe_occurrence"),
        UniqueConstraint("project_id", "campaign_id", name="uq_campaign_episode_campaign"),
        CheckConstraint(
            "status IN ('awaiting_copy','ready','blocked','materialized','scheduled','completed','canceled','failed','expired','superseded')",
            name="ck_campaign_recipe_episode_status",
        ),
        Index(
            "ix_campaign_recipe_episode_inbox",
            "project_id", "status", "decision_due_at", "starts_at",
        ),
    )


class CampaignAction(Base):
    """Ordered action in a campaign (send, tag, funnel enrollment, etc.)."""

    __tablename__ = "campaign_actions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False, server_default="0")
    action_type = Column(String(50), nullable=False)
    channel = Column(String(50), nullable=True)
    config = Column(JSONB, nullable=True)
    status = Column(String(30), nullable=False, server_default="active", index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    campaign = relationship("Campaign", back_populates="actions")
    variants = relationship("CampaignVariant", back_populates="action", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_campaign_action_order", "campaign_id", "position"),
    )


class CampaignVariant(Base):
    """Locale/content variant for a campaign action."""

    __tablename__ = "campaign_variants"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    action_id = Column(Integer, ForeignKey("campaign_actions.id", ondelete="CASCADE"), nullable=True, index=True)
    variant_key = Column(String(100), nullable=False)
    locale = Column(String(20), nullable=True, index=True)
    template_id = Column(Integer, ForeignKey("messaging_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    subject = Column(String(500), nullable=True)
    body = Column(Text, nullable=True)
    variant_config = Column(JSONB, nullable=True)
    weight = Column(Integer, nullable=False, server_default="100")
    status = Column(String(30), nullable=False, server_default="active", index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    campaign = relationship("Campaign", back_populates="variants")
    action = relationship("CampaignAction", back_populates="variants")
    template = relationship("MessagingTemplate")

    __table_args__ = (
        UniqueConstraint("campaign_id", "action_id", "variant_key", "locale", name="uq_campaign_variant_key"),
        Index("ix_campaign_variant_locale", "project_id", "campaign_id", "locale", "status"),
    )


class CampaignRun(Base):
    """Immutable audience snapshot and execution state for one campaign run."""

    __tablename__ = "campaign_runs"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    run_key = Column(String(255), nullable=False)
    trigger_type = Column(String(50), nullable=False, server_default="manual")
    status = Column(String(30), nullable=False, server_default="draft", index=True)
    audience_snapshot = Column(JSONB, nullable=True)
    audience_hash = Column(String(64), nullable=True, index=True)
    timezone = Column(String(64), nullable=True)
    starts_at = Column(DateTime, nullable=True)
    target_at = Column(DateTime, nullable=True)
    deadline_at = Column(DateTime, nullable=True)
    capacity_plan = Column(JSONB, nullable=True)
    candidate_count = Column(Integer, nullable=False, server_default="0")
    eligible_count = Column(Integer, nullable=False, server_default="0")
    planned_count = Column(Integer, nullable=False, server_default="0")
    queued_count = Column(Integer, nullable=False, server_default="0")
    sent_count = Column(Integer, nullable=False, server_default="0")
    delivered_count = Column(Integer, nullable=False, server_default="0")
    failed_count = Column(Integer, nullable=False, server_default="0")
    skipped_count = Column(Integer, nullable=False, server_default="0")
    canceled_count = Column(Integer, nullable=False, server_default="0")
    cancel_requested = Column(Boolean, nullable=False, server_default=text("false"))
    error_message = Column(Text, nullable=True)
    decision_choice_id = Column(BigInteger, ForeignKey("decision_gate_choices.id", ondelete="SET NULL"), nullable=True, index=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    project = relationship("Project")
    campaign = relationship("Campaign", back_populates="runs")
    requested_by = relationship("User")
    waves = relationship("CampaignWave", back_populates="run", cascade="all, delete-orphan", order_by="CampaignWave.position")
    recipients = relationship("CampaignRecipient", back_populates="run", cascade="all, delete-orphan")
    reservations = relationship("ChannelCapacityReservation", back_populates="run", cascade="all, delete-orphan")
    policy_override = relationship(
        "CampaignPolicyOverride",
        back_populates="run",
        cascade="all, delete-orphan",
        uselist=False,
        overlaps="project",
    )

    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_campaign_run_project_id"),
        UniqueConstraint("project_id", "campaign_id", "run_key", name="uq_campaign_run_key"),
        Index("ix_campaign_run_due", "project_id", "status", "starts_at"),
    )


class CampaignPolicyOverride(Base):
    """One-run authorization to relax selected *soft* pacing rules.

    The allow-list is validated in the service and deliberately excludes
    consent, opt-out, deliverability, provider capacity and priority.  The
    row is bound to one immutable run snapshot so an approval can never be
    reused by another campaign execution.
    """

    __tablename__ = "campaign_policy_overrides"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(BigInteger, nullable=False, index=True)
    status = Column(String(30), nullable=False, server_default="approved", index=True)
    override_keys = Column(JSONB, nullable=False)
    reason = Column(Text, nullable=False)
    risk_acknowledged = Column(Boolean, nullable=False, server_default=text("false"))
    dual_approval_required = Column(Boolean, nullable=False, server_default=text("false"))
    planned_count_snapshot = Column(Integer, nullable=False)
    policy_snapshot = Column(JSONB, nullable=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    requested_at = Column(DateTime, server_default=func.now(), nullable=False)
    approved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)

    project = relationship("Project", overlaps="policy_override,run")
    run = relationship("CampaignRun", back_populates="policy_override", overlaps="project")
    requested_by = relationship("User", foreign_keys=[requested_by_user_id])
    approved_by = relationship("User", foreign_keys=[approved_by_user_id])
    revoked_by = relationship("User", foreign_keys=[revoked_by_user_id])

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["campaign_runs.project_id", "campaign_runs.id"],
            name="fk_campaign_override_run_project",
            ondelete="CASCADE",
        ),
        UniqueConstraint("project_id", "run_id", name="uq_campaign_override_run"),
        Index("ix_campaign_override_state", "project_id", "status", "expires_at"),
    )


class CampaignWave(Base):
    """A dated/capacity-bounded portion of a run."""

    __tablename__ = "campaign_waves"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(BigInteger, ForeignKey("campaign_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False, server_default="0")
    status = Column(String(30), nullable=False, server_default="planned", index=True)
    approval_mode = Column(String(30), nullable=False, server_default="none")
    is_canary = Column(Boolean, nullable=False, server_default=text("false"))
    approved_at = Column(DateTime, nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    scheduled_at = Column(DateTime, nullable=True, index=True)
    window_start = Column(DateTime, nullable=True)
    window_end = Column(DateTime, nullable=True)
    planned_count = Column(Integer, nullable=False, server_default="0")
    queued_count = Column(Integer, nullable=False, server_default="0")
    sent_count = Column(Integer, nullable=False, server_default="0")
    failed_count = Column(Integer, nullable=False, server_default="0")
    skipped_count = Column(Integer, nullable=False, server_default="0")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    project = relationship("Project")
    run = relationship("CampaignRun", back_populates="waves")
    recipients = relationship("CampaignRecipient", back_populates="wave")

    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_campaign_wave_position"),
        Index("ix_campaign_wave_due", "project_id", "status", "scheduled_at"),
    )


class CampaignRecipient(Base):
    """Durable, idempotent per-contact action item."""

    __tablename__ = "campaign_recipients"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(BigInteger, ForeignKey("campaign_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    wave_id = Column(BigInteger, ForeignKey("campaign_waves.id", ondelete="SET NULL"), nullable=True, index=True)
    action_id = Column(Integer, ForeignKey("campaign_actions.id", ondelete="SET NULL"), nullable=True, index=True)
    variant_id = Column(Integer, ForeignKey("campaign_variants.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="SET NULL"), nullable=True, index=True)
    endpoint_id = Column(BigInteger, ForeignKey("contact_endpoints.id", ondelete="SET NULL"), nullable=True, index=True)
    channel = Column(String(50), nullable=False, server_default="email", index=True)
    endpoint_hash = Column(String(64), nullable=True, index=True)
    idempotency_key = Column(String(255), nullable=False)
    status = Column(String(30), nullable=False, server_default="pending", index=True)
    suppression_reason = Column(String(120), nullable=True)
    provider = Column(String(80), nullable=True)
    delivery_profile_id = Column(Integer, ForeignKey("channel_delivery_profiles.id", ondelete="SET NULL"), nullable=True, index=True)
    sender_identity_id = Column(Integer, ForeignKey("channel_sender_identities.id", ondelete="SET NULL"), nullable=True, index=True)
    send_log_id = Column(Integer, ForeignKey("send_logs.id", ondelete="SET NULL"), nullable=True, index=True)
    scheduled_at = Column(DateTime, nullable=True, index=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    last_error_code = Column(String(100), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    queued_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    project = relationship("Project")
    run = relationship("CampaignRun", back_populates="recipients")
    wave = relationship("CampaignWave", back_populates="recipients")
    action = relationship("CampaignAction")
    variant = relationship("CampaignVariant")
    user = relationship("MessagingUser")
    endpoint = relationship("ContactEndpoint")
    delivery_profile = relationship("ChannelDeliveryProfile")
    sender_identity = relationship("ChannelSenderIdentity")
    send_log = relationship("SendLog")

    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_campaign_recipient_idem"),
        Index("ix_campaign_recipient_dispatch", "project_id", "status", "scheduled_at"),
    )


class ChannelSenderIdentity(Base):
    """Provider-facing sender identity, separate from credentials."""

    __tablename__ = "channel_sender_identities"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(50), nullable=False, index=True)
    provider = Column(String(80), nullable=False, index=True)
    identity_key = Column(String(255), nullable=False)
    address = Column(String(500), nullable=True)
    display_name = Column(String(255), nullable=True)
    reply_to = Column(String(500), nullable=True)
    external_instance_id = Column(String(255), nullable=True)
    status = Column(String(30), nullable=False, server_default="active", index=True)
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    capabilities = Column(JSONB, nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")

    __table_args__ = (
        UniqueConstraint("project_id", "channel", "identity_key", name="uq_sender_identity_key"),
        Index("ix_sender_identity_active", "project_id", "channel", "status", "is_default"),
        Index(
            "uq_sender_identity_one_default",
            "project_id",
            "channel",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
    )


class ChannelDeliveryProfile(Base):
    """Capacity, health and routing policy for a channel/provider profile."""

    __tablename__ = "channel_delivery_profiles"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(50), nullable=False, index=True)
    provider = Column(String(80), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    sender_identity_id = Column(Integer, ForeignKey("channel_sender_identities.id", ondelete="SET NULL"), nullable=True, index=True)
    max_per_minute = Column(Integer, nullable=True)
    max_per_hour = Column(Integer, nullable=True)
    max_per_day = Column(Integer, nullable=True)
    concurrency_limit = Column(Integer, nullable=True)
    priority = Column(Integer, nullable=False, server_default="0")
    weight = Column(Integer, nullable=False, server_default="100")
    timezone = Column(String(64), nullable=True)
    warmup_config = Column(JSONB, nullable=True)
    health_status = Column(String(30), nullable=False, server_default="unknown", index=True)
    status = Column(String(30), nullable=False, server_default="active", index=True)
    config = Column(JSONB, nullable=True)
    last_health_check_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project")
    sender_identity = relationship("ChannelSenderIdentity")
    reservations = relationship("ChannelCapacityReservation", back_populates="delivery_profile", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("project_id", "channel", "provider", "name", name="uq_delivery_profile_name"),
        Index("ix_delivery_profile_select", "project_id", "channel", "status", "health_status", "priority"),
    )


class ChannelCapacityReservation(Base):
    """Persisted capacity reservation preventing concurrent campaign overbooking."""

    __tablename__ = "channel_capacity_reservations"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    delivery_profile_id = Column(Integer, ForeignKey("channel_delivery_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(BigInteger, ForeignKey("campaign_runs.id", ondelete="CASCADE"), nullable=True, index=True)
    wave_id = Column(BigInteger, ForeignKey("campaign_waves.id", ondelete="SET NULL"), nullable=True, index=True)
    bucket_start = Column(DateTime, nullable=False, index=True)
    bucket_end = Column(DateTime, nullable=False)
    units_reserved = Column(Integer, nullable=False, server_default="0")
    units_consumed = Column(Integer, nullable=False, server_default="0")
    status = Column(String(30), nullable=False, server_default="reserved", index=True)
    reservation_key = Column(String(255), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    released_at = Column(DateTime, nullable=True)

    project = relationship("Project")
    delivery_profile = relationship("ChannelDeliveryProfile", back_populates="reservations")
    run = relationship("CampaignRun", back_populates="reservations")
    wave = relationship("CampaignWave")

    __table_args__ = (
        UniqueConstraint("delivery_profile_id", "bucket_start", "reservation_key", name="uq_capacity_reservation_key"),
        Index("ix_capacity_reservation_bucket", "project_id", "delivery_profile_id", "bucket_start", "status"),
    )


class ContactVerificationRequest(Base):
    """Durable single-contact verification request, linked to a bulk job when any."""

    __tablename__ = "contact_verification_requests"

    id = Column(BigInteger, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=True, index=True)
    endpoint_id = Column(BigInteger, ForeignKey("contact_endpoints.id", ondelete="SET NULL"), nullable=True, index=True)
    job_id = Column(BigInteger, ForeignKey("contact_verification_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    verification_type = Column(String(50), nullable=False, index=True)
    provider = Column(String(80), nullable=False)
    trigger_type = Column(String(50), nullable=False, server_default="manual")
    idempotency_key = Column(String(255), nullable=False)
    status = Column(String(30), nullable=False, server_default="queued", index=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    provider_status = Column(String(100), nullable=True)
    canonical_status = Column(String(30), nullable=True)
    error_code = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    request_metadata = Column(JSONB, nullable=True)
    requested_at = Column(DateTime, server_default=func.now(), nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    project = relationship("Project")
    user = relationship("MessagingUser")
    endpoint = relationship("ContactEndpoint")
    job = relationship("ContactVerificationJob")

    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_verify_request_idem"),
        Index("ix_verify_request_due", "project_id", "status", "requested_at"),
    )


class OperationalAlert(Base):
    """Deduplicated operational alert for provider/campaign health issues."""

    __tablename__ = "operational_alerts"

    id = Column(BigInteger, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    alert_type = Column(String(80), nullable=False, index=True)
    severity = Column(String(20), nullable=False, server_default="warning", index=True)
    status = Column(String(30), nullable=False, server_default="open", index=True)
    dedupe_key = Column(String(255), nullable=False)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=True)
    context = Column(JSONB, nullable=True)
    first_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    acknowledged_at = Column(DateTime, nullable=True)
    acknowledged_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = relationship("Workspace")
    project = relationship("Project")
    acknowledged_by = relationship("User", foreign_keys=[acknowledged_by_user_id])
    resolved_by = relationship("User", foreign_keys=[resolved_by_user_id])

    __table_args__ = (
        UniqueConstraint("workspace_id", "dedupe_key", name="uq_operational_alert_dedupe"),
        Index("ix_operational_alert_open", "workspace_id", "status", "severity", "last_seen_at"),
    )
