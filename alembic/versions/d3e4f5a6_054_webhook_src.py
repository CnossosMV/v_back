"""Add webhook_sources and webhook_ingests

Revision ID: d3e4f5a6_054_webhook_src
Revises: c2d3e4f5_053_merge_heads
Create Date: 2026-03-01 10:01:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'd3e4f5a6_054_webhook_src'
down_revision = 'c2d3e4f5_053_merge_heads'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    # ── webhook_sources ────────────────────────────────────────────────
    if not table_exists('webhook_sources'):
        op.create_table(
            'webhook_sources',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('source_slug', sa.String(100), nullable=False),
            sa.Column('display_name', sa.String(200), nullable=False),
            sa.Column('source_type', sa.String(20), nullable=False, server_default='custom'),
            sa.Column('status', sa.String(20), nullable=False, server_default='active'),
            sa.Column('secret_enc', sa.Text(), nullable=True),
            sa.Column('transformer_config', sa.JSON(), nullable=True),
            sa.Column('rate_limit_per_minute', sa.Integer(), nullable=True),
            sa.Column('last_received_at', sa.DateTime(), nullable=True),
            sa.Column('total_received', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('total_failed', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.UniqueConstraint('project_id', 'source_slug', name='uq_webhook_src_proj_slug'),
        )
        op.create_index('ix_webhook_sources_project_id', 'webhook_sources', ['project_id'])

    # ── webhook_ingests ────────────────────────────────────────────────
    if not table_exists('webhook_ingests'):
        op.create_table(
            'webhook_ingests',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('source_id', sa.Integer(), nullable=False),
            sa.Column('source_slug', sa.String(100), nullable=False),
            sa.Column('received_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('headers', sa.JSON(), nullable=True),
            sa.Column('raw_payload', JSONB(), nullable=True),
            sa.Column('signature_valid', sa.Boolean(), nullable=True),
            sa.Column('idempotency_key', sa.String(255), nullable=True),
            sa.Column('processing_status', sa.String(20), nullable=False, server_default='queued'),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_retry_at', sa.DateTime(), nullable=True),
            sa.Column('resulting_event_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['source_id'], ['webhook_sources.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['resulting_event_id'], ['messaging_events.id'], ondelete='SET NULL'),
        )
        op.create_index('ix_webhook_ingests_project_id', 'webhook_ingests', ['project_id'])
        op.create_index('ix_webhook_ingests_received_at', 'webhook_ingests', ['received_at'])
        op.create_index(
            'ix_webhook_ingests_dedup',
            'webhook_ingests',
            ['project_id', 'source_slug', 'idempotency_key'],
        )
        op.create_index(
            'ix_webhook_ingests_status',
            'webhook_ingests',
            ['processing_status'],
        )

    # ── Widen messaging_users.created_via from String(30) to String(60) ──
    if table_exists('messaging_users') and column_exists('messaging_users', 'created_via'):
        op.alter_column(
            'messaging_users',
            'created_via',
            type_=sa.String(60),
            existing_type=sa.String(30),
            existing_server_default='api',
            existing_nullable=True,
        )


def downgrade() -> None:
    # Narrow messaging_users.created_via back
    if table_exists('messaging_users') and column_exists('messaging_users', 'created_via'):
        op.alter_column(
            'messaging_users',
            'created_via',
            type_=sa.String(30),
            existing_type=sa.String(60),
            existing_server_default='api',
            existing_nullable=True,
        )

    if table_exists('webhook_ingests'):
        op.drop_table('webhook_ingests')

    if table_exists('webhook_sources'):
        op.drop_table('webhook_sources')
