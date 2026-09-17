"""Per-project placeholder recipient pattern

Revision ID: 124_placeholder_ptn
Revises: 123_send_pace
Create Date: 2026-07-08 00:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = '124_placeholder_ptn'
down_revision = '123_send_pace'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(c["name"] == column_name for c in inspector.get_columns(table_name))


def upgrade() -> None:
    if not column_exists("project_send_configs", "placeholder_email_pattern"):
        op.add_column(
            "project_send_configs",
            sa.Column("placeholder_email_pattern", sa.String(length=500), nullable=True),
        )


def downgrade() -> None:
    if column_exists("project_send_configs", "placeholder_email_pattern"):
        op.drop_column("project_send_configs", "placeholder_email_pattern")
