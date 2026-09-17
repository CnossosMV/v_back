"""
Event Graph Journey Models — materialized tables for behavioral graph visualization.
"""
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Date, Text,
    ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class JourneyNode(Base):
    """Materialized per-node stats for the event graph."""
    __tablename__ = "journey_nodes"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    event_name = Column(String(255), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    frequency = Column(Integer, nullable=False, server_default="0")
    unique_users = Column(Integer, nullable=False, server_default="0")
    # Parallel counters for anonymous (pre-identified) visitors. Disjoint from
    # the identified counters above — anonymous_id and user_id never overlap.
    frequency_anon = Column(Integer, nullable=False, server_default="0")
    unique_anon_visitors = Column(Integer, nullable=False, server_default="0")
    current_occupancy = Column(Integer, nullable=False, server_default="0")

    # Thermal breakdown
    hot_count = Column(Integer, nullable=False, server_default="0")
    warm_count = Column(Integer, nullable=False, server_default="0")
    cold_count = Column(Integer, nullable=False, server_default="0")
    dead_count = Column(Integer, nullable=False, server_default="0")

    # TOC metrics (work without goal event)
    throughput_rate = Column(Float, nullable=True)
    avg_dwell_seconds = Column(Float, nullable=True)

    # Conversion-dependent (meaningful only with goal event)
    conversion_rate = Column(Float, nullable=False, server_default="0")
    churned_from_here = Column(Integer, nullable=False, server_default="0")
    timed_out_here = Column(Integer, nullable=False, server_default="0")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    project = relationship("Project")

    __table_args__ = (
        UniqueConstraint('project_id', 'event_name', 'period_start', 'period_end', name='uq_jn_proj_evt_period'),
        Index('ix_jn_proj_period', 'project_id', 'period_start', 'period_end'),
    )


class JourneyEdge(Base):
    """Materialized transition data between events."""
    __tablename__ = "journey_edges"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_event = Column(String(255), nullable=False)
    target_event = Column(String(255), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    total_transitions = Column(Integer, nullable=False, server_default="0")
    converted_transitions = Column(Integer, nullable=False, server_default="0")
    unique_users = Column(Integer, nullable=False, server_default="0")
    # Parallel counters for anonymous visitors (see JourneyNode for rationale).
    frequency_anon = Column(Integer, nullable=False, server_default="0")
    unique_anon_visitors = Column(Integer, nullable=False, server_default="0")
    median_seconds = Column(Float, nullable=True)
    p90_seconds = Column(Float, nullable=True)

    # Bayesian priors for Thompson Sampling
    beta_alpha = Column(Float, nullable=False, server_default="1.0")
    beta_beta = Column(Float, nullable=False, server_default="1.0")

    # Gradient signal
    drop_off_rate = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    project = relationship("Project")

    __table_args__ = (
        UniqueConstraint('project_id', 'source_event', 'target_event', 'period_start', 'period_end', name='uq_je_proj_src_tgt_period'),
        Index('ix_je_proj_period', 'project_id', 'period_start', 'period_end'),
    )


class JourneyIntervention(Base):
    """Bridges SendLog into the event graph — the 'arm' for bandits."""
    __tablename__ = "journey_interventions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)

    pre_event = Column(String(255), nullable=False)
    post_event = Column(String(255), nullable=True)

    channel = Column(String(30), nullable=False)
    source_type = Column(String(50), nullable=False)
    template_id = Column(Integer, nullable=True)
    arm_id = Column(String(100), nullable=False)

    total_sent = Column(Integer, nullable=False, server_default="0")
    total_responded = Column(Integer, nullable=False, server_default="0")
    total_converted = Column(Integer, nullable=False, server_default="0")
    median_response_seconds = Column(Float, nullable=True)

    # Bayesian bandit priors
    beta_alpha = Column(Float, nullable=False, server_default="1.0")
    beta_beta = Column(Float, nullable=False, server_default="1.0")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    project = relationship("Project")

    __table_args__ = (
        UniqueConstraint('project_id', 'pre_event', 'arm_id', 'period_start', 'period_end', name='uq_ji_proj_pre_arm_period'),
    )


class JourneySnapshot(Base):
    """Per-user current state cache for event graph."""
    __tablename__ = "journey_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False)

    current_event = Column(String(255), nullable=True)
    current_event_at = Column(DateTime(timezone=True), nullable=True)
    thermal_state = Column(String(10), nullable=True)  # hot, warm, cold, dead
    terminal_state = Column(String(20), nullable=True)  # converted, churned, engagement_timeout

    last_intervention_at = Column(DateTime(timezone=True), nullable=True)
    last_intervention_arm = Column(String(100), nullable=True)
    responded_to_last = Column(Boolean, nullable=False, server_default="false")

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    project = relationship("Project", back_populates="journey_snapshots")
    user = relationship("MessagingUser")

    __table_args__ = (
        UniqueConstraint('project_id', 'user_id', name='uq_js_proj_user'),
        Index('ix_js_proj_thermal', 'project_id', 'thermal_state'),
    )


class JourneyBackfillJob(Base):
    """Tracks backfill job progress."""
    __tablename__ = "journey_backfill_jobs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    job_type = Column(String(30), nullable=False)  # sessions, interventions, full_materialize
    status = Column(String(20), nullable=False, server_default="pending")  # pending, running, completed, failed
    total_rows = Column(Integer, nullable=True)
    processed_rows = Column(Integer, nullable=False, server_default="0")
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
