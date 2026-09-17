"""Add click tracking: send_log_clicks table + click fields on send_logs

Revision ID: r9s0t1u2_088_click_trk
Revises: q8r9s0t1_087_bkfl_uid
Create Date: 2026-03-26 23:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'r9s0t1u2_088_click_trk'
down_revision = 'q8r9s0t1_087_bkfl_uid'
branch_labels = None
depends_on = None


def table_exists(name):
    return name in inspect(op.get_bind()).get_table_names()


def column_exists(table, col):
    return col in [c['name'] for c in inspect(op.get_bind()).get_columns(table)]


def upgrade() -> None:
    if not table_exists('send_log_clicks'):
        op.create_table(
            'send_log_clicks',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('send_log_id', sa.Integer(), nullable=False),
            sa.Column('link_index', sa.Integer(), nullable=False),
            sa.Column('original_url', sa.Text(), nullable=False),
            sa.Column('click_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('first_click_at', sa.DateTime(), nullable=True),
            sa.Column('last_click_at', sa.DateTime(), nullable=True),
            sa.Column('user_agent', sa.String(500), nullable=True),
            sa.ForeignKeyConstraint(['send_log_id'], ['send_logs.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('send_log_id', 'link_index', name='uq_slc_log_link'),
        )
        op.create_index('ix_send_log_clicks_id', 'send_log_clicks', ['id'])
        op.create_index('ix_slc_send_log', 'send_log_clicks', ['send_log_id'])

    if not column_exists('send_logs', 'click_count'):
        op.add_column('send_logs',
            sa.Column('click_count', sa.Integer(), nullable=False, server_default='0'))
    if not column_exists('send_logs', 'first_click_at'):
        op.add_column('send_logs',
            sa.Column('first_click_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_table('send_log_clicks')
    op.drop_column('send_logs', 'click_count')
    op.drop_column('send_logs', 'first_click_at')
