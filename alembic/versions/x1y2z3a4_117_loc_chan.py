"""Add locale_channel_map (per-locale sender routing)

Revision ID: x1y2z3a4_117_loc_chan
Revises: w0x1y2z3_116_tpl_locale
Create Date: 2026-06-16

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'x1y2z3a4_117_loc_chan'
down_revision = 'w0x1y2z3_116_tpl_locale'
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("locale_channel_map"):
        return
    op.create_table(
        "locale_channel_map",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("channel_type", sa.String(length=20), nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=False),  # channel-specific instance id
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "locale", "channel_type", name="uq_loc_chan_proj_loc_chan"),
    )


def downgrade() -> None:
    if _has_table("locale_channel_map"):
        op.drop_table("locale_channel_map")
