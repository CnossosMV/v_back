"""Add stable slot_id to funnel_steps (slot-ID contract)

Revision ID: o3p4q5r6_109_slot_id
Revises: n2o3p4q5_108_positions
Create Date: 2026-06-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision = 'o3p4q5r6_109_slot_id'
down_revision = 'n2o3p4q5_108_positions'
branch_labels = None
depends_on = None


def _columns(table: str):
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "slot_id" not in _columns("funnel_steps"):
        op.add_column("funnel_steps", sa.Column("slot_id", sa.String(length=36), nullable=True))
        # Backfill existing steps with stable UUIDs (PG13+ has gen_random_uuid()).
        op.execute(text(
            "UPDATE funnel_steps SET slot_id = gen_random_uuid()::text WHERE slot_id IS NULL"
        ))
        op.create_index("ix_funnel_steps_slot_id", "funnel_steps", ["slot_id"])


def downgrade() -> None:
    if "slot_id" in _columns("funnel_steps"):
        op.drop_index("ix_funnel_steps_slot_id", table_name="funnel_steps")
        op.drop_column("funnel_steps", "slot_id")
