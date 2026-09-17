"""Add team session events table

Revision ID: m0a1b2c3_029_sess_evts
Revises: l9f0a1b2_028_sess_agnt
Create Date: 2026-02-08 12:00:00.000000

Creates team_session_events table for tracking events during
agent team sessions (agent switches, escalations, guardrails, etc.)

Note: Made idempotent with table existence checks.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy import inspect


revision = 'm0a1b2c3_029_sess_evts'
down_revision = 'l9f0a1b2_028_sess_agnt'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    """Check if a table already exists in the database."""
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if table_exists('team_session_events'):
        return

    op.create_table(
        'team_session_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(50), nullable=False),
        sa.Column('event_data', JSON, server_default='{}', nullable=True),
        sa.Column('agent_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['agent_id'], ['specialist_agents.id'], ondelete='SET NULL'),
    )
    op.create_index('ix_team_sess_events_id', 'team_session_events', ['id'])
    op.create_index('ix_team_sess_events_sess', 'team_session_events', ['session_id'])
    op.create_index('ix_team_sess_events_type', 'team_session_events', ['event_type'])
    op.create_index('ix_team_sess_events_agent', 'team_session_events', ['agent_id'])
    op.create_index('ix_team_sess_events_created', 'team_session_events', ['created_at'])


def downgrade() -> None:
    op.drop_table('team_session_events')
