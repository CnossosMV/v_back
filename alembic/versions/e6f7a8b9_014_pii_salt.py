"""013_add_pii_salt

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-01-18 10:00:00.000000

Adds pii_salt field to projects table for GDPR-compliant PII hashing.
Each project gets a unique salt for hashing email/phone.
"""
from alembic import op
import sqlalchemy as sa
import secrets

# revision identifiers, used by Alembic.
revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add pii_salt column to projects table
    op.add_column(
        'projects',
        sa.Column('pii_salt', sa.String(64), nullable=True)
    )

    # Generate unique salt for existing projects
    connection = op.get_bind()
    projects = connection.execute(sa.text("SELECT id FROM projects")).fetchall()
    for project in projects:
        salt = secrets.token_hex(32)
        connection.execute(
            sa.text("UPDATE projects SET pii_salt = :salt WHERE id = :id"),
            {"salt": salt, "id": project[0]}
        )


def downgrade() -> None:
    op.drop_column('projects', 'pii_salt')
