"""Add storage_key and file_size to knowledge_assets

Revision ID: n4j5k6l7_064_ka_storage
Revises: m3i4j5k6_063_hndl_links
Create Date: 2026-03-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'n4j5k6l7_064_ka_storage'
down_revision = 'm3i4j5k6_063_hndl_links'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('knowledge_assets', 'storage_key'):
        op.add_column('knowledge_assets', sa.Column('storage_key', sa.String(500), nullable=True))

    if not column_exists('knowledge_assets', 'file_size'):
        op.add_column('knowledge_assets', sa.Column('file_size', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    if column_exists('knowledge_assets', 'file_size'):
        op.drop_column('knowledge_assets', 'file_size')

    if column_exists('knowledge_assets', 'storage_key'):
        op.drop_column('knowledge_assets', 'storage_key')
