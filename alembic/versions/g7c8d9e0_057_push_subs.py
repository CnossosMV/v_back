"""Add push_subscriptions table

Revision ID: g7c8d9e0_057_push_subs
Revises: f6b7c8d9_056_proj_mbr
Create Date: 2026-03-01 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'g7c8d9e0_057_push_subs'
down_revision = 'f6b7c8d9_056_proj_mbr'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if table_exists('push_subscriptions'):
        return

    op.create_table(
        'push_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh_key', sa.Text(), nullable=False),
        sa.Column('auth_key', sa.Text(), nullable=False),
        sa.Column('device_label', sa.String(100), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint', name='uq_push_sub_endpoint'),
    )

    op.create_index('ix_push_sub_user_active', 'push_subscriptions', ['user_id', 'is_active'])


def downgrade() -> None:
    if table_exists('push_subscriptions'):
        op.drop_table('push_subscriptions')
