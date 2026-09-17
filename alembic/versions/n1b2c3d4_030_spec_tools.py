"""Add specialist tools and tool executions

Revision ID: n1b2c3d4_030_spec_tools
Revises: m0a1b2c3_029_sess_evts
Create Date: 2026-02-08 14:00:00.000000

Creates specialist_tools and tool_executions tables for
tool integration (webhook, MCP, catalog) in agent teams.

Note: Made idempotent with table existence checks.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy import inspect


revision = 'n1b2c3d4_030_spec_tools'
down_revision = 'm0a1b2c3_029_sess_evts'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    """Check if a table already exists in the database."""
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # specialist_tools
    if not table_exists('specialist_tools'):
        op.create_table(
            'specialist_tools',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('specialist_id', sa.Integer(), nullable=False),
            sa.Column('tool_type', sa.String(30), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('when_to_use', sa.Text(), nullable=True),
            sa.Column('config', JSON, server_default='{}', nullable=True),
            sa.Column('auth_config_encrypted', sa.Text(), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
            sa.Column('timeout_ms', sa.Integer(), server_default='10000', nullable=False),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('tool_metadata', JSON, server_default='{}', nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['specialist_id'], ['specialist_agents.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_spec_tools_id', 'specialist_tools', ['id'])
        op.create_index('ix_spec_tools_spec', 'specialist_tools', ['specialist_id'])
        op.create_index('ix_spec_tools_type', 'specialist_tools', ['tool_type'])

    # tool_executions
    if not table_exists('tool_executions'):
        op.create_table(
            'tool_executions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tool_id', sa.Integer(), nullable=True),
            sa.Column('session_id', sa.Integer(), nullable=True),
            sa.Column('message_id', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(20), nullable=False),
            sa.Column('request_data', JSON, server_default='{}', nullable=True),
            sa.Column('response_data', JSON, server_default='{}', nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('duration_ms', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['tool_id'], ['specialist_tools.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['message_id'], ['chat_messages.id'], ondelete='SET NULL'),
        )
        op.create_index('ix_tool_exec_id', 'tool_executions', ['id'])
        op.create_index('ix_tool_exec_tool', 'tool_executions', ['tool_id'])
        op.create_index('ix_tool_exec_sess', 'tool_executions', ['session_id'])
        op.create_index('ix_tool_exec_status', 'tool_executions', ['status'])
        op.create_index('ix_tool_exec_created', 'tool_executions', ['created_at'])


def downgrade() -> None:
    op.drop_table('tool_executions')
    op.drop_table('specialist_tools')
