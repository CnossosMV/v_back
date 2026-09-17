"""Add is_blocked, blocked_at, blocked_reason to messaging_users

Revision ID: m4n5o6p7_083_blk_cnt
Revises: l3m4n5o6_082_llm_multi
Create Date: 2026-03-23 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'm4n5o6p7_083_blk_cnt'
down_revision = 'l3m4n5o6_082_llm_multi'
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
    if not column_exists('messaging_users', 'is_blocked'):
        op.add_column('messaging_users', sa.Column(
            'is_blocked', sa.Boolean(), server_default=sa.text('false'), nullable=False,
        ))

    if not column_exists('messaging_users', 'blocked_at'):
        op.add_column('messaging_users', sa.Column(
            'blocked_at', sa.DateTime(), nullable=True,
        ))

    if not column_exists('messaging_users', 'blocked_reason'):
        op.add_column('messaging_users', sa.Column(
            'blocked_reason', sa.String(255), nullable=True,
        ))

    if not index_exists('ix_msg_users_project_blocked', 'messaging_users'):
        op.create_index(
            'ix_msg_users_project_blocked', 'messaging_users',
            ['project_id', 'is_blocked'],
        )


def downgrade() -> None:
    if index_exists('ix_msg_users_project_blocked', 'messaging_users'):
        op.drop_index('ix_msg_users_project_blocked', table_name='messaging_users')
    if column_exists('messaging_users', 'blocked_reason'):
        op.drop_column('messaging_users', 'blocked_reason')
    if column_exists('messaging_users', 'blocked_at'):
        op.drop_column('messaging_users', 'blocked_at')
    if column_exists('messaging_users', 'is_blocked'):
        op.drop_column('messaging_users', 'is_blocked')
