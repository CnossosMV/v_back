"""Add declared lane to Event Actions

Revision ID: 144_event_action_lane
Revises: 143_quiet_hours_policy
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "144_event_action_lane"
down_revision = "143_quiet_hours_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("event_actions")}
    if "lane" not in columns:
        op.add_column(
            "event_actions",
            sa.Column(
                "lane",
                sa.String(length=30),
                nullable=False,
                server_default="promotional",
            ),
        )

    checks = {check["name"] for check in inspect(bind).get_check_constraints("event_actions")}
    if "ck_event_actions_lane" not in checks:
        op.create_check_constraint(
            "ck_event_actions_lane",
            "event_actions",
            "lane IN ('transactional', 'conversational', 'manual', 'promotional')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    checks = {check["name"] for check in inspector.get_check_constraints("event_actions")}
    if "ck_event_actions_lane" in checks:
        op.drop_constraint("ck_event_actions_lane", "event_actions", type_="check")
    columns = {column["name"] for column in inspect(bind).get_columns("event_actions")}
    if "lane" in columns:
        op.drop_column("event_actions", "lane")
