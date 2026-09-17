"""Per-project engine rollout, decision gates and operator capability audit."""

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class ProjectEngineRollout(Base):
    __tablename__ = "project_engine_rollouts"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    feature_key = Column(String(80), nullable=False)
    mode = Column(String(20), nullable=False, server_default="inherit")
    config = Column(JSONB, nullable=True)
    version = Column(Integer, nullable=False, server_default="1")
    updated_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "feature_key", name="uq_engine_rollout_feature"),
        Index("ix_engine_rollout_project", "project_id", "feature_key"),
    )


class ProjectEngineRolloutEvent(Base):
    __tablename__ = "project_engine_rollout_events"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    feature_key = Column(String(80), nullable=False)
    previous_mode = Column(String(20), nullable=True)
    new_mode = Column(String(20), nullable=False)
    previous_config = Column(JSONB, nullable=True)
    new_config = Column(JSONB, nullable=True)
    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_engine_rollout_event_project", "project_id", "created_at"),)


class DecisionGateEvaluation(Base):
    __tablename__ = "decision_gate_evaluations"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    gate_type = Column(String(80), nullable=False)
    subject_type = Column(String(80), nullable=False)
    subject_id = Column(String(120), nullable=False)
    subject_version = Column(Integer, nullable=True)
    status = Column(String(30), nullable=False, server_default="valid")
    method = Column(String(20), nullable=False, server_default="exact")
    inputs_hash = Column(String(64), nullable=False)
    input_snapshot = Column(JSONB, nullable=False)
    baseline = Column(JSONB, nullable=False)
    options = Column(JSONB, nullable=False)
    provenance = Column(JSONB, nullable=False)
    evaluated_at = Column(DateTime, nullable=False, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        Index("ix_decision_gate_subject", "project_id", "gate_type", "subject_type", "subject_id"),
        Index("ix_decision_gate_expiry", "project_id", "status", "expires_at"),
    )


class DecisionGateChoice(Base):
    __tablename__ = "decision_gate_choices"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    evaluation_id = Column(BigInteger, ForeignKey("decision_gate_evaluations.id", ondelete="CASCADE"), nullable=False)
    option_key = Column(String(80), nullable=False)
    consequence_snapshot = Column(JSONB, nullable=False)
    reason = Column(Text, nullable=True)
    chosen_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    chosen_at = Column(DateTime, nullable=False, server_default=func.now())
    execution_type = Column(String(80), nullable=True)
    execution_id = Column(String(120), nullable=True)

    __table_args__ = (
        UniqueConstraint("project_id", "evaluation_id", name="uq_decision_gate_choice"),
        Index("ix_decision_choice_project", "project_id", "chosen_at"),
    )


class DecisionGateOutcome(Base):
    __tablename__ = "decision_gate_outcomes"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    choice_id = Column(BigInteger, ForeignKey("decision_gate_choices.id", ondelete="CASCADE"), nullable=False)
    outcome = Column(JSONB, nullable=False)
    reconciled_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("project_id", "choice_id", name="uq_decision_gate_outcome"),)


class ProviderOperationalState(Base):
    __tablename__ = "provider_operational_states"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    profile_type = Column(String(50), nullable=False)
    profile_id = Column(String(120), nullable=False)
    channel = Column(String(50), nullable=False)
    provider = Column(String(80), nullable=False)
    status = Column(String(30), nullable=False)
    quota = Column(JSONB, nullable=True)
    usage = Column(JSONB, nullable=True)
    capabilities = Column(JSONB, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(120), nullable=True)
    source = Column(String(80), nullable=False)
    observed_at = Column(DateTime, nullable=False, server_default=func.now())
    stale_after = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "profile_type", "profile_id", name="uq_provider_operational_state"),
        Index("ix_provider_state_project", "project_id", "channel", "status"),
    )


class CapabilityExecution(Base):
    __tablename__ = "capability_executions"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    capability_key = Column(String(120), nullable=False)
    phase = Column(String(20), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decision_choice_id = Column(BigInteger, ForeignKey("decision_gate_choices.id", ondelete="SET NULL"), nullable=True)
    input_hash = Column(String(64), nullable=False)
    input_payload = Column(JSONB, nullable=False)
    output_payload = Column(JSONB, nullable=True)
    status = Column(String(30), nullable=False)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("project_id", "capability_key", "idempotency_key", name="uq_capability_execution_key"),
        Index("ix_capability_execution_project", "project_id", "created_at"),
    )
