"""Add audience sync webhooks

Revision ID: j9k0l1m2_104_audience_webhooks
Revises: a7b8c9d0_096_audience_sources
Create Date: 2026-05-27

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "j9k0l1m2_104_audience_webhooks"
down_revision = "a7b8c9d0_096_audience_sources"
branch_labels = None
depends_on = None


def _inspector():
    return inspect(op.get_bind())


def _table_exists(name: str) -> bool:
    return name in _inspector().get_table_names()


def _index_exists(table: str, name: str) -> bool:
    if not _table_exists(table):
        return False
    return name in {idx["name"] for idx in _inspector().get_indexes(table)}


def _create_index_once(name: str, table: str, columns: list[str], unique: bool = False) -> None:
    if _table_exists(table) and not _index_exists(table, name):
        op.create_index(name, table, columns, unique=unique)


def _drop_index_once(name: str, table: str) -> None:
    if _index_exists(table, name):
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    if not _table_exists("messaging_audience_webhook_endpoints"):
        op.create_table(
            "messaging_audience_webhook_endpoints",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("url", sa.String(length=1000), nullable=False),
            sa.Column("secret", sa.Text(), nullable=True),
            sa.Column("events", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("last_delivery_status", sa.String(length=30), nullable=True),
            sa.Column("last_delivered_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_once("ix_messaging_audience_webhook_endpoints_id", "messaging_audience_webhook_endpoints", ["id"])
    _create_index_once("ix_messaging_audience_webhook_endpoints_project_id", "messaging_audience_webhook_endpoints", ["project_id"])
    _create_index_once("ix_messaging_audience_webhook_endpoints_is_active", "messaging_audience_webhook_endpoints", ["is_active"])
    _create_index_once(
        "ix_audience_webhook_endpoints_project_active",
        "messaging_audience_webhook_endpoints",
        ["project_id", "is_active"],
    )

    if not _table_exists("messaging_audience_webhook_deliveries"):
        op.create_table(
            "messaging_audience_webhook_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("webhook_endpoint_id", sa.Integer(), nullable=False),
            sa.Column("audience_id", sa.Integer(), nullable=True),
            sa.Column("audience_destination_id", sa.Integer(), nullable=True),
            sa.Column("sync_job_id", sa.Integer(), nullable=True),
            sa.Column("event_name", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=30), server_default="queued", nullable=False),
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("next_retry_at", sa.DateTime(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("response_status", sa.Integer(), nullable=True),
            sa.Column("response_body", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("payload_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["webhook_endpoint_id"], ["messaging_audience_webhook_endpoints.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["audience_id"], ["messaging_audiences.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["audience_destination_id"], ["messaging_audience_destinations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["sync_job_id"], ["messaging_audience_sync_jobs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    delivery_indexes = {
        "ix_aud_wh_deliv_id": ["id"],
        "ix_aud_wh_deliv_project": ["project_id"],
        "ix_aud_wh_deliv_endpoint": ["webhook_endpoint_id"],
        "ix_aud_wh_deliv_audience": ["audience_id"],
        "ix_aud_wh_deliv_aud_dest": ["audience_destination_id"],
        "ix_aud_wh_deliv_sync_job": ["sync_job_id"],
        "ix_aud_wh_deliv_event": ["event_name"],
        "ix_aud_wh_deliv_status": ["status"],
        "ix_aud_wh_deliv_next_retry": ["next_retry_at"],
        "ix_aud_wh_deliv_created": ["created_at"],
    }
    for index_name, columns in delivery_indexes.items():
        _create_index_once(index_name, "messaging_audience_webhook_deliveries", columns)
    _create_index_once("ix_audience_webhook_deliveries_due", "messaging_audience_webhook_deliveries", ["status", "next_retry_at"])
    _create_index_once("ix_audience_webhook_deliveries_project_created", "messaging_audience_webhook_deliveries", ["project_id", "created_at"])


def downgrade() -> None:
    _drop_index_once("ix_audience_webhook_deliveries_project_created", "messaging_audience_webhook_deliveries")
    _drop_index_once("ix_audience_webhook_deliveries_due", "messaging_audience_webhook_deliveries")
    if _table_exists("messaging_audience_webhook_deliveries"):
        op.drop_table("messaging_audience_webhook_deliveries")
    _drop_index_once("ix_audience_webhook_endpoints_project_active", "messaging_audience_webhook_endpoints")
    if _table_exists("messaging_audience_webhook_endpoints"):
        op.drop_table("messaging_audience_webhook_endpoints")
