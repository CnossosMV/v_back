"""Add retry_of_id to send_logs

Revision ID: w4x5y6z7_093_retry_col
Revises: v3w4x5y6_092_wa_status
Create Date: 2026-03-30 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'w4x5y6z7_093_retry_col'
down_revision = 'v3w4x5y6_092_wa_status'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    if not column_exists('send_logs', 'retry_of_id'):
        op.add_column('send_logs', sa.Column(
            'retry_of_id', sa.Integer(),
            sa.ForeignKey('send_logs.id', ondelete='SET NULL'),
            nullable=True,
        ))

    if not index_exists('ix_send_logs_retry_of_id', 'send_logs'):
        op.create_index('ix_send_logs_retry_of_id', 'send_logs', ['retry_of_id'])


def downgrade() -> None:
    op.drop_index('ix_send_logs_retry_of_id', table_name='send_logs')
    op.drop_column('send_logs', 'retry_of_id')
