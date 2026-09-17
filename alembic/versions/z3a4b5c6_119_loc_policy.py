"""Add locale_policy_overrides (Guardian per-locale override)

Revision ID: z3a4b5c6_119_loc_policy
Revises: y2z3a4b5_118_basecell_loc
Create Date: 2026-06-16

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'z3a4b5c6_119_loc_policy'
down_revision = 'y2z3a4b5_118_basecell_loc'
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("locale_policy_overrides"):
        return
    op.create_table(
        "locale_policy_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("quiet_hours", sa.JSON(), nullable=True),       # {enabled,start,end,channels,timezone}
        sa.Column("timezone", sa.String(length=40), nullable=True),
        sa.Column("contact_caps", sa.JSON(), nullable=True),
        sa.Column("channel_cooldowns", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "locale", name="uq_loc_policy_proj_locale"),
    )


def downgrade() -> None:
    if _has_table("locale_policy_overrides"):
        op.drop_table("locale_policy_overrides")
