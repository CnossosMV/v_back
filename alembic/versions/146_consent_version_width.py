"""Allow tenant-owned consent policy identifiers.

Revision ID: 146_consent_version_width
Revises: 145_project_import
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa


revision = "146_consent_version_width"
down_revision = "145_project_import"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "messaging_users",
        "consent_version",
        existing_type=sa.String(length=20),
        type_=sa.String(length=100),
        existing_nullable=True,
    )


def downgrade():
    op.alter_column(
        "messaging_users",
        "consent_version",
        existing_type=sa.String(length=100),
        type_=sa.String(length=20),
        existing_nullable=True,
    )
