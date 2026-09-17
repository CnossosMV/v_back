"""add_smart_analytics_tables

Revision ID: d5bb3a39fb14
Revises: 88c68c0aa8db
Create Date: 2026-01-27 00:07:55.794790

Note: This migration now follows 88c68c0aa8db (orphan bridge).
Tables are created idempotently to handle databases where the schema
already exists from the original 88c68c0aa8db migration.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'd5bb3a39fb14'
down_revision = '88c68c0aa8db'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    """Check if a table already exists in the database."""
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def index_exists(index_name: str, table_name: str) -> bool:
    """Check if an index already exists on a table."""
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    # Create Smart Analytics tables (idempotent - check if exists first)
    if table_exists('tenant_achievements'):
        # Tables already exist from 88c68c0aa8db, skip creation
        return

    op.create_table('tenant_achievements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=255), nullable=True),
    sa.Column('achievement_type', sa.String(length=100), nullable=False),
    sa.Column('achievement_data', sa.JSON(), nullable=True),
    sa.Column('unlocked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_tenant_achievements_tenant_type', 'tenant_achievements', ['tenant_id', 'achievement_type'], unique=False)
    op.create_index(op.f('ix_tenant_achievements_id'), 'tenant_achievements', ['id'], unique=False)
    op.create_index(op.f('ix_tenant_achievements_tenant_id'), 'tenant_achievements', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_tenant_achievements_user_id'), 'tenant_achievements', ['user_id'], unique=False)

    op.create_table('tenant_api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('api_key', sa.String(length=255), nullable=False),
    sa.Column('key_name', sa.String(length=255), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('last_used', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tenant_api_keys_api_key'), 'tenant_api_keys', ['api_key'], unique=True)
    op.create_index(op.f('ix_tenant_api_keys_id'), 'tenant_api_keys', ['id'], unique=False)
    op.create_index(op.f('ix_tenant_api_keys_is_active'), 'tenant_api_keys', ['is_active'], unique=False)
    op.create_index(op.f('ix_tenant_api_keys_tenant_id'), 'tenant_api_keys', ['tenant_id'], unique=False)

    op.create_table('tenant_automations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('rule_name', sa.String(length=255), nullable=False),
    sa.Column('trigger', sa.JSON(), nullable=False),
    sa.Column('actions', sa.JSON(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('last_triggered', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_tenant_automations_active', 'tenant_automations', ['tenant_id', 'active'], unique=False)
    op.create_index(op.f('ix_tenant_automations_active'), 'tenant_automations', ['active'], unique=False)
    op.create_index(op.f('ix_tenant_automations_id'), 'tenant_automations', ['id'], unique=False)
    op.create_index(op.f('ix_tenant_automations_tenant_id'), 'tenant_automations', ['tenant_id'], unique=False)

    op.create_table('tenant_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=255), nullable=True),
    sa.Column('event_data', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_tenant_events_button_clicks', 'tenant_events', ['tenant_id', 'created_at'], unique=False, postgresql_where="event_data->>'event' = 'button_click'")
    op.create_index('idx_tenant_events_tenant_date', 'tenant_events', ['tenant_id', 'created_at'], unique=False)
    op.create_index('idx_tenant_events_user_date', 'tenant_events', ['tenant_id', 'user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_tenant_events_created_at'), 'tenant_events', ['created_at'], unique=False)
    op.create_index(op.f('ix_tenant_events_id'), 'tenant_events', ['id'], unique=False)
    op.create_index(op.f('ix_tenant_events_tenant_id'), 'tenant_events', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_tenant_events_user_id'), 'tenant_events', ['user_id'], unique=False)

    op.create_table('tenant_marked_elements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('selector', sa.String(length=500), nullable=False),
    sa.Column('tracking_type', sa.String(length=100), nullable=False),
    sa.Column('friendly_name', sa.String(length=255), nullable=False),
    sa.Column('element_data', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_tenant_marked_elements_tenant_type', 'tenant_marked_elements', ['tenant_id', 'tracking_type'], unique=False)
    op.create_index('idx_tenant_marked_elements_unique', 'tenant_marked_elements', ['tenant_id', 'selector'], unique=True)
    op.create_index(op.f('ix_tenant_marked_elements_id'), 'tenant_marked_elements', ['id'], unique=False)
    op.create_index(op.f('ix_tenant_marked_elements_tenant_id'), 'tenant_marked_elements', ['tenant_id'], unique=False)

    op.create_table('tenant_schemas',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('schema_name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('fields', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_tenant_schemas_tenant_id', 'tenant_schemas', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_tenant_schemas_id'), 'tenant_schemas', ['id'], unique=False)
    op.create_index(op.f('ix_tenant_schemas_tenant_id'), 'tenant_schemas', ['tenant_id'], unique=False)

    op.create_table('tenant_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('settings', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tenant_settings_id'), 'tenant_settings', ['id'], unique=False)
    op.create_index(op.f('ix_tenant_settings_tenant_id'), 'tenant_settings', ['tenant_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_tenant_settings_tenant_id'), table_name='tenant_settings')
    op.drop_index(op.f('ix_tenant_settings_id'), table_name='tenant_settings')
    op.drop_table('tenant_settings')

    op.drop_index(op.f('ix_tenant_schemas_tenant_id'), table_name='tenant_schemas')
    op.drop_index(op.f('ix_tenant_schemas_id'), table_name='tenant_schemas')
    op.drop_index('idx_tenant_schemas_tenant_id', table_name='tenant_schemas')
    op.drop_table('tenant_schemas')

    op.drop_index(op.f('ix_tenant_marked_elements_tenant_id'), table_name='tenant_marked_elements')
    op.drop_index(op.f('ix_tenant_marked_elements_id'), table_name='tenant_marked_elements')
    op.drop_index('idx_tenant_marked_elements_unique', table_name='tenant_marked_elements')
    op.drop_index('idx_tenant_marked_elements_tenant_type', table_name='tenant_marked_elements')
    op.drop_table('tenant_marked_elements')

    op.drop_index(op.f('ix_tenant_events_user_id'), table_name='tenant_events')
    op.drop_index(op.f('ix_tenant_events_tenant_id'), table_name='tenant_events')
    op.drop_index(op.f('ix_tenant_events_id'), table_name='tenant_events')
    op.drop_index(op.f('ix_tenant_events_created_at'), table_name='tenant_events')
    op.drop_index('idx_tenant_events_user_date', table_name='tenant_events')
    op.drop_index('idx_tenant_events_tenant_date', table_name='tenant_events')
    op.drop_index('idx_tenant_events_button_clicks', table_name='tenant_events', postgresql_where="event_data->>'event' = 'button_click'")
    op.drop_table('tenant_events')

    op.drop_index(op.f('ix_tenant_automations_tenant_id'), table_name='tenant_automations')
    op.drop_index(op.f('ix_tenant_automations_id'), table_name='tenant_automations')
    op.drop_index(op.f('ix_tenant_automations_active'), table_name='tenant_automations')
    op.drop_index('idx_tenant_automations_active', table_name='tenant_automations')
    op.drop_table('tenant_automations')

    op.drop_index(op.f('ix_tenant_api_keys_tenant_id'), table_name='tenant_api_keys')
    op.drop_index(op.f('ix_tenant_api_keys_is_active'), table_name='tenant_api_keys')
    op.drop_index(op.f('ix_tenant_api_keys_id'), table_name='tenant_api_keys')
    op.drop_index(op.f('ix_tenant_api_keys_api_key'), table_name='tenant_api_keys')
    op.drop_table('tenant_api_keys')

    op.drop_index(op.f('ix_tenant_achievements_user_id'), table_name='tenant_achievements')
    op.drop_index(op.f('ix_tenant_achievements_tenant_id'), table_name='tenant_achievements')
    op.drop_index(op.f('ix_tenant_achievements_id'), table_name='tenant_achievements')
    op.drop_index('idx_tenant_achievements_tenant_type', table_name='tenant_achievements')
    op.drop_table('tenant_achievements')
