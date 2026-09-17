"""044 add auto track spa pages

Revision ID: b5c6d7e8_044_spa_track
Revises: a4b5c6d7_043_tpl_media
Create Date: 2026-02-17 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'b5c6d7e8_044_spa_track'
down_revision = 'a4b5c6d7_043_tpl_media'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('project_personalization_configs', 'auto_track_spa_pages'):
        op.add_column(
            'project_personalization_configs',
            sa.Column('auto_track_spa_pages', sa.Boolean(), nullable=False, server_default=sa.text('false'))
        )


def downgrade() -> None:
    if column_exists('project_personalization_configs', 'auto_track_spa_pages'):
        op.drop_column('project_personalization_configs', 'auto_track_spa_pages')
