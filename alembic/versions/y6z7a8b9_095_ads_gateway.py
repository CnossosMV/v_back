"""Add server-side ads gateway fields and delivery logs

Revision ID: y6z7a8b9_095_ads_gateway
Revises: x5y6z7a8_094_undo_merge
Create Date: 2026-05-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = 'y6z7a8b9_095_ads_gateway'
down_revision = 'x5y6z7a8_094_undo_merge'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def table_exists(table_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def index_exists(table_name: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not column_exists("messaging_events", "external_event_id"):
        op.add_column("messaging_events", sa.Column("external_event_id", sa.String(length=255), nullable=True))
    if not column_exists("messaging_events", "campaign_origin"):
        op.add_column("messaging_events", sa.Column("campaign_origin", sa.String(length=50), nullable=True))
    if not column_exists("messaging_events", "attribution"):
        op.add_column("messaging_events", sa.Column("attribution", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    if not column_exists("messaging_event_mappings", "provider_settings"):
        op.add_column("messaging_event_mappings", sa.Column("provider_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    if not table_exists("messaging_destination_deliveries"):
        op.create_table(
            "messaging_destination_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("destination_id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("event_mapping_id", sa.Integer(), nullable=True),
            sa.Column("destination_type", sa.String(length=50), nullable=False),
            sa.Column("provider_event_name", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("request_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("response_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("skipped_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["destination_id"], ["messaging_destinations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["event_id"], ["messaging_events.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["event_mapping_id"], ["messaging_event_mappings.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedupe_key"),
        )

    indexes = [
        ("ix_messaging_events_external_event_id", "messaging_events", ["external_event_id"]),
        ("ix_messaging_events_campaign_origin", "messaging_events", ["campaign_origin"]),
        ("ix_messaging_destination_deliveries_id", "messaging_destination_deliveries", ["id"]),
        ("ix_messaging_destination_deliveries_project_id", "messaging_destination_deliveries", ["project_id"]),
        ("ix_messaging_destination_deliveries_destination_id", "messaging_destination_deliveries", ["destination_id"]),
        ("ix_messaging_destination_deliveries_event_id", "messaging_destination_deliveries", ["event_id"]),
        ("ix_messaging_destination_deliveries_event_mapping_id", "messaging_destination_deliveries", ["event_mapping_id"]),
        ("ix_messaging_destination_deliveries_destination_type", "messaging_destination_deliveries", ["destination_type"]),
        ("ix_messaging_destination_deliveries_status", "messaging_destination_deliveries", ["status"]),
        ("ix_messaging_destination_deliveries_dedupe_key", "messaging_destination_deliveries", ["dedupe_key"]),
        ("ix_messaging_destination_deliveries_next_retry_at", "messaging_destination_deliveries", ["next_retry_at"]),
        ("ix_messaging_destination_deliveries_created_at", "messaging_destination_deliveries", ["created_at"]),
        ("ix_dest_delivery_project_status_due", "messaging_destination_deliveries", ["project_id", "status", "next_retry_at"]),
        ("ix_dest_delivery_event_dest", "messaging_destination_deliveries", ["event_id", "destination_id"]),
    ]
    for name, table, cols in indexes:
        if table_exists(table) and not index_exists(table, name):
            op.create_index(name, table, cols)


def downgrade() -> None:
    if table_exists("messaging_destination_deliveries"):
        op.drop_table("messaging_destination_deliveries")
    if column_exists("messaging_event_mappings", "provider_settings"):
        op.drop_column("messaging_event_mappings", "provider_settings")
    if column_exists("messaging_events", "attribution"):
        op.drop_column("messaging_events", "attribution")
    if column_exists("messaging_events", "campaign_origin"):
        op.drop_column("messaging_events", "campaign_origin")
    if column_exists("messaging_events", "external_event_id"):
        op.drop_column("messaging_events", "external_event_id")
