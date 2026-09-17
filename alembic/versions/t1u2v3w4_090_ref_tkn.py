"""Add refresh_tokens table

Revision ID: t1u2v3w4_090_ref_tkn
Revises: s0t1u2v3_089_smtp_fdbk
Create Date: 2026-03-27 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 't1u2v3w4_090_ref_tkn'
down_revision = 's0t1u2v3_089_smtp_fdbk'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if table_exists('refresh_tokens'):
        return

    op.create_table(
        'refresh_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('is_revoked', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('device_info', sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_refresh_tkn_hash', 'refresh_tokens', ['token_hash'])
    op.create_index('ix_refresh_tkn_user', 'refresh_tokens', ['user_id'])


def downgrade() -> None:
    if not table_exists('refresh_tokens'):
        return

    op.drop_index('ix_refresh_tkn_user', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tkn_hash', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
