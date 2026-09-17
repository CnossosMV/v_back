"""023_add_chat_widget_config

Revision ID: i6c7d8e9f0g1
Revises: h5b6c7d8e9f0
Create Date: 2026-01-19 10:10:00.000000

Adds unified chat system models - Phase 3:
- ChatWidgetConfig (Embeddable Widget Configuration)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

# revision identifiers, used by Alembic.
revision = 'i6c7d8e9f0g1'
down_revision = 'h5b6c7d8e9f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create chat_widget_configs table
    op.create_table(
        'chat_widget_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chatbot_id', sa.Integer(), nullable=False, unique=True),
        sa.Column('widget_key', sa.String(100), nullable=False, unique=True),  # Public embed key (wk_xxx)

        # Theme settings
        sa.Column('theme', sa.JSON(), default={}, nullable=True),  # {primary_color, secondary_color, position, size, etc.}

        # Welcome and branding
        sa.Column('welcome_message', sa.String(500), nullable=True),
        sa.Column('placeholder_text', sa.String(200), nullable=True),
        sa.Column('bot_name', sa.String(100), nullable=True),
        sa.Column('bot_avatar_url', sa.String(500), nullable=True),

        # Feature toggles
        sa.Column('enable_webchat', sa.Boolean(), default=True, nullable=False),
        sa.Column('enable_whatsapp_button', sa.Boolean(), default=False, nullable=False),
        sa.Column('whatsapp_number', sa.String(50), nullable=True),
        sa.Column('enable_file_upload', sa.Boolean(), default=False, nullable=False),
        sa.Column('enable_feedback', sa.Boolean(), default=True, nullable=False),
        sa.Column('enable_sound', sa.Boolean(), default=True, nullable=False),
        sa.Column('enable_typing_indicator', sa.Boolean(), default=True, nullable=False),

        # Security
        sa.Column('allowed_domains', ARRAY(sa.String), default=[], nullable=True),  # Domain whitelist
        sa.Column('rate_limit_per_minute', sa.Integer(), default=20, nullable=False),
        sa.Column('require_email', sa.Boolean(), default=False, nullable=False),

        # Advanced settings
        sa.Column('auto_open_delay', sa.Integer(), nullable=True),  # Milliseconds before auto-opening
        sa.Column('greeting_delay', sa.Integer(), default=1000, nullable=True),  # Delay before showing greeting
        sa.Column('offline_message', sa.String(500), nullable=True),
        sa.Column('custom_css', sa.Text(), nullable=True),
        sa.Column('custom_launcher_icon', sa.String(500), nullable=True),

        # Analytics
        sa.Column('track_page_views', sa.Boolean(), default=False, nullable=False),

        # Status
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),

        # Timestamps
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),

        sa.ForeignKeyConstraint(['chatbot_id'], ['chatbots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes for chat_widget_configs
    op.create_index('ix_chat_widget_configs_chatbot', 'chat_widget_configs', ['chatbot_id'], unique=True)
    op.create_index('ix_chat_widget_configs_key', 'chat_widget_configs', ['widget_key'], unique=True)
    op.create_index('ix_chat_widget_configs_active', 'chat_widget_configs', ['is_active'])


def downgrade() -> None:
    # Drop indexes and table
    op.drop_index('ix_chat_widget_configs_active', table_name='chat_widget_configs')
    op.drop_index('ix_chat_widget_configs_key', table_name='chat_widget_configs')
    op.drop_index('ix_chat_widget_configs_chatbot', table_name='chat_widget_configs')
    op.drop_table('chat_widget_configs')
