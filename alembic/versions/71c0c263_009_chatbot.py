"""008_add_chatbot_tables

Revision ID: 71c0c263c580
Revises: 325852b0879b
Create Date: 2025-11-17 18:19:27.386008

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '71c0c263c580'
down_revision = '325852b0879b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create chatbots table
    op.create_table(
        'chatbots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('intention', sa.String(length=500), nullable=True),
        sa.Column('model_provider', sa.String(length=50), nullable=False, server_default='openai'),
        sa.Column('model_name', sa.String(length=100), nullable=False, server_default='gpt-4o-mini'),
        sa.Column('temperature', sa.Float(), nullable=True, server_default='0.7'),
        sa.Column('max_tokens', sa.Integer(), nullable=True, server_default='2000'),
        sa.Column('system_prompt', sa.Text(), nullable=True),
        sa.Column('whatsapp_instance_id', sa.Integer(), nullable=True),
        sa.Column('auto_respond_whatsapp', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='active'),
        sa.Column('is_public', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('vector_store_path', sa.String(length=500), nullable=True),
        sa.Column('knowledge_base_path', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['whatsapp_instance_id'], ['whatsapp_instances.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chatbots_id'), 'chatbots', ['id'], unique=False)
    op.create_index(op.f('ix_chatbots_project_id'), 'chatbots', ['project_id'], unique=False)
    op.create_index(op.f('ix_chatbots_status'), 'chatbots', ['status'], unique=False)
    op.create_index(op.f('ix_chatbots_whatsapp_instance_id'), 'chatbots', ['whatsapp_instance_id'], unique=False)

    # Create chat_sessions table
    op.create_table(
        'chat_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chatbot_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('user_identifier', sa.String(length=200), nullable=False),
        sa.Column('channel', sa.String(length=50), nullable=False, server_default='web'),
        sa.Column('whatsapp_message_id', sa.Integer(), nullable=True),
        sa.Column('context_data', sa.JSON(), nullable=True),
        sa.Column('session_metadata', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_interaction_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['chatbot_id'], ['chatbots.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['whatsapp_message_id'], ['whatsapp_messages.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chat_sessions_id'), 'chat_sessions', ['id'], unique=False)
    op.create_index(op.f('ix_chat_sessions_chatbot_id'), 'chat_sessions', ['chatbot_id'], unique=False)
    op.create_index(op.f('ix_chat_sessions_user_id'), 'chat_sessions', ['user_id'], unique=False)
    op.create_index(op.f('ix_chat_sessions_user_identifier'), 'chat_sessions', ['user_identifier'], unique=False)
    op.create_index(op.f('ix_chat_sessions_channel'), 'chat_sessions', ['channel'], unique=False)
    op.create_index(op.f('ix_chat_sessions_is_active'), 'chat_sessions', ['is_active'], unique=False)
    op.create_index(op.f('ix_chat_sessions_started_at'), 'chat_sessions', ['started_at'], unique=False)

    # Create chat_messages table
    op.create_table(
        'chat_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('images', sa.JSON(), nullable=True),
        sa.Column('attachments', sa.JSON(), nullable=True),
        sa.Column('retrieved_documents', sa.JSON(), nullable=True),
        sa.Column('retrieval_metadata', sa.JSON(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('message_metadata', sa.JSON(), nullable=True),
        sa.Column('timestamp', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chat_messages_id'), 'chat_messages', ['id'], unique=False)
    op.create_index(op.f('ix_chat_messages_session_id'), 'chat_messages', ['session_id'], unique=False)
    op.create_index(op.f('ix_chat_messages_role'), 'chat_messages', ['role'], unique=False)
    op.create_index(op.f('ix_chat_messages_timestamp'), 'chat_messages', ['timestamp'], unique=False)

    # Create knowledge_documents table
    op.create_table(
        'knowledge_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chatbot_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(length=50), nullable=False),
        sa.Column('source_url', sa.String(length=1000), nullable=True),
        sa.Column('file_path', sa.String(length=500), nullable=True),
        sa.Column('file_name', sa.String(length=200), nullable=True),
        sa.Column('file_type', sa.String(length=50), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('embedding_model', sa.String(length=100), nullable=True),
        sa.Column('processed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('processing_error', sa.Text(), nullable=True),
        sa.Column('document_metadata', sa.JSON(), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('last_updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['chatbot_id'], ['chatbots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_knowledge_documents_id'), 'knowledge_documents', ['id'], unique=False)
    op.create_index(op.f('ix_knowledge_documents_chatbot_id'), 'knowledge_documents', ['chatbot_id'], unique=False)
    op.create_index(op.f('ix_knowledge_documents_source_type'), 'knowledge_documents', ['source_type'], unique=False)
    op.create_index(op.f('ix_knowledge_documents_content_hash'), 'knowledge_documents', ['content_hash'], unique=False)
    op.create_index(op.f('ix_knowledge_documents_processed'), 'knowledge_documents', ['processed'], unique=False)


def downgrade() -> None:
    # Drop tables in reverse order (due to foreign keys)
    op.drop_index(op.f('ix_knowledge_documents_processed'), table_name='knowledge_documents')
    op.drop_index(op.f('ix_knowledge_documents_content_hash'), table_name='knowledge_documents')
    op.drop_index(op.f('ix_knowledge_documents_source_type'), table_name='knowledge_documents')
    op.drop_index(op.f('ix_knowledge_documents_chatbot_id'), table_name='knowledge_documents')
    op.drop_index(op.f('ix_knowledge_documents_id'), table_name='knowledge_documents')
    op.drop_table('knowledge_documents')

    op.drop_index(op.f('ix_chat_messages_timestamp'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_role'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_session_id'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_id'), table_name='chat_messages')
    op.drop_table('chat_messages')

    op.drop_index(op.f('ix_chat_sessions_started_at'), table_name='chat_sessions')
    op.drop_index(op.f('ix_chat_sessions_is_active'), table_name='chat_sessions')
    op.drop_index(op.f('ix_chat_sessions_channel'), table_name='chat_sessions')
    op.drop_index(op.f('ix_chat_sessions_user_identifier'), table_name='chat_sessions')
    op.drop_index(op.f('ix_chat_sessions_user_id'), table_name='chat_sessions')
    op.drop_index(op.f('ix_chat_sessions_chatbot_id'), table_name='chat_sessions')
    op.drop_index(op.f('ix_chat_sessions_id'), table_name='chat_sessions')
    op.drop_table('chat_sessions')

    op.drop_index(op.f('ix_chatbots_whatsapp_instance_id'), table_name='chatbots')
    op.drop_index(op.f('ix_chatbots_status'), table_name='chatbots')
    op.drop_index(op.f('ix_chatbots_project_id'), table_name='chatbots')
    op.drop_index(op.f('ix_chatbots_id'), table_name='chatbots')
    op.drop_table('chatbots')