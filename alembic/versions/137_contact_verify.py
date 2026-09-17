"""Add provider-neutral contact verification.

Revision ID: 137_contact_verify
Revises: 136_first_art_email
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "137_contact_verify"
down_revision = "136_first_art_email"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _table_exists("workspace_verification_entitlements"):
        op.create_table(
            "workspace_verification_entitlements",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("access_mode", sa.String(20), server_default="disabled", nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("granted_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("valid_until", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("workspace_id", name="uq_workspace_verify_entitlement"),
        )
        op.create_index("ix_workspace_verify_ent_workspace", "workspace_verification_entitlements", ["workspace_id"])

    if not _table_exists("project_verification_settings"):
        op.create_table(
            "project_verification_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("email_provider", sa.String(50), server_default="millionverifier", nullable=False),
            sa.Column("whatsapp_provider", sa.String(50), server_default="evolution_api", nullable=False),
            sa.Column("whatsapp_instance_id", sa.Integer(), sa.ForeignKey("whatsapp_instances.id", ondelete="SET NULL"), nullable=True),
            sa.Column("email_auto_verify", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("whatsapp_auto_verify", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("email_recheck_days", sa.Integer(), server_default="60", nullable=False),
            sa.Column("whatsapp_recheck_days", sa.Integer(), server_default="60", nullable=False),
            sa.Column("email_send_policy", sa.String(30), server_default="block_invalid", nullable=False),
            sa.Column("whatsapp_send_policy", sa.String(30), server_default="block_invalid", nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("project_id", name="uq_project_verify_settings"),
        )
        op.create_index("ix_project_verify_settings_project", "project_verification_settings", ["project_id"])

    if not _table_exists("contact_verification_jobs"):
        op.create_table(
            "contact_verification_jobs",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("verification_type", sa.String(30), nullable=False),
            sa.Column("provider", sa.String(50), nullable=False),
            sa.Column("source_type", sa.String(30), server_default="project_contacts", nullable=False),
            sa.Column("trigger_type", sa.String(30), server_default="manual", nullable=False),
            sa.Column("status", sa.String(30), server_default="queued", nullable=False),
            sa.Column("selection", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("requested_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("candidate_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("unique_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("processed_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("valid_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("invalid_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("risky_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("provider_units", sa.Integer(), server_default="0", nullable=False),
            sa.Column("billing_disposition", sa.String(20), server_default="waived", nullable=False),
            sa.Column("billing_reservation_id", sa.String(100), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_contact_verify_job_project_created", "contact_verification_jobs", ["project_id", "created_at"])
        op.create_index("ix_contact_verify_job_status", "contact_verification_jobs", ["status"])

    if not _table_exists("contact_verification_states"):
        op.create_table(
            "contact_verification_states",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("verification_type", sa.String(30), nullable=False),
            sa.Column("identifier_hash", sa.String(64), nullable=False),
            sa.Column("provider", sa.String(50), nullable=False),
            sa.Column("provider_key_source", sa.String(20), server_default="platform", nullable=False),
            sa.Column("provider_version", sa.String(30), nullable=True),
            sa.Column("canonical_status", sa.String(20), nullable=False),
            sa.Column("provider_status", sa.String(50), nullable=True),
            sa.Column("provider_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("checked_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("last_attempt_status", sa.String(20), server_default="succeeded", nullable=False),
            sa.Column("last_attempt_at", sa.DateTime(), nullable=False),
            sa.Column("last_error_code", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("project_id", "user_id", "verification_type", name="uq_contact_verify_state"),
        )
        op.create_index("ix_contact_verify_state_status", "contact_verification_states", ["project_id", "verification_type", "canonical_status"])
        op.create_index("ix_contact_verify_state_expires", "contact_verification_states", ["expires_at"])
        op.execute(sa.text("""
            INSERT INTO contact_verification_states (
                project_id, user_id, verification_type, identifier_hash, provider, provider_key_source,
                canonical_status, provider_status, checked_at, expires_at,
                last_attempt_status, last_attempt_at
            )
            SELECT project_id, id, 'whatsapp', phone_hash, 'legacy', 'legacy',
                   whatsapp_status, whatsapp_status, whatsapp_checked_at,
                   whatsapp_checked_at + interval '60 days', 'succeeded', whatsapp_checked_at
            FROM messaging_users
            WHERE whatsapp_status IN ('valid', 'invalid')
              AND whatsapp_checked_at IS NOT NULL
              AND phone_hash IS NOT NULL
            ON CONFLICT (project_id, user_id, verification_type) DO NOTHING
        """))

    if not _table_exists("contact_verification_items"):
        op.create_table(
            "contact_verification_items",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("contact_verification_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("messaging_users.id", ondelete="CASCADE"), nullable=True),
            sa.Column("source_row_key", sa.String(100), nullable=True),
            sa.Column("verification_type", sa.String(30), nullable=False),
            sa.Column("identifier_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(30), server_default="queued", nullable=False),
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("canonical_status", sa.String(20), nullable=True),
            sa.Column("provider_status", sa.String(50), nullable=True),
            sa.Column("provider_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("error_code", sa.String(100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("job_id", "user_id", "verification_type", name="uq_contact_verify_job_user"),
        )
        op.create_index("ix_contact_verify_item_job_status", "contact_verification_items", ["job_id", "status"])
        op.create_index("ix_contact_verify_item_hash", "contact_verification_items", ["identifier_hash"])

    if not _table_exists("contact_verification_operations"):
        op.create_table(
            "contact_verification_operations",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("usage_key", sa.String(64), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("contact_verification_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("provider", sa.String(50), nullable=False),
            sa.Column("provider_key_source", sa.String(20), server_default="platform", nullable=False),
            sa.Column("operation_type", sa.String(30), nullable=False),
            sa.Column("provider_operation_id", sa.String(255), nullable=True),
            sa.Column("status", sa.String(30), server_default="queued", nullable=False),
            sa.Column("item_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("provider_units", sa.Integer(), server_default="0", nullable=False),
            sa.Column("customer_units", sa.Integer(), server_default="0", nullable=False),
            sa.Column("billing_disposition", sa.String(20), server_default="waived", nullable=False),
            sa.Column("request_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("response_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("usage_key", name="uq_contact_verify_op_usage"),
        )
        op.create_index("ix_contact_verify_op_job_status", "contact_verification_operations", ["job_id", "status"])
        op.create_index("ix_contact_verify_op_external", "contact_verification_operations", ["provider_operation_id"])


def downgrade() -> None:
    for table_name in (
        "contact_verification_operations",
        "contact_verification_items",
        "contact_verification_states",
        "contact_verification_jobs",
        "project_verification_settings",
        "workspace_verification_entitlements",
    ):
        if _table_exists(table_name):
            op.drop_table(table_name)
