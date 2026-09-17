"""Add commercial opportunity calendar and campaign context.

Revision ID: 148_commercial_opportunity
Revises: 147_project_import_agent
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "148_commercial_opportunity"
down_revision = "147_project_import_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("opportunity_config", postgresql.JSONB(), nullable=True))
    op.create_table(
        "commercial_opportunities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_key", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("opportunity_type", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("country_code", sa.String(2), nullable=True),
        sa.Column("region_code", sa.String(80), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("peak_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("priority_source", sa.String(40), nullable=False, server_default="tie_requires_decision"),
        sa.Column("priority_reason", sa.Text(), nullable=True),
        sa.Column("purpose_keys", postgresql.JSONB(), nullable=True),
        sa.Column("context", postgresql.JSONB(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "external_key", name="uq_commercial_opportunity_key"),
    )
    op.create_index("ix_commercial_opportunities_project_id", "commercial_opportunities", ["project_id"])
    op.create_index("ix_commercial_opportunities_opportunity_type", "commercial_opportunities", ["opportunity_type"])
    op.create_index("ix_commercial_opportunities_status", "commercial_opportunities", ["status"])
    op.create_index("ix_commercial_opportunities_country_code", "commercial_opportunities", ["country_code"])
    op.create_index("ix_commercial_opportunities_region_code", "commercial_opportunities", ["region_code"])
    op.create_index("ix_commercial_opportunities_starts_at", "commercial_opportunities", ["starts_at"])
    op.create_index("ix_commercial_opportunities_expires_at", "commercial_opportunities", ["expires_at"])
    op.create_index(
        "ix_commercial_opportunity_window",
        "commercial_opportunities",
        ["project_id", "status", "starts_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("commercial_opportunities")
    op.drop_column("campaigns", "opportunity_config")
