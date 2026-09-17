"""Add messaging audience sync foundation

Revision ID: i8j9k0l1_103_audience_sync
Revises: h7i8j9k0_102_tracking_domains
Create Date: 2026-05-25

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = 'i8j9k0l1_103_audience_sync'
down_revision = 'h7i8j9k0_102_tracking_domains'
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
    if not _table_exists("messaging_ads_consent_evidence"):
        op.create_table(
            "messaging_ads_consent_evidence",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(length=50), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("source", sa.String(length=100), nullable=True),
            sa.Column("policy_version", sa.String(length=100), nullable=True),
            sa.Column("evidence_id", sa.String(length=255), nullable=True),
            sa.Column("captured_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("ip_address", sa.String(length=45), nullable=True),
            sa.Column("user_agent", sa.String(length=500), nullable=True),
            sa.Column("page_url", sa.String(length=1000), nullable=True),
            sa.Column("evidence_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_once("ix_messaging_ads_consent_evidence_id", "messaging_ads_consent_evidence", ["id"])
    _create_index_once("ix_messaging_ads_consent_evidence_project_id", "messaging_ads_consent_evidence", ["project_id"])
    _create_index_once("ix_messaging_ads_consent_evidence_user_id", "messaging_ads_consent_evidence", ["user_id"])
    _create_index_once("ix_messaging_ads_consent_evidence_scope", "messaging_ads_consent_evidence", ["scope"])
    _create_index_once("ix_messaging_ads_consent_evidence_status", "messaging_ads_consent_evidence", ["status"])
    _create_index_once("ix_messaging_ads_consent_evidence_evidence_id", "messaging_ads_consent_evidence", ["evidence_id"])
    _create_index_once("ix_messaging_ads_consent_evidence_captured_at", "messaging_ads_consent_evidence", ["captured_at"])
    _create_index_once("ix_ads_consent_project_user_scope", "messaging_ads_consent_evidence", ["project_id", "user_id", "scope"])

    if not _table_exists("messaging_audiences"):
        op.create_table(
            "messaging_audiences",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("rule_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
            sa.Column("refresh_mode", sa.String(length=20), server_default="manual", nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.Column("last_evaluated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_once("ix_messaging_audiences_id", "messaging_audiences", ["id"])
    _create_index_once("ix_messaging_audiences_project_id", "messaging_audiences", ["project_id"])
    _create_index_once("ix_messaging_audiences_status", "messaging_audiences", ["status"])
    _create_index_once("ix_audiences_project_status", "messaging_audiences", ["project_id", "status"])

    if not _table_exists("messaging_audience_destinations"):
        op.create_table(
            "messaging_audience_destinations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("audience_id", sa.Integer(), nullable=False),
            sa.Column("destination_id", sa.Integer(), nullable=True),
            sa.Column("provider_type", sa.String(length=50), nullable=False),
            sa.Column("external_audience_id", sa.String(length=255), nullable=True),
            sa.Column("external_audience_name", sa.String(length=255), nullable=True),
            sa.Column("sync_mode", sa.String(length=20), server_default="add_remove", nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("last_sync_status", sa.String(length=30), nullable=True),
            sa.Column("last_sync_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["audience_id"], ["messaging_audiences.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["destination_id"], ["messaging_destinations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_once("ix_messaging_audience_destinations_id", "messaging_audience_destinations", ["id"])
    _create_index_once("ix_messaging_audience_destinations_project_id", "messaging_audience_destinations", ["project_id"])
    _create_index_once("ix_messaging_audience_destinations_audience_id", "messaging_audience_destinations", ["audience_id"])
    _create_index_once("ix_messaging_audience_destinations_destination_id", "messaging_audience_destinations", ["destination_id"])
    _create_index_once("ix_messaging_audience_destinations_provider_type", "messaging_audience_destinations", ["provider_type"])
    _create_index_once("ix_audience_dest_project_provider", "messaging_audience_destinations", ["project_id", "provider_type"])

    if not _table_exists("messaging_audience_memberships"):
        op.create_table(
            "messaging_audience_memberships",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("audience_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=20), server_default="excluded", nullable=False),
            sa.Column("eligibility_reason", sa.String(length=100), nullable=True),
            sa.Column("eligibility_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("identifiers_present", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("last_evaluated_at", sa.DateTime(), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(), nullable=True),
            sa.Column("provider_status", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["audience_id"], ["messaging_audiences.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("audience_id", "user_id", name="uq_audience_membership_user"),
        )
    _create_index_once("ix_messaging_audience_memberships_id", "messaging_audience_memberships", ["id"])
    _create_index_once("ix_messaging_audience_memberships_project_id", "messaging_audience_memberships", ["project_id"])
    _create_index_once("ix_messaging_audience_memberships_audience_id", "messaging_audience_memberships", ["audience_id"])
    _create_index_once("ix_messaging_audience_memberships_user_id", "messaging_audience_memberships", ["user_id"])
    _create_index_once("ix_messaging_audience_memberships_state", "messaging_audience_memberships", ["state"])
    _create_index_once("ix_messaging_audience_memberships_eligibility_reason", "messaging_audience_memberships", ["eligibility_reason"])
    _create_index_once("ix_audience_membership_project_state", "messaging_audience_memberships", ["project_id", "state"])

    if not _table_exists("messaging_audience_sync_jobs"):
        op.create_table(
            "messaging_audience_sync_jobs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("audience_id", sa.Integer(), nullable=False),
            sa.Column("audience_destination_id", sa.Integer(), nullable=True),
            sa.Column("provider_type", sa.String(length=50), nullable=False),
            sa.Column("operation", sa.String(length=20), server_default="sync", nullable=False),
            sa.Column("status", sa.String(length=30), server_default="queued", nullable=False),
            sa.Column("total_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("eligible_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("add_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("remove_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("dry_run", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("request_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("response_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["audience_destination_id"], ["messaging_audience_destinations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["audience_id"], ["messaging_audiences.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_once("ix_messaging_audience_sync_jobs_id", "messaging_audience_sync_jobs", ["id"])
    _create_index_once("ix_messaging_audience_sync_jobs_project_id", "messaging_audience_sync_jobs", ["project_id"])
    _create_index_once("ix_messaging_audience_sync_jobs_audience_id", "messaging_audience_sync_jobs", ["audience_id"])
    _create_index_once("ix_messaging_audience_sync_jobs_audience_destination_id", "messaging_audience_sync_jobs", ["audience_destination_id"])
    _create_index_once("ix_messaging_audience_sync_jobs_provider_type", "messaging_audience_sync_jobs", ["provider_type"])
    _create_index_once("ix_messaging_audience_sync_jobs_status", "messaging_audience_sync_jobs", ["status"])
    _create_index_once("ix_messaging_audience_sync_jobs_created_at", "messaging_audience_sync_jobs", ["created_at"])
    _create_index_once("ix_audience_sync_jobs_project_created", "messaging_audience_sync_jobs", ["project_id", "created_at"])

    if not _table_exists("messaging_audience_sync_items"):
        op.create_table(
            "messaging_audience_sync_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("operation", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=30), server_default="queued", nullable=False),
            sa.Column("identifiers_present", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("provider_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["messaging_audience_sync_jobs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_once("ix_messaging_audience_sync_items_id", "messaging_audience_sync_items", ["id"])
    _create_index_once("ix_messaging_audience_sync_items_project_id", "messaging_audience_sync_items", ["project_id"])
    _create_index_once("ix_messaging_audience_sync_items_job_id", "messaging_audience_sync_items", ["job_id"])
    _create_index_once("ix_messaging_audience_sync_items_user_id", "messaging_audience_sync_items", ["user_id"])
    _create_index_once("ix_messaging_audience_sync_items_status", "messaging_audience_sync_items", ["status"])
    _create_index_once("ix_audience_sync_items_job_status", "messaging_audience_sync_items", ["job_id", "status"])


def downgrade() -> None:
    for idx in [
        ("ix_audience_sync_items_job_status", "messaging_audience_sync_items"),
        ("ix_messaging_audience_sync_items_status", "messaging_audience_sync_items"),
        ("ix_messaging_audience_sync_items_user_id", "messaging_audience_sync_items"),
        ("ix_messaging_audience_sync_items_job_id", "messaging_audience_sync_items"),
        ("ix_messaging_audience_sync_items_project_id", "messaging_audience_sync_items"),
        ("ix_messaging_audience_sync_items_id", "messaging_audience_sync_items"),
    ]:
        _drop_index_once(*idx)
    if _table_exists("messaging_audience_sync_items"):
        op.drop_table("messaging_audience_sync_items")

    for idx in [
        ("ix_audience_sync_jobs_project_created", "messaging_audience_sync_jobs"),
        ("ix_messaging_audience_sync_jobs_created_at", "messaging_audience_sync_jobs"),
        ("ix_messaging_audience_sync_jobs_status", "messaging_audience_sync_jobs"),
        ("ix_messaging_audience_sync_jobs_provider_type", "messaging_audience_sync_jobs"),
        ("ix_messaging_audience_sync_jobs_audience_destination_id", "messaging_audience_sync_jobs"),
        ("ix_messaging_audience_sync_jobs_audience_id", "messaging_audience_sync_jobs"),
        ("ix_messaging_audience_sync_jobs_project_id", "messaging_audience_sync_jobs"),
        ("ix_messaging_audience_sync_jobs_id", "messaging_audience_sync_jobs"),
    ]:
        _drop_index_once(*idx)
    if _table_exists("messaging_audience_sync_jobs"):
        op.drop_table("messaging_audience_sync_jobs")

    for idx in [
        ("ix_audience_membership_project_state", "messaging_audience_memberships"),
        ("ix_messaging_audience_memberships_eligibility_reason", "messaging_audience_memberships"),
        ("ix_messaging_audience_memberships_state", "messaging_audience_memberships"),
        ("ix_messaging_audience_memberships_user_id", "messaging_audience_memberships"),
        ("ix_messaging_audience_memberships_audience_id", "messaging_audience_memberships"),
        ("ix_messaging_audience_memberships_project_id", "messaging_audience_memberships"),
        ("ix_messaging_audience_memberships_id", "messaging_audience_memberships"),
    ]:
        _drop_index_once(*idx)
    if _table_exists("messaging_audience_memberships"):
        op.drop_table("messaging_audience_memberships")

    for idx in [
        ("ix_audience_dest_project_provider", "messaging_audience_destinations"),
        ("ix_messaging_audience_destinations_provider_type", "messaging_audience_destinations"),
        ("ix_messaging_audience_destinations_destination_id", "messaging_audience_destinations"),
        ("ix_messaging_audience_destinations_audience_id", "messaging_audience_destinations"),
        ("ix_messaging_audience_destinations_project_id", "messaging_audience_destinations"),
        ("ix_messaging_audience_destinations_id", "messaging_audience_destinations"),
    ]:
        _drop_index_once(*idx)
    if _table_exists("messaging_audience_destinations"):
        op.drop_table("messaging_audience_destinations")

    for idx in [
        ("ix_audiences_project_status", "messaging_audiences"),
        ("ix_messaging_audiences_status", "messaging_audiences"),
        ("ix_messaging_audiences_project_id", "messaging_audiences"),
        ("ix_messaging_audiences_id", "messaging_audiences"),
    ]:
        _drop_index_once(*idx)
    if _table_exists("messaging_audiences"):
        op.drop_table("messaging_audiences")

    for idx in [
        ("ix_ads_consent_project_user_scope", "messaging_ads_consent_evidence"),
        ("ix_messaging_ads_consent_evidence_captured_at", "messaging_ads_consent_evidence"),
        ("ix_messaging_ads_consent_evidence_evidence_id", "messaging_ads_consent_evidence"),
        ("ix_messaging_ads_consent_evidence_status", "messaging_ads_consent_evidence"),
        ("ix_messaging_ads_consent_evidence_scope", "messaging_ads_consent_evidence"),
        ("ix_messaging_ads_consent_evidence_user_id", "messaging_ads_consent_evidence"),
        ("ix_messaging_ads_consent_evidence_project_id", "messaging_ads_consent_evidence"),
        ("ix_messaging_ads_consent_evidence_id", "messaging_ads_consent_evidence"),
    ]:
        _drop_index_once(*idx)
    if _table_exists("messaging_ads_consent_evidence"):
        op.drop_table("messaging_ads_consent_evidence")
