"""Add contact position + transition tables (Phase 3)

Revision ID: n2o3p4q5_108_positions
Revises: m1n2o3p4_107_supersede
Create Date: 2026-06-13

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'n2o3p4q5_108_positions'
down_revision = 'm1n2o3p4_107_supersede'
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("contact_positions"):
        op.create_table(
            "contact_positions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("type", sa.String(length=255), nullable=False, server_default="default"),
            sa.Column("stage", sa.String(length=50), nullable=True),
            sa.Column("age_bucket", sa.String(length=30), nullable=True),
            sa.Column("position_entered_at", sa.DateTime(), nullable=True),
            sa.Column("type_entered_at", sa.DateTime(), nullable=True),
            sa.Column("stage_entered_at", sa.DateTime(), nullable=True),
            sa.Column("computed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("project_id", "user_id", name="uq_contact_position_user"),
        )
        op.create_index("ix_contact_pos_proj_type", "contact_positions",
                        ["project_id", "type", "stage", "age_bucket"])

    if not _has_table("contact_position_transitions"):
        op.create_table(
            "contact_position_transitions",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("from_type", sa.String(length=255), nullable=True),
            sa.Column("to_type", sa.String(length=255), nullable=True),
            sa.Column("from_stage", sa.String(length=50), nullable=True),
            sa.Column("to_stage", sa.String(length=50), nullable=True),
            sa.Column("reason", sa.String(length=50), nullable=True),
            sa.Column("occurred_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_contact_pos_trans_user", "contact_position_transitions",
                        ["project_id", "user_id", "occurred_at"])


def downgrade() -> None:
    for tbl in ("contact_position_transitions", "contact_positions"):
        if _has_table(tbl):
            op.drop_table(tbl)
