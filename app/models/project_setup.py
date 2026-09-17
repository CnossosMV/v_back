"""Persistent, tenant-owned project setup plans and decisions."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class ProjectSetupPlan(Base):
    """A versioned configuration proposal; it never authorizes module execution."""

    __tablename__ = "project_setup_plans"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    version = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    objective = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, server_default="draft")
    plan_data = Column(JSONB, nullable=False)
    assessment_snapshot = Column(JSONB, nullable=True)
    fingerprint = Column(String(64), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    decisions = relationship(
        "ProjectSetupDecision",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="ProjectSetupDecision.id",
    )
    phase_approvals = relationship(
        "ProjectSetupPhaseApproval",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="ProjectSetupPhaseApproval.id",
    )

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_project_setup_plan_version"),
        CheckConstraint(
            "status IN ('draft','in_progress','completed','superseded','archived')",
            name="ck_project_setup_plan_status",
        ),
        Index("ix_project_setup_plans_project_status", "project_id", "status"),
    )


class ProjectSetupDecision(Base):
    """Append-only evidence or tenant decision attached to a setup plan."""

    __tablename__ = "project_setup_decisions"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(Integer, ForeignKey("project_setup_plans.id", ondelete="CASCADE"), nullable=False)
    decision_key = Column(String(160), nullable=False)
    revision = Column(Integer, nullable=False)
    evidence_kind = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False)
    value = Column(JSONB, nullable=False)
    rationale = Column(Text, nullable=False)
    sources = Column(JSONB, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    plan = relationship("ProjectSetupPlan", back_populates="decisions")

    __table_args__ = (
        UniqueConstraint("plan_id", "decision_key", "revision", name="uq_project_setup_decision_revision"),
        CheckConstraint(
            "evidence_kind IN ('observed','inferred','recommended','tenant_decided')",
            name="ck_project_setup_decision_evidence",
        ),
        CheckConstraint(
            "status IN ('proposed','accepted','rejected','superseded')",
            name="ck_project_setup_decision_status",
        ),
        Index("ix_project_setup_decisions_plan_key", "plan_id", "decision_key"),
        Index("ix_project_setup_decisions_project", "project_id"),
    )


class ProjectSetupPhaseApproval(Base):
    """Business approval bound to one exact plan/decision fingerprint."""

    __tablename__ = "project_setup_phase_approvals"

    id = Column(BigInteger, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(Integer, ForeignKey("project_setup_plans.id", ondelete="CASCADE"), nullable=False)
    phase_key = Column(String(120), nullable=False)
    plan_fingerprint = Column(String(64), nullable=False)
    approval_note = Column(Text, nullable=False)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(DateTime, nullable=False, server_default=func.now())

    plan = relationship("ProjectSetupPlan", back_populates="phase_approvals")

    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "phase_key",
            "plan_fingerprint",
            name="uq_project_setup_phase_approval_fingerprint",
        ),
        Index("ix_project_setup_phase_approvals_plan_phase", "plan_id", "phase_key"),
        Index("ix_project_setup_phase_approvals_project", "project_id"),
    )
