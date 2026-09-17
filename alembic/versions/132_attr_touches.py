"""Add durable advertising attribution touches and delivery diagnostics.

Revision ID: 132_attr_touches
Revises: 131_schema_align
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "132_attr_touches"
down_revision = "131_schema_align"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _column_exists(table: str, column: str) -> bool:
    return any(item["name"] == column for item in inspect(op.get_bind()).get_columns(table))


def _index_exists(table: str, name: str) -> bool:
    return any(item["name"] == name for item in inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    if not _table_exists("messaging_attribution_touches"):
        op.create_table(
            "messaging_attribution_touches",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("anonymous_id", sa.String(length=100), nullable=True),
            sa.Column("provider", sa.String(length=30), nullable=False),
            sa.Column("identifier_type", sa.String(length=30), nullable=False),
            sa.Column("identifier_value", sa.Text(), nullable=False),
            sa.Column("identifier_hash", sa.String(length=64), nullable=False),
            sa.Column("source_event_id", sa.Integer(), nullable=True),
            sa.Column("capture_source", sa.String(length=30), server_default="legacy", nullable=False),
            sa.Column("tracking_domain_id", sa.Integer(), nullable=True),
            sa.Column("page_url", sa.String(length=1000), nullable=True),
            sa.Column("utm", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_event_id"], ["messaging_events.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["tracking_domain_id"], ["messaging_tracking_domains.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "project_id", "provider", "identifier_type", "identifier_hash",
                name="uq_attr_touch_proj_provider_identifier",
            ),
        )

    indexes = {
        "ix_messaging_attribution_touches_id": (["id"], False),
        "ix_messaging_attribution_touches_project_id": (["project_id"], False),
        "ix_messaging_attribution_touches_user_id": (["user_id"], False),
        "ix_messaging_attribution_touches_anonymous_id": (["anonymous_id"], False),
        "ix_messaging_attribution_touches_provider": (["provider"], False),
        "ix_messaging_attribution_touches_identifier_hash": (["identifier_hash"], False),
        "ix_messaging_attribution_touches_source_event_id": (["source_event_id"], False),
        "ix_messaging_attribution_touches_captured_at": (["captured_at"], False),
        "ix_messaging_attribution_touches_expires_at": (["expires_at"], False),
        "ix_attr_touch_user_provider_time": (["project_id", "user_id", "provider", "captured_at"], False),
        "ix_attr_touch_anon_provider_time": (["project_id", "anonymous_id", "provider", "captured_at"], False),
    }
    for name, (columns, unique) in indexes.items():
        if not _index_exists("messaging_attribution_touches", name):
            op.create_index(name, "messaging_attribution_touches", columns, unique=unique)

    if _table_exists("messaging_destination_deliveries") and not _column_exists(
        "messaging_destination_deliveries", "attribution_resolution"
    ):
        op.add_column(
            "messaging_destination_deliveries",
            sa.Column("attribution_resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    if _table_exists("messaging_destination_deliveries") and _column_exists(
        "messaging_destination_deliveries", "attribution_resolution"
    ):
        op.drop_column("messaging_destination_deliveries", "attribution_resolution")
    if _table_exists("messaging_attribution_touches"):
        op.drop_table("messaging_attribution_touches")
