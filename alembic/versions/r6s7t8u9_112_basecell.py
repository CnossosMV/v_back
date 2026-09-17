"""Add base_cell_messages (Base authoring)

Revision ID: r6s7t8u9_112_basecell
Revises: q5r6s7t8_111_send_slot
Create Date: 2026-06-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'r6s7t8u9_112_basecell'
down_revision = 'q5r6s7t8_111_send_slot'
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("base_cell_messages"):
        return
    op.create_table(
        "base_cell_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(length=255), nullable=False, server_default="default"),
        sa.Column("stage", sa.String(length=50), nullable=True),
        sa.Column("age_bucket", sa.String(length=30), nullable=True),
        sa.Column("channel", sa.String(length=50), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("content_subject", sa.String(length=255), nullable=True),
        sa.Column("slot_id", sa.String(length=36), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_base_cell_proj", "base_cell_messages",
                    ["project_id", "type", "stage", "age_bucket"])


def downgrade() -> None:
    if _has_table("base_cell_messages"):
        op.drop_table("base_cell_messages")
