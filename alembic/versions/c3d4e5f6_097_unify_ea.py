"""Unify Event Actions with Funnels (source, is_system, event_action_id)

Revision ID: c3d4e5f6_097_unify_ea
Revises: b2c3d4e5_096_journey
Create Date: 2026-04-13

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'c3d4e5f6_097_unify_ea'
down_revision = 'b2c3d4e5_096_journey'
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c['name'] == column for c in insp.get_columns(table))


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(i['name'] == index for i in insp.get_indexes(table))


def upgrade() -> None:
    if not _has_column('funnels', 'source'):
        op.add_column(
            'funnels',
            sa.Column('source', sa.String(length=50), nullable=False, server_default='user'),
        )
    if not _has_column('funnels', 'is_system'):
        op.add_column(
            'funnels',
            sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )
    if not _has_column('funnels', 'event_action_id'):
        op.add_column(
            'funnels',
            sa.Column('event_action_id', sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            'fk_funnels_event_action_id',
            'funnels',
            'event_actions',
            ['event_action_id'],
            ['id'],
            ondelete='CASCADE',
        )
    if not _has_column('funnels', 'cooldown_seconds'):
        op.add_column(
            'funnels',
            sa.Column('cooldown_seconds', sa.Integer(), nullable=True),
        )
    if not _has_column('funnels', 'react_to_delivery'):
        op.add_column(
            'funnels',
            sa.Column('react_to_delivery', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )

    if not _has_index('funnels', 'ix_funnels_source_event_action'):
        op.create_index(
            'ix_funnels_source_event_action',
            'funnels',
            ['source', 'event_action_id'],
        )
    if not _has_index('funnels', 'ix_funnels_user_list'):
        op.create_index(
            'ix_funnels_user_list',
            'funnels',
            ['project_id', 'status'],
            postgresql_where=sa.text('is_system = false'),
        )
    if not _has_index('funnels', 'uq_funnels_event_action_id'):
        op.create_index(
            'uq_funnels_event_action_id',
            'funnels',
            ['event_action_id'],
            unique=True,
            postgresql_where=sa.text('event_action_id IS NOT NULL'),
        )


def downgrade() -> None:
    if _has_index('funnels', 'uq_funnels_event_action_id'):
        op.drop_index('uq_funnels_event_action_id', table_name='funnels')
    if _has_index('funnels', 'ix_funnels_user_list'):
        op.drop_index('ix_funnels_user_list', table_name='funnels')
    if _has_index('funnels', 'ix_funnels_source_event_action'):
        op.drop_index('ix_funnels_source_event_action', table_name='funnels')

    if _has_column('funnels', 'react_to_delivery'):
        op.drop_column('funnels', 'react_to_delivery')
    if _has_column('funnels', 'cooldown_seconds'):
        op.drop_column('funnels', 'cooldown_seconds')
    if _has_column('funnels', 'event_action_id'):
        op.drop_constraint('fk_funnels_event_action_id', 'funnels', type_='foreignkey')
        op.drop_column('funnels', 'event_action_id')
    if _has_column('funnels', 'is_system'):
        op.drop_column('funnels', 'is_system')
    if _has_column('funnels', 'source'):
        op.drop_column('funnels', 'source')
