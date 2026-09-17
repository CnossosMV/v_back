"""Add project LLM config table

Revision ID: p3d4e5f6_032_proj_llm_cfg
Revises: o2c3d4e5_031_plybk_anlyt
Create Date: 2026-02-08

One row per (project, provider). A project can have up to 3 configs:
one for OpenAI, one for Anthropic, one for Google.
If a row exists for a provider, the project uses its own key for that provider.
If no row exists, the system key is used.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'p3d4e5f6_032_proj_llm_cfg'
down_revision = 'o2c3d4e5_031_plybk_anlyt'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not table_exists("project_llm_configs"):
        op.create_table(
            "project_llm_configs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(50), nullable=False),
            sa.Column("api_key_encrypted", sa.Text(), nullable=False),
            sa.Column("preferred_model", sa.String(100), nullable=True),
            sa.Column("temperature", sa.Float(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("project_id", "provider", name="uq_project_llm_project_provider"),
        )
        op.create_index("ix_project_llm_configs_id", "project_llm_configs", ["id"])
        op.create_index("ix_project_llm_configs_project_id", "project_llm_configs", ["project_id"])


def downgrade() -> None:
    op.drop_table("project_llm_configs")
