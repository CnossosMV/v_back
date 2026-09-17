"""Add instance_id to send_logs

Revision ID: r8s9t0u1_068_inst_id_slog
Revises: q7r8s9t0_067_ch_evt_bus
Create Date: 2026-03-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'r8s9t0u1_068_inst_id_slog'
down_revision = 'q7r8s9t0_067_ch_evt_bus'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns("send_logs")}

    if "instance_id" not in cols:
        op.add_column(
            "send_logs",
            sa.Column("instance_id", sa.Integer, nullable=True),
        )
        op.create_index("ix_send_logs_instance_id", "send_logs", ["instance_id"])


def downgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns("send_logs")}

    if "instance_id" in cols:
        op.drop_index("ix_send_logs_instance_id", table_name="send_logs")
        op.drop_column("send_logs", "instance_id")
