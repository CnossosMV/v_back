"""Add indexes + JSONB to messaging_events

Revision ID: s6a7b8c9_035_evt_idx_jb
Revises: r5f6a7b8_034_tpl_chnl_type
Create Date: 2026-02-10 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 's6a7b8c9_035_evt_idx_jb'
down_revision = 'r5f6a7b8_034_tpl_chnl_type'
branch_labels = None
depends_on = None


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def column_type_is(table_name: str, column_name: str, type_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = inspector.get_columns(table_name)
    for col in columns:
        if col['name'] == column_name:
            return type_name.lower() in str(col['type']).lower()
    return False


def upgrade() -> None:
    # 1. Composite index on (project_id, event_name)
    if not index_exists('ix_msg_events_proj_name', 'messaging_events'):
        op.create_index(
            'ix_msg_events_proj_name',
            'messaging_events',
            ['project_id', 'event_name']
        )

    # 2. Composite index on (project_id, created_at)
    if not index_exists('ix_msg_events_proj_created', 'messaging_events'):
        op.create_index(
            'ix_msg_events_proj_created',
            'messaging_events',
            ['project_id', 'created_at']
        )

    # 3. Convert properties from JSON to JSONB
    if not column_type_is('messaging_events', 'properties', 'jsonb'):
        op.execute(
            'ALTER TABLE messaging_events ALTER COLUMN properties TYPE JSONB USING properties::jsonb'
        )

    # 4. GIN index on properties (JSONB)
    if not index_exists('ix_msg_events_props_gin', 'messaging_events'):
        op.create_index(
            'ix_msg_events_props_gin',
            'messaging_events',
            ['properties'],
            postgresql_using='gin'
        )


def downgrade() -> None:
    # Remove GIN index
    if index_exists('ix_msg_events_props_gin', 'messaging_events'):
        op.drop_index('ix_msg_events_props_gin', table_name='messaging_events')

    # Convert back to JSON
    if column_type_is('messaging_events', 'properties', 'jsonb'):
        op.execute(
            'ALTER TABLE messaging_events ALTER COLUMN properties TYPE JSON USING properties::json'
        )

    # Remove composite indexes
    if index_exists('ix_msg_events_proj_created', 'messaging_events'):
        op.drop_index('ix_msg_events_proj_created', table_name='messaging_events')

    if index_exists('ix_msg_events_proj_name', 'messaging_events'):
        op.drop_index('ix_msg_events_proj_name', table_name='messaging_events')
