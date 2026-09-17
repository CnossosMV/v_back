"""021_add_unified_chat_models_phase1

Revision ID: g4a5b6c7d8e9
Revises: f3a4b5c6d7e8
Create Date: 2026-01-19 10:00:00.000000

Adds unified chat system models - Phase 1:
- MessagingProvider (Twilio + Evolution abstraction)
- Modifications to ChatSession and ChatMessage for human takeover support
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

# revision identifiers, used by Alembic.
revision = 'g4a5b6c7d8e9'
down_revision = 'f3a4b5c6d7e8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create messaging_providers table
    op.create_table(
        'messaging_providers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('chatbot_id', sa.Integer(), nullable=True),
        sa.Column('provider_type', sa.String(50), nullable=False),  # evolution_api, twilio_sms, twilio_whatsapp
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('credentials_encrypted', sa.Text(), nullable=True),  # Fernet encrypted JSON
        sa.Column('phone_number', sa.String(50), nullable=True),
        sa.Column('webhook_url', sa.String(500), nullable=True),
        sa.Column('webhook_secret', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('is_verified', sa.Boolean(), default=False, nullable=False),
        sa.Column('provider_metadata', sa.JSON(), nullable=True),  # Additional provider-specific config
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['chatbot_id'], ['chatbots.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "provider_type IN ('evolution_api', 'twilio_sms', 'twilio_whatsapp')",
            name='valid_provider_type'
        )
    )

    # Create indexes for messaging_providers
    op.create_index('ix_messaging_providers_project', 'messaging_providers', ['project_id'])
    op.create_index('ix_messaging_providers_chatbot', 'messaging_providers', ['chatbot_id'])
    op.create_index('ix_messaging_providers_type', 'messaging_providers', ['provider_type'])
    op.create_index('ix_messaging_providers_active', 'messaging_providers', ['is_active'])

    # Add new columns to chat_sessions for human takeover support
    op.add_column('chat_sessions', sa.Column('support_ticket_id', sa.Integer(), nullable=True))
    op.add_column('chat_sessions', sa.Column('human_takeover', sa.Boolean(), default=False, nullable=False, server_default='false'))
    op.add_column('chat_sessions', sa.Column('human_takeover_at', sa.DateTime(), nullable=True))
    op.add_column('chat_sessions', sa.Column('bot_resumed_at', sa.DateTime(), nullable=True))
    op.add_column('chat_sessions', sa.Column('provider_id', sa.Integer(), nullable=True))
    op.add_column('chat_sessions', sa.Column('external_conversation_id', sa.String(255), nullable=True))

    # Add foreign key for provider_id
    op.create_foreign_key(
        'fk_chat_sessions_provider',
        'chat_sessions', 'messaging_providers',
        ['provider_id'], ['id'],
        ondelete='SET NULL'
    )

    # Create index for external_conversation_id
    op.create_index('ix_chat_sessions_external_conv', 'chat_sessions', ['external_conversation_id'])
    op.create_index('ix_chat_sessions_human_takeover', 'chat_sessions', ['human_takeover'])

    # Add new columns to chat_messages for sender tracking
    op.add_column('chat_messages', sa.Column('sender_type', sa.String(20), nullable=True))  # bot, human_agent, customer
    op.add_column('chat_messages', sa.Column('sent_by_user_id', sa.Integer(), nullable=True))
    op.add_column('chat_messages', sa.Column('external_message_id', sa.String(255), nullable=True))
    op.add_column('chat_messages', sa.Column('delivery_status', sa.String(20), nullable=True))  # pending, sent, delivered, read, failed

    # Add foreign key for sent_by_user_id
    op.create_foreign_key(
        'fk_chat_messages_sent_by_user',
        'chat_messages', 'users',
        ['sent_by_user_id'], ['id'],
        ondelete='SET NULL'
    )

    # Create indexes for chat_messages
    op.create_index('ix_chat_messages_sender_type', 'chat_messages', ['sender_type'])
    op.create_index('ix_chat_messages_external_id', 'chat_messages', ['external_message_id'])

    # Backfill sender_type based on existing role values
    op.execute("""
        UPDATE chat_messages
        SET sender_type = CASE
            WHEN role = 'user' THEN 'customer'
            WHEN role = 'assistant' THEN 'bot'
            WHEN role = 'system' THEN 'bot'
            ELSE 'customer'
        END
        WHERE sender_type IS NULL
    """)


def downgrade() -> None:
    # Drop indexes from chat_messages
    op.drop_index('ix_chat_messages_external_id', table_name='chat_messages')
    op.drop_index('ix_chat_messages_sender_type', table_name='chat_messages')

    # Drop foreign key and columns from chat_messages
    op.drop_constraint('fk_chat_messages_sent_by_user', 'chat_messages', type_='foreignkey')
    op.drop_column('chat_messages', 'delivery_status')
    op.drop_column('chat_messages', 'external_message_id')
    op.drop_column('chat_messages', 'sent_by_user_id')
    op.drop_column('chat_messages', 'sender_type')

    # Drop indexes from chat_sessions
    op.drop_index('ix_chat_sessions_human_takeover', table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_external_conv', table_name='chat_sessions')

    # Drop foreign key and columns from chat_sessions
    op.drop_constraint('fk_chat_sessions_provider', 'chat_sessions', type_='foreignkey')
    op.drop_column('chat_sessions', 'external_conversation_id')
    op.drop_column('chat_sessions', 'provider_id')
    op.drop_column('chat_sessions', 'bot_resumed_at')
    op.drop_column('chat_sessions', 'human_takeover_at')
    op.drop_column('chat_sessions', 'human_takeover')
    op.drop_column('chat_sessions', 'support_ticket_id')

    # Drop messaging_providers table
    op.drop_index('ix_messaging_providers_active', table_name='messaging_providers')
    op.drop_index('ix_messaging_providers_type', table_name='messaging_providers')
    op.drop_index('ix_messaging_providers_chatbot', table_name='messaging_providers')
    op.drop_index('ix_messaging_providers_project', table_name='messaging_providers')
    op.drop_table('messaging_providers')
