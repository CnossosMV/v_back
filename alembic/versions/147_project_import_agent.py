"""Make Project Import application durable for MCP agents.

Revision ID: 147_project_import_agent
Revises: 146_consent_version_width
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "147_project_import_agent"
down_revision = "146_consent_version_width"
branch_labels = None
depends_on = None


STATUS_CONSTRAINT = "ck_project_import_status"


def _columns() -> set[str]:
    return {item["name"] for item in inspect(op.get_bind()).get_columns("project_imports")}


def _checks() -> set[str]:
    return {
        item.get("name")
        for item in inspect(op.get_bind()).get_check_constraints("project_imports")
        if item.get("name")
    }


def upgrade() -> None:
    if "apply_requested_at" not in _columns():
        op.add_column("project_imports", sa.Column("apply_requested_at", sa.DateTime(), nullable=True))
    if STATUS_CONSTRAINT in _checks():
        op.drop_constraint(STATUS_CONSTRAINT, "project_imports", type_="check")
    op.create_check_constraint(
        STATUS_CONSTRAINT,
        "project_imports",
        "status IN ('created','uploading','uploaded','validating','ready','blocked','queued',"
        "'applying','reconciling','completed','failed','canceled')",
    )
    if "project_import_upload_grants" not in set(inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "project_import_upload_grants",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("import_id", sa.String(36), sa.ForeignKey("project_imports.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "status IN ('active','claimed','used','revoked','expired')",
                name="ck_project_import_upload_grant_status",
            ),
        )
        op.create_index("ix_project_import_upload_grants_import_id", "project_import_upload_grants", ["import_id"])
        op.create_index("ix_project_import_upload_grants_project_id", "project_import_upload_grants", ["project_id"])
        op.create_index("ix_project_import_upload_grants_user_id", "project_import_upload_grants", ["user_id"])
        op.create_index("ix_project_import_upload_grants_status", "project_import_upload_grants", ["status"])


def downgrade() -> None:
    if "project_import_upload_grants" in set(inspect(op.get_bind()).get_table_names()):
        op.drop_table("project_import_upload_grants")
    op.execute("UPDATE project_imports SET status = 'ready' WHERE status = 'queued'")
    if STATUS_CONSTRAINT in _checks():
        op.drop_constraint(STATUS_CONSTRAINT, "project_imports", type_="check")
    op.create_check_constraint(
        STATUS_CONSTRAINT,
        "project_imports",
        "status IN ('created','uploading','uploaded','validating','ready','blocked',"
        "'applying','reconciling','completed','failed','canceled')",
    )
    if "apply_requested_at" in _columns():
        op.drop_column("project_imports", "apply_requested_at")
