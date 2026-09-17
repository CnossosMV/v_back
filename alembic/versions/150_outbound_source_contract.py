"""Register event templates in the outbound attention contract.

Revision ID: 150_outbound_source_contract
Revises: 149_orchestration_attention
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "150_outbound_source_contract"
down_revision = "149_orchestration_attention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messaging_templates",
        sa.Column("automation_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "messaging_templates",
        sa.Column("purpose_key", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "messaging_templates",
        sa.Column("attention_policy", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_messaging_templates_automation_enabled",
        "messaging_templates",
        ["automation_enabled"],
        unique=False,
    )
    op.create_index(
        "ix_messaging_templates_purpose_key",
        "messaging_templates",
        ["purpose_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_messaging_templates_purpose_key", table_name="messaging_templates")
    op.drop_index("ix_messaging_templates_automation_enabled", table_name="messaging_templates")
    op.drop_column("messaging_templates", "attention_policy")
    op.drop_column("messaging_templates", "purpose_key")
    op.drop_column("messaging_templates", "automation_enabled")
