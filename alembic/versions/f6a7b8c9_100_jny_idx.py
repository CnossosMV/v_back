"""Add journey graph performance indexes

Revision ID: f6a7b8c9_100_jny_idx
Revises: e5f6a7b8_099_anon_jcnt
Create Date: 2026-05-11

"""
from alembic import op
from sqlalchemy import inspect


revision = 'f6a7b8c9_100_jny_idx'
down_revision = 'e5f6a7b8_099_anon_jcnt'
branch_labels = None
depends_on = None


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(idx['name'] == index for idx in insp.get_indexes(table))


def upgrade() -> None:
    if not _has_index('messaging_events', 'ix_msg_events_proj_user_created'):
        op.create_index(
            'ix_msg_events_proj_user_created',
            'messaging_events',
            ['project_id', 'user_id', 'created_at'],
        )
    if not _has_index('messaging_events', 'ix_msg_events_proj_anon_created'):
        op.create_index(
            'ix_msg_events_proj_anon_created',
            'messaging_events',
            ['project_id', 'anonymous_id', 'created_at'],
        )
    if not _has_index('journey_nodes', 'ix_jn_proj_period'):
        op.create_index(
            'ix_jn_proj_period',
            'journey_nodes',
            ['project_id', 'period_start', 'period_end'],
        )
    if not _has_index('journey_edges', 'ix_je_proj_period'):
        op.create_index(
            'ix_je_proj_period',
            'journey_edges',
            ['project_id', 'period_start', 'period_end'],
        )


def downgrade() -> None:
    if _has_index('journey_edges', 'ix_je_proj_period'):
        op.drop_index('ix_je_proj_period', table_name='journey_edges')
    if _has_index('journey_nodes', 'ix_jn_proj_period'):
        op.drop_index('ix_jn_proj_period', table_name='journey_nodes')
    if _has_index('messaging_events', 'ix_msg_events_proj_anon_created'):
        op.drop_index('ix_msg_events_proj_anon_created', table_name='messaging_events')
    if _has_index('messaging_events', 'ix_msg_events_proj_user_created'):
        op.drop_index('ix_msg_events_proj_user_created', table_name='messaging_events')
