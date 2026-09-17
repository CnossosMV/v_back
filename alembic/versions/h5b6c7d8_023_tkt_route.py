"""022_add_support_ticket_agent_routing

Revision ID: h5b6c7d8e9f0
Revises: g4a5b6c7d8e9
Create Date: 2026-01-19 10:05:00.000000

Adds unified chat system models - Phase 2:
- SupportTicket (Human Intervention)
- ChatbotAgentRouting (Agent Order Configuration)
- AgentConfig (Per-agent settings in routing)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

# revision identifiers, used by Alembic.
revision = 'h5b6c7d8e9f0'
down_revision = 'g4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create support_tickets table
    op.create_table(
        'support_tickets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('chatbot_id', sa.Integer(), nullable=True),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('ticket_number', sa.String(50), nullable=False, unique=True),  # PROJ-001 format
        sa.Column('status', sa.String(30), default='open', nullable=False),  # open, in_progress, waiting_customer, resolved, closed
        sa.Column('priority', sa.String(20), default='medium', nullable=False),  # low, medium, high, urgent
        sa.Column('assigned_to_user_id', sa.Integer(), nullable=True),
        sa.Column('human_takeover', sa.Boolean(), default=False, nullable=False),
        sa.Column('bot_can_resume', sa.Boolean(), default=True, nullable=False),
        sa.Column('customer_identifier', sa.String(255), nullable=False),  # phone, email, etc.
        sa.Column('customer_name', sa.String(200), nullable=True),
        sa.Column('channel', sa.String(50), nullable=False),  # web, whatsapp, sms
        sa.Column('tags', ARRAY(sa.String), nullable=True),
        sa.Column('ticket_metadata', sa.JSON(), default={}, nullable=True),
        sa.Column('internal_notes', sa.Text(), nullable=True),
        sa.Column('escalation_reason', sa.String(500), nullable=True),
        sa.Column('first_response_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['chatbot_id'], ['chatbots.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['assigned_to_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting_customer', 'resolved', 'closed')",
            name='valid_ticket_status'
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name='valid_ticket_priority'
        )
    )

    # Create indexes for support_tickets
    op.create_index('ix_support_tickets_project', 'support_tickets', ['project_id'])
    op.create_index('ix_support_tickets_chatbot', 'support_tickets', ['chatbot_id'])
    op.create_index('ix_support_tickets_session', 'support_tickets', ['session_id'])
    op.create_index('ix_support_tickets_status', 'support_tickets', ['status'])
    op.create_index('ix_support_tickets_assigned', 'support_tickets', ['assigned_to_user_id'])
    op.create_index('ix_support_tickets_number', 'support_tickets', ['ticket_number'], unique=True)
    op.create_index('ix_support_tickets_created', 'support_tickets', ['created_at'])

    # Now add the foreign key from chat_sessions to support_tickets
    op.create_foreign_key(
        'fk_chat_sessions_support_ticket',
        'chat_sessions', 'support_tickets',
        ['support_ticket_id'], ['id'],
        ondelete='SET NULL'
    )

    # Create chatbot_agent_routing table
    op.create_table(
        'chatbot_agent_routing',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chatbot_id', sa.Integer(), nullable=False, unique=True),
        sa.Column('routing_mode', sa.String(30), default='bot_then_human', nullable=False),  # human_only, bot_only, bot_then_human, custom
        sa.Column('fallback_to_human', sa.Boolean(), default=True, nullable=False),
        sa.Column('escalation_keywords', ARRAY(sa.String), default=[], nullable=True),  # e.g., ["talk to human", "agent please"]
        sa.Column('confidence_threshold', sa.Float(), default=0.6, nullable=True),  # Bot confidence threshold for escalation
        sa.Column('max_bot_turns', sa.Integer(), default=10, nullable=True),  # Max turns before auto-escalate
        sa.Column('routing_metadata', sa.JSON(), default={}, nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['chatbot_id'], ['chatbots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "routing_mode IN ('human_only', 'bot_only', 'bot_then_human', 'custom')",
            name='valid_routing_mode'
        )
    )

    # Create index for chatbot_agent_routing
    op.create_index('ix_chatbot_agent_routing_chatbot', 'chatbot_agent_routing', ['chatbot_id'], unique=True)

    # Create agent_configs table (for custom routing)
    op.create_table(
        'agent_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('routing_id', sa.Integer(), nullable=False),
        sa.Column('agent_type', sa.String(30), nullable=False),  # llm_chatbot, human_agent
        sa.Column('agent_id', sa.Integer(), nullable=True),  # chatbot_id if agent_type=llm_chatbot, null for human
        sa.Column('agent_name', sa.String(200), nullable=True),  # Display name
        sa.Column('priority_order', sa.Integer(), nullable=False),  # 1, 2, 3...
        sa.Column('max_attempts', sa.Integer(), default=3, nullable=False),  # Max attempts before escalating
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('agent_config', sa.JSON(), default={}, nullable=True),  # Agent-specific config
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['routing_id'], ['chatbot_agent_routing.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['agent_id'], ['chatbots.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "agent_type IN ('llm_chatbot', 'human_agent')",
            name='valid_agent_type'
        )
    )

    # Create indexes for agent_configs
    op.create_index('ix_agent_configs_routing', 'agent_configs', ['routing_id'])
    op.create_index('ix_agent_configs_agent', 'agent_configs', ['agent_id'])
    op.create_index('ix_agent_configs_priority', 'agent_configs', ['routing_id', 'priority_order'])


def downgrade() -> None:
    # Drop agent_configs indexes and table
    op.drop_index('ix_agent_configs_priority', table_name='agent_configs')
    op.drop_index('ix_agent_configs_agent', table_name='agent_configs')
    op.drop_index('ix_agent_configs_routing', table_name='agent_configs')
    op.drop_table('agent_configs')

    # Drop chatbot_agent_routing index and table
    op.drop_index('ix_chatbot_agent_routing_chatbot', table_name='chatbot_agent_routing')
    op.drop_table('chatbot_agent_routing')

    # Drop foreign key from chat_sessions to support_tickets
    op.drop_constraint('fk_chat_sessions_support_ticket', 'chat_sessions', type_='foreignkey')

    # Drop support_tickets indexes and table
    op.drop_index('ix_support_tickets_created', table_name='support_tickets')
    op.drop_index('ix_support_tickets_number', table_name='support_tickets')
    op.drop_index('ix_support_tickets_assigned', table_name='support_tickets')
    op.drop_index('ix_support_tickets_status', table_name='support_tickets')
    op.drop_index('ix_support_tickets_session', table_name='support_tickets')
    op.drop_index('ix_support_tickets_chatbot', table_name='support_tickets')
    op.drop_index('ix_support_tickets_project', table_name='support_tickets')
    op.drop_table('support_tickets')
