"""006_rename_metadata_cols

Revision ID: 325852b0879b
Revises: d8f4e2a1b9c0
Create Date: 2025-11-17 17:09:11.124504

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '325852b0879b'
down_revision = 'd8f4e2a1b9c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename metadata columns to avoid SQLAlchemy reserved keyword
    op.alter_column('postforme_social_accounts', 'metadata',
                    new_column_name='platform_metadata')
    op.alter_column('postforme_posts', 'metadata',
                    new_column_name='post_metadata')
    op.alter_column('postforme_media', 'metadata',
                    new_column_name='media_metadata')


def downgrade() -> None:
    # Revert metadata column renames
    op.alter_column('postforme_social_accounts', 'platform_metadata',
                    new_column_name='metadata')
    op.alter_column('postforme_posts', 'post_metadata',
                    new_column_name='metadata')
    op.alter_column('postforme_media', 'media_metadata',
                    new_column_name='metadata')