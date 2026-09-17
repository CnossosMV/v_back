"""Project migration and versioned lifecycle-model persistence.

These tables are intentionally separate from the live automation tables.  An
import can therefore be validated and approved without changing production
behaviour, and a lifecycle model can be evaluated in shadow before activation.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class LifecycleModel(Base):
    """One immutable-ish, versioned definition of Type/Stage/Age semantics."""

    __tablename__ = "lifecycle_models"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    name = Column(String(120), nullable=False)
    status = Column(String(20), nullable=False, server_default="draft", index=True)
    contract_version = Column(String(20), nullable=False, server_default="1.0")
    definition = Column(JSONB, nullable=False)
    checksum = Column(String(64), nullable=False)
    validation_report = Column(JSONB, nullable=True)
    source_import_id = Column(String(36), ForeignKey("project_imports.id", ondelete="SET NULL"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    activated_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project", foreign_keys=[project_id])

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_lifecycle_model_version"),
        CheckConstraint(
            "status IN ('draft','validated','shadow','active','archived')",
            name="ck_lifecycle_model_status",
        ),
        Index(
            "uq_lifecycle_one_active",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_lifecycle_one_shadow",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'shadow'"),
        ),
    )


class ProjectImport(Base):
    """Durable state machine for one Project Import bundle."""

    __tablename__ = "project_imports"

    id = Column(String(36), primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    client_import_id = Column(String(255), nullable=False)
    contract_version = Column(String(20), nullable=False, server_default="1.0")
    status = Column(String(20), nullable=False, server_default="created", index=True)
    import_mode = Column(String(30), nullable=False, server_default="fill_missing")
    source_system = Column(String(100), nullable=False)
    source_project = Column(String(255), nullable=True)
    source_version = Column(String(100), nullable=True)
    manifest = Column(JSONB, nullable=True)
    validation_report = Column(JSONB, nullable=True)
    reconciliation_report = Column(JSONB, nullable=True)
    record_counts = Column(JSONB, nullable=True)
    bundle_storage_key = Column(String(500), nullable=True)
    bundle_checksum = Column(String(64), nullable=True)
    bundle_size_bytes = Column(BigInteger, nullable=True)
    lifecycle_model_id = Column(Integer, ForeignKey("lifecycle_models.id", ondelete="SET NULL"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    applied_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    error_message = Column(Text, nullable=True)
    dry_run = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    uploaded_at = Column(DateTime, nullable=True)
    validated_at = Column(DateTime, nullable=True)
    apply_requested_at = Column(DateTime, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    project = relationship("Project", foreign_keys=[project_id])
    lifecycle_model = relationship("LifecycleModel", foreign_keys=[lifecycle_model_id])
    records = relationship("ProjectImportRecord", back_populates="project_import", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("project_id", "client_import_id", name="uq_project_import_client_id"),
        CheckConstraint(
            "status IN ('created','uploading','uploaded','validating','ready','blocked','queued',"
            "'applying','reconciling','completed','failed','canceled')",
            name="ck_project_import_status",
        ),
        CheckConstraint(
            "import_mode IN ('fill_missing','authoritative_fields')",
            name="ck_project_import_mode",
        ),
    )


class ProjectImportRecord(Base):
    """Idempotency and reconciliation ledger for an imported source record."""

    __tablename__ = "project_import_records"

    id = Column(BigInteger, primary_key=True)
    import_id = Column(String(36), ForeignKey("project_imports.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    record_type = Column(String(40), nullable=False)
    external_id = Column(String(255), nullable=False)
    checksum = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, server_default="pending", index=True)
    target_type = Column(String(80), nullable=True)
    target_id = Column(String(100), nullable=True)
    action = Column(String(30), nullable=True)
    error_code = Column(String(80), nullable=True)
    error_message = Column(Text, nullable=True)
    record_metadata = Column(JSONB, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project_import = relationship("ProjectImport", back_populates="records")

    __table_args__ = (
        UniqueConstraint("import_id", "record_type", "external_id", name="uq_project_import_record"),
        CheckConstraint(
            "status IN ('pending','created','updated','unchanged','skipped','blocked','failed')",
            name="ck_project_import_record_status",
        ),
        Index("ix_project_import_record_lookup", "project_id", "record_type", "external_id"),
    )


class ProjectImportUploadGrant(Base):
    """Short-lived one-time capability for the MCP-declared binary data plane."""

    __tablename__ = "project_import_upload_grants"

    id = Column(BigInteger, primary_key=True)
    import_id = Column(String(36), ForeignKey("project_imports.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    status = Column(String(20), nullable=False, server_default="active", index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','claimed','used','revoked','expired')",
            name="ck_project_import_upload_grant_status",
        ),
    )


class ProjectLifecycleCutover(Base):
    """Authoritative per-purpose orchestration owner and monotonic epoch."""

    __tablename__ = "project_lifecycle_cutovers"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose_key = Column(String(120), nullable=False)
    mode = Column(String(20), nullable=False, server_default="legacy")
    orchestration_epoch = Column(Integer, nullable=False, server_default="0")
    lifecycle_model_id = Column(Integer, ForeignKey("lifecycle_models.id", ondelete="SET NULL"), nullable=True)
    previous_mode = Column(String(20), nullable=True)
    reason = Column(Text, nullable=True)
    changed_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_at = Column(DateTime, server_default=func.now(), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "purpose_key", name="uq_project_lifecycle_cutover"),
        CheckConstraint("mode IN ('legacy','shadow','versya')", name="ck_project_cutover_mode"),
        CheckConstraint("orchestration_epoch >= 0", name="ck_project_cutover_epoch"),
    )
