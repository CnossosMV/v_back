"""Add email tracking columns to send_logs

Revision ID: p6r7s8t9_066_email_trk
Revises: o5k6l7m8_065_defer_send
Create Date: 2026-03-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'p6r7s8t9_066_email_trk'
down_revision = 'o5k6l7m8_065_defer_send'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns("send_logs")}

    if "tracking_token" not in cols:
        op.add_column("send_logs", sa.Column("tracking_token", sa.String(36), nullable=True))
        op.create_index("ix_send_logs_tracking_token", "send_logs", ["tracking_token"], unique=True)

    if "opened_at" not in cols:
        op.add_column("send_logs", sa.Column("opened_at", sa.DateTime, nullable=True))

    if "open_count" not in cols:
        op.add_column("send_logs", sa.Column("open_count", sa.Integer, server_default="0", nullable=False))


def downgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns("send_logs")}

    if "open_count" in cols:
        op.drop_column("send_logs", "open_count")
    if "opened_at" in cols:
        op.drop_column("send_logs", "opened_at")
    if "tracking_token" in cols:
        op.drop_index("ix_send_logs_tracking_token", table_name="send_logs")
        op.drop_column("send_logs", "tracking_token")
