"""Add event actions tables

Revision ID: e7f8a9b0_026_evt_actions
Revises: d6cc4def625f
Create Date: 2026-02-02 10:00:00.000000

Creates tables for the Event Actions system:
- event_actions: Automation rules triggered by events
- scheduled_event_actions: Delayed action queue
- event_action_executions: Audit log
- event_action_cooldowns: Deduplication tracking

Note: Made idempotent to handle potential migration state inconsistencies.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy import inspect


revision = 'e7f8a9b0_026_evt_actions'
down_revision = 'd6cc4def625f'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    """Check if a table already exists in the database."""
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # Check if tables already exist (idempotent migration)
    if table_exists('event_actions'):
        return

    # Create event_actions table
    op.create_table(
        'event_actions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('trigger_event', sa.String(200), nullable=False),
        sa.Column('conditions', JSON, server_default='[]', nullable=True),
        sa.Column('actions', JSON, nullable=False),
        sa.Column('stop_conditions', JSON, server_default='[]', nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
        sa.Column('cooldown_seconds', sa.Integer(), server_default='86400', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE')
    )
    op.create_index('ix_event_actions_id', 'event_actions', ['id'])
    op.create_index('ix_event_actions_project_id', 'event_actions', ['project_id'])
    op.create_index('ix_event_actions_trigger_event', 'event_actions', ['trigger_event'])
    op.create_index('ix_event_actions_is_active', 'event_actions', ['is_active'])
    op.create_index('ix_event_actions_proj_trigger', 'event_actions', ['project_id', 'trigger_event'])

    # Create scheduled_event_actions table
    op.create_table(
        'scheduled_event_actions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('event_action_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('action_index', sa.Integer(), nullable=False),
        sa.Column('action_config', JSON, nullable=False),
        sa.Column('variables', JSON, server_default='{}', nullable=True),
        sa.Column('scheduled_for', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(20), server_default='pending', nullable=False),
        sa.Column('executed_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancel_reason', sa.String(200), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['event_action_id'], ['event_actions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['event_id'], ['messaging_events.id'], ondelete='SET NULL'),
        sa.CheckConstraint("status IN ('pending', 'executed', 'cancelled')", name='valid_sched_action_status')
    )
    op.create_index('ix_sched_evt_actions_id', 'scheduled_event_actions', ['id'])
    op.create_index('ix_sched_evt_actions_project_id', 'scheduled_event_actions', ['project_id'])
    op.create_index('ix_sched_evt_actions_ea_id', 'scheduled_event_actions', ['event_action_id'])
    op.create_index('ix_sched_evt_actions_user_id', 'scheduled_event_actions', ['user_id'])
    op.create_index('ix_sched_evt_actions_event_id', 'scheduled_event_actions', ['event_id'])
    op.create_index('ix_sched_evt_actions_sched_for', 'scheduled_event_actions', ['scheduled_for'])
    op.create_index('ix_sched_evt_actions_status', 'scheduled_event_actions', ['status'])

    # Create event_action_executions table
    op.create_table(
        'event_action_executions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_action_id', sa.Integer(), nullable=True),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('actions_executed', JSON, server_default='[]', nullable=True),
        sa.Column('status', sa.String(20), server_default='success', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['event_action_id'], ['event_actions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['event_id'], ['messaging_events.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='SET NULL'),
        sa.CheckConstraint("status IN ('success', 'partial', 'failed')", name='valid_exec_status')
    )
    op.create_index('ix_evt_action_exec_id', 'event_action_executions', ['id'])
    op.create_index('ix_evt_action_exec_ea_id', 'event_action_executions', ['event_action_id'])
    op.create_index('ix_evt_action_exec_event_id', 'event_action_executions', ['event_id'])
    op.create_index('ix_evt_action_exec_user_id', 'event_action_executions', ['user_id'])
    op.create_index('ix_evt_action_exec_status', 'event_action_executions', ['status'])
    op.create_index('ix_evt_action_exec_started', 'event_action_executions', ['started_at'])

    # Create event_action_cooldowns table
    op.create_table(
        'event_action_cooldowns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('event_action_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('last_triggered_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['event_action_id'], ['event_actions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='CASCADE')
    )
    op.create_index('ix_evt_action_cooldowns_id', 'event_action_cooldowns', ['id'])
    op.create_index('ix_evt_action_cooldowns_proj', 'event_action_cooldowns', ['project_id'])
    op.create_index('ix_evt_action_cooldowns_expires', 'event_action_cooldowns', ['expires_at'])
    op.create_index('ix_cooldown_unique', 'event_action_cooldowns', ['event_action_id', 'user_id'], unique=True)


def downgrade() -> None:
    op.drop_table('event_action_cooldowns')
    op.drop_table('event_action_executions')
    op.drop_table('scheduled_event_actions')
    op.drop_table('event_actions')
