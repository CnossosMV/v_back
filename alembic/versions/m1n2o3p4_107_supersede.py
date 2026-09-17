"""Add intent + supersession columns to send_logs

Revision ID: m1n2o3p4_107_supersede
Revises: l1m2n3o4_106_mcp_origin
Create Date: 2026-06-13

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'm1n2o3p4_107_supersede'
down_revision = 'l1m2n3o4_106_mcp_origin'
branch_labels = None
depends_on = None


def _columns(table: str):
    insp = inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    cols = _columns("send_logs")
    if "intent_tier" not in cols:
        op.add_column("send_logs", sa.Column("intent_tier", sa.Integer(), nullable=True))
    if "intent_class" not in cols:
        op.add_column("send_logs", sa.Column("intent_class", sa.String(length=30), nullable=True))
    if "superseded_at" not in cols:
        op.add_column("send_logs", sa.Column("superseded_at", sa.DateTime(), nullable=True))
    if "superseded_reason" not in cols:
        op.add_column("send_logs", sa.Column("superseded_reason", sa.String(length=255), nullable=True))


def downgrade() -> None:
    cols = _columns("send_logs")
    for name in ("superseded_reason", "superseded_at", "intent_class", "intent_tier"):
        if name in cols:
            op.drop_column("send_logs", name)
