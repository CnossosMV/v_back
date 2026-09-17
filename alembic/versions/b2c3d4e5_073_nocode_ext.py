"""Add nocode extension tables for Chrome Extension feature

Revision ID: b2c3d4e5_073_nocode_ext
Revises: a1b2c3d4_072_wa_proj_multi
Create Date: 2026-03-06

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'b2c3d4e5_073_nocode_ext'
down_revision = 'a1b2c3d4_072_wa_proj_multi'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # ---- 1. nocode_mappings ----
    if not table_exists('nocode_mappings'):
        op.create_table(
            'nocode_mappings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('mapping_uid', sa.String(36), nullable=False),
            sa.Column('event_name', sa.String(200), nullable=False),
            sa.Column('trigger', sa.String(30), nullable=False),
            sa.Column('vef', JSONB(), nullable=False),
            sa.Column('page_pattern', sa.String(500), nullable=True),
            sa.Column('page_scope_type', sa.String(20), server_default='glob', nullable=True),
            sa.Column('viewport_scope', sa.String(20), server_default='all', nullable=True),
            sa.Column('properties', JSONB(), nullable=True),
            sa.Column('status', sa.String(20), server_default='draft', nullable=True),
            sa.Column('version', sa.Integer(), server_default='1', nullable=True),
            sa.Column('display_name', sa.String(200), nullable=True),
            sa.Column('consent_required', sa.Boolean(), server_default='false', nullable=True),
            sa.Column('created_by_id', sa.Integer(), nullable=True),
            sa.Column('updated_by_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
            sa.UniqueConstraint('mapping_uid', name='uq_nocode_mappings_uid'),
        )
        op.create_index('ix_nocode_mappings_project_id', 'nocode_mappings', ['project_id'])
        op.create_index('ix_nocode_mappings_proj_status', 'nocode_mappings', ['project_id', 'status'])
        op.create_index('ix_nocode_mappings_proj_event', 'nocode_mappings', ['project_id', 'event_name'])

    # ---- 2. nocode_config_snapshots ----
    if not table_exists('nocode_config_snapshots'):
        op.create_table(
            'nocode_config_snapshots',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('mappings_snapshot', JSONB(), nullable=False),
            sa.Column('checksum', sa.String(64), nullable=False),
            sa.Column('mapping_count', sa.Integer(), nullable=True),
            sa.Column('published_by_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['published_by_id'], ['users.id'], ondelete='SET NULL'),
            sa.UniqueConstraint('project_id', 'version', name='uq_nocode_snapshots_proj_ver'),
        )
        op.create_index('ix_nocode_config_snapshots_project_id', 'nocode_config_snapshots', ['project_id'])

    # ---- 3. nocode_debug_events ----
    if not table_exists('nocode_debug_events'):
        op.create_table(
            'nocode_debug_events',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('session_id', sa.String(64), nullable=True),
            sa.Column('mapping_uid', sa.String(36), nullable=True),
            sa.Column('event_name', sa.String(200), nullable=True),
            sa.Column('payload', JSONB(), nullable=True),
            sa.Column('vef_resolution', JSONB(), nullable=True),
            sa.Column('status', sa.String(20), server_default='received', nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_nocode_debug_events_session_id', 'nocode_debug_events', ['session_id'])
        op.create_index('ix_nocode_debug_events_created_at', 'nocode_debug_events', ['created_at'])

    # ---- 4. extension_audit_log ----
    if not table_exists('extension_audit_log'):
        op.create_table(
            'extension_audit_log',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('action', sa.String(50), nullable=True),
            sa.Column('target_type', sa.String(50), nullable=True),
            sa.Column('target_id', sa.String(50), nullable=True),
            sa.Column('details', JSONB(), nullable=True),
            sa.Column('ip_address', sa.String(45), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        )
        op.create_index('ix_ext_audit_proj_created', 'extension_audit_log', ['project_id', 'created_at'])

    # ---- 5. extension_tokens ----
    if not table_exists('extension_tokens'):
        op.create_table(
            'extension_tokens',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('token_hash', sa.String(255), nullable=True),
            sa.Column('token_prefix', sa.String(20), nullable=True),
            sa.Column('role', sa.String(20), server_default='editor', nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default='true', nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_extension_tokens_token_hash', 'extension_tokens', ['token_hash'])


def downgrade() -> None:
    op.drop_table('extension_tokens')
    op.drop_table('extension_audit_log')
    op.drop_table('nocode_debug_events')
    op.drop_table('nocode_config_snapshots')
    op.drop_table('nocode_mappings')
