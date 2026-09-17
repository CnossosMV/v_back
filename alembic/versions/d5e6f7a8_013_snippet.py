"""012_add_snippet_installed_at

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-01-18 00:00:00.000000

Adds snippet_installed_at field to messaging_domains to track
when the SDK first reports successful installation.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd5e6f7a8b9c0'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'messaging_domains',
        sa.Column('snippet_installed_at', sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('messaging_domains', 'snippet_installed_at')
