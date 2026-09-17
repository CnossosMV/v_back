"""Add anonymous-visitor counters to journey nodes/edges

Revision ID: e5f6a7b8_099_anon_jcnt
Revises: d4e5f6a7_098_tpl_channels
Create Date: 2026-04-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'e5f6a7b8_099_anon_jcnt'
down_revision = 'd4e5f6a7_098_tpl_channels'
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c['name'] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column('journey_nodes', 'frequency_anon'):
        op.add_column(
            'journey_nodes',
            sa.Column('frequency_anon', sa.Integer(), nullable=False, server_default='0'),
        )
    if not _has_column('journey_nodes', 'unique_anon_visitors'):
        op.add_column(
            'journey_nodes',
            sa.Column('unique_anon_visitors', sa.Integer(), nullable=False, server_default='0'),
        )
    if not _has_column('journey_edges', 'frequency_anon'):
        op.add_column(
            'journey_edges',
            sa.Column('frequency_anon', sa.Integer(), nullable=False, server_default='0'),
        )
    if not _has_column('journey_edges', 'unique_anon_visitors'):
        op.add_column(
            'journey_edges',
            sa.Column('unique_anon_visitors', sa.Integer(), nullable=False, server_default='0'),
        )


def downgrade() -> None:
    if _has_column('journey_edges', 'unique_anon_visitors'):
        op.drop_column('journey_edges', 'unique_anon_visitors')
    if _has_column('journey_edges', 'frequency_anon'):
        op.drop_column('journey_edges', 'frequency_anon')
    if _has_column('journey_nodes', 'unique_anon_visitors'):
        op.drop_column('journey_nodes', 'unique_anon_visitors')
    if _has_column('journey_nodes', 'frequency_anon'):
        op.drop_column('journey_nodes', 'frequency_anon')
