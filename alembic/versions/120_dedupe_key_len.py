"""Increase destination dedupe key length

Revision ID: 120_dedupe_key_len
Revises: z3a4b5c6_119_loc_policy
Create Date: 2026-06-22

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "120_dedupe_key_len"
down_revision = "z3a4b5c6_119_loc_policy"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("messaging_destination_deliveries", "dedupe_key"):
        return
    op.alter_column(
        "messaging_destination_deliveries",
        "dedupe_key",
        existing_type=sa.String(length=255),
        type_=sa.String(length=1024),
        existing_nullable=False,
    )


def downgrade() -> None:
    if not _has_column("messaging_destination_deliveries", "dedupe_key"):
        return
    op.alter_column(
        "messaging_destination_deliveries",
        "dedupe_key",
        existing_type=sa.String(length=1024),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
