"""Add agent team cols to sessions

Revision ID: l9f0a1b2_028_sess_agnt
Revises: k8e9f0a1_027_agnt_teams
Create Date: 2026-02-08 10:01:00.000000

Adds nullable columns to chat_sessions and chat_messages
for Agent Teams support (team_id, current_agent_id, session_state, etc.)

Note: Made idempotent with column existence checks.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy import inspect


revision = 'l9f0a1b2_028_sess_agnt'
down_revision = 'k8e9f0a1_027_agnt_teams'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    """Check if a column already exists in a table."""
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    # Add columns to chat_sessions
    if not column_exists('chat_sessions', 'team_id'):
        op.add_column('chat_sessions', sa.Column('team_id', sa.Integer(), nullable=True))
        op.create_foreign_key(
            'fk_chat_sessions_team_id', 'chat_sessions', 'agent_teams',
            ['team_id'], ['id'], ondelete='SET NULL'
        )
        op.create_index('ix_chat_sessions_team_id', 'chat_sessions', ['team_id'])

    if not column_exists('chat_sessions', 'current_agent_id'):
        op.add_column('chat_sessions', sa.Column('current_agent_id', sa.Integer(), nullable=True))
        op.create_foreign_key(
            'fk_chat_sessions_current_agent', 'chat_sessions', 'specialist_agents',
            ['current_agent_id'], ['id'], ondelete='SET NULL'
        )
        op.create_index('ix_chat_sessions_agent_id', 'chat_sessions', ['current_agent_id'])

    if not column_exists('chat_sessions', 'session_state'):
        op.add_column('chat_sessions', sa.Column('session_state', sa.String(30), nullable=True))
        op.create_index('ix_chat_sessions_state', 'chat_sessions', ['session_state'])

    if not column_exists('chat_sessions', 'context_summary'):
        op.add_column('chat_sessions', sa.Column('context_summary', sa.Text(), nullable=True))

    if not column_exists('chat_sessions', 'extracted_entities'):
        op.add_column('chat_sessions', sa.Column('extracted_entities', JSON, nullable=True))

    if not column_exists('chat_sessions', 'routing_history'):
        op.add_column('chat_sessions', sa.Column('routing_history', JSON, nullable=True))

    # Add columns to chat_messages
    if not column_exists('chat_messages', 'agent_id'):
        op.add_column('chat_messages', sa.Column('agent_id', sa.Integer(), nullable=True))
        op.create_foreign_key(
            'fk_chat_messages_agent_id', 'chat_messages', 'specialist_agents',
            ['agent_id'], ['id'], ondelete='SET NULL'
        )
        op.create_index('ix_chat_messages_agent_id', 'chat_messages', ['agent_id'])

    if not column_exists('chat_messages', 'routing_decision'):
        op.add_column('chat_messages', sa.Column('routing_decision', JSON, nullable=True))


def downgrade() -> None:
    # Remove columns from chat_messages
    op.drop_constraint('fk_chat_messages_agent_id', 'chat_messages', type_='foreignkey')
    op.drop_index('ix_chat_messages_agent_id', table_name='chat_messages')
    op.drop_column('chat_messages', 'routing_decision')
    op.drop_column('chat_messages', 'agent_id')

    # Remove columns from chat_sessions
    op.drop_constraint('fk_chat_sessions_current_agent', 'chat_sessions', type_='foreignkey')
    op.drop_constraint('fk_chat_sessions_team_id', 'chat_sessions', type_='foreignkey')
    op.drop_index('ix_chat_sessions_state', table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_agent_id', table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_team_id', table_name='chat_sessions')
    op.drop_column('chat_sessions', 'routing_history')
    op.drop_column('chat_sessions', 'extracted_entities')
    op.drop_column('chat_sessions', 'context_summary')
    op.drop_column('chat_sessions', 'session_state')
    op.drop_column('chat_sessions', 'current_agent_id')
    op.drop_column('chat_sessions', 'team_id')
