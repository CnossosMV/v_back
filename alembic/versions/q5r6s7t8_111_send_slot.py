"""Add slot_id to send_logs (bandit attribution)

Revision ID: q5r6s7t8_111_send_slot
Revises: p4q5r6s7_110_armobs
Create Date: 2026-06-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'q5r6s7t8_111_send_slot'
down_revision = 'p4q5r6s7_110_armobs'
branch_labels = None
depends_on = None


def _columns(table: str):
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    cols = _columns("send_logs")
    if "slot_id" not in cols:
        op.add_column("send_logs", sa.Column("slot_id", sa.String(length=36), nullable=True))
        op.create_index("ix_send_logs_slot", "send_logs", ["slot_id"])


def downgrade() -> None:
    if "slot_id" in _columns("send_logs"):
        op.drop_index("ix_send_logs_slot", table_name="send_logs")
        op.drop_column("send_logs", "slot_id")
