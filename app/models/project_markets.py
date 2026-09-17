"""First-class, tenant-owned commercial markets."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class ProjectMarket(Base):
    """A commercial/calendar boundary inside one multi-market project.

    Locale remains a content-variant axis.  The stable ``key`` is intended for
    references from campaigns and future connectivity/policy mappings; it is
    deliberately not derived from a mutable label.
    """

    __tablename__ = "project_markets"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key = Column(String(40), nullable=False)
    country_code = Column(String(2), nullable=False, index=True)
    region_codes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    timezone = Column(String(64), nullable=False)
    locales = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    calendar_tags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status = Column(String(20), nullable=False, server_default="active", index=True)
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project", back_populates="markets")

    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_project_market_key"),
        CheckConstraint(
            "status IN ('active','archived')", name="ck_project_market_status"
        ),
        CheckConstraint(
            "is_default = false OR status = 'active'",
            name="ck_project_market_default_active",
        ),
        Index("ix_project_markets_project_status", "project_id", "status", "key"),
        Index(
            "uq_project_markets_default",
            "project_id",
            unique=True,
            postgresql_where=text("is_default = true"),
            sqlite_where=text("is_default = 1"),
        ),
    )
