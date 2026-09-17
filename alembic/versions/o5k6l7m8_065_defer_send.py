"""Add deferred send columns to send_logs

Revision ID: o5k6l7m8_065_defer_send
Revises: n4j5k6l7_064_ka_storage
Create Date: 2026-03-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'o5k6l7m8_065_defer_send'
down_revision = 'n4j5k6l7_064_ka_storage'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    indexes = insp.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    if not column_exists('send_logs', 'expires_at'):
        op.add_column('send_logs', sa.Column('expires_at', sa.DateTime(), nullable=True))

    if not column_exists('send_logs', 'render_context'):
        op.add_column('send_logs', sa.Column('render_context', sa.JSON(), nullable=True))

    if not column_exists('send_logs', 'deferred_source_enrollment_id'):
        op.add_column('send_logs', sa.Column(
            'deferred_source_enrollment_id',
            sa.Integer(),
            sa.ForeignKey('funnel_enrollments.id', ondelete='SET NULL'),
            nullable=True,
        ))

    if not index_exists('ix_send_logs_deferred', 'send_logs'):
        op.create_index(
            'ix_send_logs_deferred',
            'send_logs',
            ['status', 'scheduled_at'],
            postgresql_where=sa.text("status = 'deferred'"),
        )


def downgrade() -> None:
    if index_exists('ix_send_logs_deferred', 'send_logs'):
        op.drop_index('ix_send_logs_deferred', table_name='send_logs')

    if column_exists('send_logs', 'deferred_source_enrollment_id'):
        op.drop_column('send_logs', 'deferred_source_enrollment_id')

    if column_exists('send_logs', 'render_context'):
        op.drop_column('send_logs', 'render_context')

    if column_exists('send_logs', 'expires_at'):
        op.drop_column('send_logs', 'expires_at')
