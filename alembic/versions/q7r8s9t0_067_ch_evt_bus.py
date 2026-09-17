"""Add react_to_delivery to event_actions

Revision ID: q7r8s9t0_067_ch_evt_bus
Revises: p6r7s8t9_066_email_trk
Create Date: 2026-03-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'q7r8s9t0_067_ch_evt_bus'
down_revision = 'p6r7s8t9_066_email_trk'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns("event_actions")}

    if "react_to_delivery" not in cols:
        op.add_column(
            "event_actions",
            sa.Column("react_to_delivery", sa.Boolean, server_default="false", nullable=False),
        )


def downgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = {c["name"] for c in insp.get_columns("event_actions")}

    if "react_to_delivery" in cols:
        op.drop_column("event_actions", "react_to_delivery")
