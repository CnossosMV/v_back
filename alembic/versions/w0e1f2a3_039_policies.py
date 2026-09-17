"""Add policy layer tables

Revision ID: w0e1f2a3_039_policies
Revises: v9d0e1f2_038_intent_score
Create Date: 2026-02-14 10:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'w0e1f2a3_039_policies'
down_revision = 'v9d0e1f2_038_intent_score'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # --- Table 1: project_policies ---
    if not table_exists('project_policies'):
        op.create_table(
            'project_policies',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('contact_caps', sa.JSON(), nullable=True),
            sa.Column('channel_cooldowns', sa.JSON(), nullable=True),
            sa.Column('quiet_hours', sa.JSON(), nullable=True),
            sa.Column('suppression_config', sa.JSON(), nullable=True),
            sa.Column('priority_rules', sa.JSON(), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.UniqueConstraint('project_id', name='uq_project_policy'),
        )
        op.create_index('ix_project_policies_id', 'project_policies', ['id'])
        op.create_index('ix_project_policies_project_id', 'project_policies', ['project_id'])

    # --- Table 2: contact_ledger ---
    if not table_exists('contact_ledger'):
        op.create_table(
            'contact_ledger',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('channel', sa.String(30), nullable=False),
            sa.Column('source', sa.String(30), nullable=False),
            sa.Column('source_id', sa.String(100), nullable=True),
            sa.Column('sent_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_contact_ledger_id', 'contact_ledger', ['id'])
        op.create_index(
            'ix_contact_ledger_lookup',
            'contact_ledger',
            ['project_id', 'user_id', 'channel', 'sent_at'],
        )
        op.create_index('ix_contact_ledger_sent_at', 'contact_ledger', ['sent_at'])


def downgrade() -> None:
    if table_exists('contact_ledger'):
        op.drop_table('contact_ledger')
    if table_exists('project_policies'):
        op.drop_table('project_policies')
