"""Add usage_mode and slug to knowledge_assets

Revision ID: j0f1g2h3_060_asset_mode
Revises: i9e0f1g2_059_widen_log_act
Create Date: 2026-03-02 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'j0f1g2h3_060_asset_mode'
down_revision = 'i9e0f1g2_059_widen_log_act'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    if not column_exists('knowledge_assets', 'usage_mode'):
        op.add_column(
            'knowledge_assets',
            sa.Column(
                'usage_mode',
                sa.String(10),
                nullable=False,
                server_default='rag',
            ),
        )
        op.execute(
            "ALTER TABLE knowledge_assets ADD CONSTRAINT ck_ka_usage_mode "
            "CHECK (usage_mode IN ('rag', 'direct', 'both'))"
        )

    if not column_exists('knowledge_assets', 'slug'):
        op.add_column(
            'knowledge_assets',
            sa.Column('slug', sa.String(100), nullable=True),
        )

    if not index_exists('uq_ka_project_slug', 'knowledge_assets'):
        op.create_index(
            'uq_ka_project_slug',
            'knowledge_assets',
            ['project_id', 'slug'],
            unique=True,
            postgresql_where=sa.text('slug IS NOT NULL'),
        )


def downgrade() -> None:
    if index_exists('uq_ka_project_slug', 'knowledge_assets'):
        op.drop_index('uq_ka_project_slug', table_name='knowledge_assets')
    if column_exists('knowledge_assets', 'slug'):
        op.drop_column('knowledge_assets', 'slug')
    if column_exists('knowledge_assets', 'usage_mode'):
        op.execute(
            "ALTER TABLE knowledge_assets DROP CONSTRAINT IF EXISTS ck_ka_usage_mode"
        )
        op.drop_column('knowledge_assets', 'usage_mode')
