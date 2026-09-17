"""Add delivery_feedback table + soft bounce config + webhook secret

Revision ID: p7q8r9s0_086_dlvry_fdbk
Revises: o6p7q8r9_085_fix_dt
Create Date: 2026-03-26 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'p7q8r9s0_086_dlvry_fdbk'
down_revision = 'o6p7q8r9_085_fix_dt'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    return table_name in insp.get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    # 1. delivery_feedback table
    if not table_exists('delivery_feedback'):
        op.create_table(
            'delivery_feedback',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('channel', sa.String(50), nullable=False),
            sa.Column('recipient', sa.String(255), nullable=False),
            sa.Column('feedback_type', sa.String(30), nullable=False),
            sa.Column('reason', sa.String(500), nullable=True),
            sa.Column('provider', sa.String(50), nullable=True),
            sa.Column('provider_code', sa.String(50), nullable=True),
            sa.Column('provider_detail', sa.Text(), nullable=True),
            sa.Column('raw_payload', JSONB(), nullable=True),
            sa.Column('send_log_id', sa.Integer(), nullable=True),
            sa.Column('action_taken', sa.String(50), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['send_log_id'], ['send_logs.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_delivery_feedback_id', 'delivery_feedback', ['id'])
        op.create_index('ix_dlvry_fdbk_proj_ch_rcpt', 'delivery_feedback', ['project_id', 'channel', 'recipient'])
        op.create_index('ix_dlvry_fdbk_proj_user', 'delivery_feedback', ['project_id', 'user_id'])
        op.create_index('ix_dlvry_fdbk_send_log', 'delivery_feedback', ['send_log_id'])

    # 2. ProjectSendConfig: soft bounce threshold
    if not column_exists('project_send_configs', 'soft_bounce_threshold'):
        op.add_column('project_send_configs',
            sa.Column('soft_bounce_threshold', sa.Integer(), nullable=False, server_default=sa.text('3')))
    if not column_exists('project_send_configs', 'soft_bounce_window_days'):
        op.add_column('project_send_configs',
            sa.Column('soft_bounce_window_days', sa.Integer(), nullable=False, server_default=sa.text('7')))

    # 3. EmailInstance: feedback webhook secret
    if not column_exists('email_instances', 'feedback_webhook_secret'):
        op.add_column('email_instances',
            sa.Column('feedback_webhook_secret', sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_table('delivery_feedback')
    op.drop_column('project_send_configs', 'soft_bounce_threshold')
    op.drop_column('project_send_configs', 'soft_bounce_window_days')
    op.drop_column('email_instances', 'feedback_webhook_secret')
