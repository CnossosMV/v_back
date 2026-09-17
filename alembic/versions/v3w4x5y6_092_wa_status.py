"""Add whatsapp_status to messaging_users

Revision ID: v3w4x5y6_092_wa_status
Revises: u2v3w4x5_091_dev_fp
Create Date: 2026-03-30 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'v3w4x5y6_092_wa_status'
down_revision = 'u2v3w4x5_091_dev_fp'
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
    if not column_exists('messaging_users', 'whatsapp_status'):
        op.add_column('messaging_users', sa.Column('whatsapp_status', sa.String(20), nullable=True))

    if not column_exists('messaging_users', 'whatsapp_checked_at'):
        op.add_column('messaging_users', sa.Column('whatsapp_checked_at', sa.DateTime(), nullable=True))

    if not index_exists('ix_msg_users_wa_status', 'messaging_users'):
        op.create_index('ix_msg_users_wa_status', 'messaging_users', ['project_id', 'whatsapp_status'])


def downgrade() -> None:
    op.drop_index('ix_msg_users_wa_status', table_name='messaging_users')
    op.drop_column('messaging_users', 'whatsapp_checked_at')
    op.drop_column('messaging_users', 'whatsapp_status')
