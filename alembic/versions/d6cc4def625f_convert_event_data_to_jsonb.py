"""convert_event_data_to_jsonb

Revision ID: d6cc4def625f
Revises: cfa6bcd514be
Create Date: 2026-01-27 01:15:00.000000

Converts tenant_events.event_data from JSON to JSONB to enable GIN index.
JSONB is more efficient for queries and supports GIN indexing.

Note: Made idempotent to handle databases where column is already JSONB.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import inspect


revision = 'd6cc4def625f'
down_revision = 'cfa6bcd514be'
branch_labels = None
depends_on = None


def get_column_type(table_name: str, column_name: str) -> str:
    """Get the type of a column."""
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = inspector.get_columns(table_name)
    for col in columns:
        if col['name'] == column_name:
            return str(col['type']).upper()
    return ''


def index_exists(index_name: str, table_name: str) -> bool:
    """Check if an index already exists on a table."""
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    # Check if column is already JSONB
    col_type = get_column_type('tenant_events', 'event_data')
    if 'JSONB' not in col_type:
        # Convert event_data column from JSON to JSONB
        op.execute('ALTER TABLE tenant_events ALTER COLUMN event_data TYPE JSONB USING event_data::jsonb')

    # Create the GIN index for fast JSON queries (if not exists)
    if not index_exists('idx_tenant_events_data_gin', 'tenant_events'):
        op.create_index(
            'idx_tenant_events_data_gin',
            'tenant_events',
            ['event_data'],
            unique=False,
            postgresql_using='gin'
        )


def downgrade() -> None:
    # Drop the GIN index
    op.drop_index('idx_tenant_events_data_gin', table_name='tenant_events', postgresql_using='gin')

    # Convert back to JSON
    op.execute('ALTER TABLE tenant_events ALTER COLUMN event_data TYPE JSON USING event_data::json')
