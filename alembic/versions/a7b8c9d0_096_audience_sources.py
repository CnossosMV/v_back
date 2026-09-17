"""Add audience source metadata

Revision ID: a7b8c9d0_096_audience_sources
Revises: i8j9k0l1_103_audience_sync
Create Date: 2026-05-27

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "a7b8c9d0_096_audience_sources"
down_revision = "i8j9k0l1_103_audience_sync"
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def index_exists(table_name: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not table_exists("messaging_audiences"):
        return
    if not column_exists("messaging_audiences", "source_type"):
        op.add_column(
            "messaging_audiences",
            sa.Column("source_type", sa.String(length=30), nullable=False, server_default="dynamic_rule"),
        )
    if not column_exists("messaging_audiences", "source_config"):
        op.add_column(
            "messaging_audiences",
            sa.Column("source_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )
    if not index_exists("messaging_audiences", "ix_messaging_audiences_source_type"):
        op.create_index("ix_messaging_audiences_source_type", "messaging_audiences", ["source_type"])


def downgrade() -> None:
    if not table_exists("messaging_audiences"):
        return
    if index_exists("messaging_audiences", "ix_messaging_audiences_source_type"):
        op.drop_index("ix_messaging_audiences_source_type", table_name="messaging_audiences")
    if column_exists("messaging_audiences", "source_config"):
        op.drop_column("messaging_audiences", "source_config")
    if column_exists("messaging_audiences", "source_type"):
        op.drop_column("messaging_audiences", "source_type")
