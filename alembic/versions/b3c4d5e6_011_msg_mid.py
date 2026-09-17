"""010_add_messaging_middleware

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-01-17 00:00:00.000000

Adds Messaging Middleware tables:
- messaging_domains: Verified domains for frontend SDK auth
- messaging_api_keys: Secret keys for backend auth
- messaging_channels: Delivery channels with webhook config
- messaging_templates: Message templates with variables
- messaging_users: Identified contacts
- messaging_events: Recorded events
- messaging_logs: Message delivery log
- messaging_event_locks: Deduplication
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'b3c4d5e6f7a8'
down_revision = 'a2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types using raw SQL with IF NOT EXISTS for reliability
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE channeltype AS ENUM ('email', 'sms', 'whatsapp', 'inapp', 'push', 'webhook');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE authtype AS ENUM ('none', 'bearer', 'basic', 'api_key', 'custom_header');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE messagestatus AS ENUM ('pending', 'queued', 'sent', 'delivered', 'failed', 'bounced');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Define enum column types with create_type=False
    channel_type = postgresql.ENUM(
        'email', 'sms', 'whatsapp', 'inapp', 'push', 'webhook',
        name='channeltype',
        create_type=False
    )
    auth_type = postgresql.ENUM(
        'none', 'bearer', 'basic', 'api_key', 'custom_header',
        name='authtype',
        create_type=False
    )
    message_status = postgresql.ENUM(
        'pending', 'queued', 'sent', 'delivered', 'failed', 'bounced',
        name='messagestatus',
        create_type=False
    )

    # Create messaging_domains table
    op.create_table(
        'messaging_domains',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=False),
        sa.Column('write_key', sa.String(length=100), nullable=False),
        sa.Column('is_verified', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('verification_token', sa.String(length=100), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('allowed_origins', postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messaging_domains_id'), 'messaging_domains', ['id'], unique=False)
    op.create_index(op.f('ix_messaging_domains_project_id'), 'messaging_domains', ['project_id'], unique=False)
    op.create_index(op.f('ix_messaging_domains_domain'), 'messaging_domains', ['domain'], unique=False)
    op.create_index(op.f('ix_messaging_domains_write_key'), 'messaging_domains', ['write_key'], unique=True)

    # Create messaging_api_keys table
    op.create_table(
        'messaging_api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('secret_key_hash', sa.String(length=255), nullable=False),
        sa.Column('key_prefix', sa.String(length=20), nullable=False),
        sa.Column('permissions', postgresql.ARRAY(sa.String()), server_default='{"send","track","identify"}'),
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=False, server_default='1000'),
        sa.Column('rate_limit_per_day', sa.Integer(), nullable=False, server_default='100000'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messaging_api_keys_id'), 'messaging_api_keys', ['id'], unique=False)
    op.create_index(op.f('ix_messaging_api_keys_project_id'), 'messaging_api_keys', ['project_id'], unique=False)

    # Create messaging_channels table
    op.create_table(
        'messaging_channels',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=100), nullable=False),
        sa.Column('channel_type', postgresql.ENUM('email', 'sms', 'whatsapp', 'inapp', 'push', 'webhook', name='channeltype', create_type=False), nullable=False),
        sa.Column('webhook_url', sa.String(length=500), nullable=False),
        sa.Column('auth_type', postgresql.ENUM('none', 'bearer', 'basic', 'api_key', 'custom_header', name='authtype', create_type=False), nullable=False, server_default='none'),
        sa.Column('auth_config', sa.JSON(), nullable=True),
        sa.Column('headers', sa.JSON(), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('max_retries', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('retry_delay_seconds', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messaging_channels_id'), 'messaging_channels', ['id'], unique=False)
    op.create_index(op.f('ix_messaging_channels_project_id'), 'messaging_channels', ['project_id'], unique=False)
    op.create_index(op.f('ix_messaging_channels_slug'), 'messaging_channels', ['slug'], unique=False)
    op.create_index('ix_messaging_channels_project_slug', 'messaging_channels', ['project_id', 'slug'], unique=True)

    # Create messaging_templates table
    op.create_table(
        'messaging_templates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('channel_id', sa.Integer(), nullable=True),
        sa.Column('slug', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=500), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('trigger_events', postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['channel_id'], ['messaging_channels.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messaging_templates_id'), 'messaging_templates', ['id'], unique=False)
    op.create_index(op.f('ix_messaging_templates_project_id'), 'messaging_templates', ['project_id'], unique=False)
    op.create_index(op.f('ix_messaging_templates_channel_id'), 'messaging_templates', ['channel_id'], unique=False)
    op.create_index(op.f('ix_messaging_templates_slug'), 'messaging_templates', ['slug'], unique=False)
    op.create_index('ix_messaging_templates_project_slug', 'messaging_templates', ['project_id', 'slug'], unique=True)

    # Create messaging_users table
    op.create_table(
        'messaging_users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('external_id', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('phone', sa.String(length=50), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('properties', sa.JSON(), nullable=True),
        sa.Column('is_subscribed', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('first_seen_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messaging_users_id'), 'messaging_users', ['id'], unique=False)
    op.create_index(op.f('ix_messaging_users_project_id'), 'messaging_users', ['project_id'], unique=False)
    op.create_index(op.f('ix_messaging_users_external_id'), 'messaging_users', ['external_id'], unique=False)
    op.create_index(op.f('ix_messaging_users_email'), 'messaging_users', ['email'], unique=False)
    op.create_index('ix_messaging_users_project_external', 'messaging_users', ['project_id', 'external_id'], unique=True)

    # Create messaging_events table
    op.create_table(
        'messaging_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('anonymous_id', sa.String(length=100), nullable=True),
        sa.Column('event_name', sa.String(length=255), nullable=False),
        sa.Column('properties', sa.JSON(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='frontend'),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('processed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messaging_events_id'), 'messaging_events', ['id'], unique=False)
    op.create_index(op.f('ix_messaging_events_project_id'), 'messaging_events', ['project_id'], unique=False)
    op.create_index(op.f('ix_messaging_events_user_id'), 'messaging_events', ['user_id'], unique=False)
    op.create_index(op.f('ix_messaging_events_anonymous_id'), 'messaging_events', ['anonymous_id'], unique=False)
    op.create_index(op.f('ix_messaging_events_event_name'), 'messaging_events', ['event_name'], unique=False)
    op.create_index(op.f('ix_messaging_events_processed'), 'messaging_events', ['processed'], unique=False)
    op.create_index(op.f('ix_messaging_events_created_at'), 'messaging_events', ['created_at'], unique=False)

    # Create messaging_logs table
    op.create_table(
        'messaging_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('template_id', sa.Integer(), nullable=True),
        sa.Column('channel_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('template_slug', sa.String(length=100), nullable=True),
        sa.Column('channel_type', sa.String(length=50), nullable=True),
        sa.Column('recipient', sa.String(length=255), nullable=False),
        sa.Column('rendered_subject', sa.String(length=500), nullable=True),
        sa.Column('rendered_body', sa.Text(), nullable=True),
        sa.Column('status', postgresql.ENUM('pending', 'queued', 'sent', 'delivered', 'failed', 'bounced', name='messagestatus', create_type=False), nullable=False, server_default='pending'),
        sa.Column('provider_message_id', sa.String(length=255), nullable=True),
        sa.Column('provider_response', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('next_retry_at', sa.DateTime(), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.Column('failed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['template_id'], ['messaging_templates.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['channel_id'], ['messaging_channels.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['event_id'], ['messaging_events.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messaging_logs_id'), 'messaging_logs', ['id'], unique=False)
    op.create_index(op.f('ix_messaging_logs_project_id'), 'messaging_logs', ['project_id'], unique=False)
    op.create_index(op.f('ix_messaging_logs_template_id'), 'messaging_logs', ['template_id'], unique=False)
    op.create_index(op.f('ix_messaging_logs_channel_id'), 'messaging_logs', ['channel_id'], unique=False)
    op.create_index(op.f('ix_messaging_logs_user_id'), 'messaging_logs', ['user_id'], unique=False)
    op.create_index(op.f('ix_messaging_logs_event_id'), 'messaging_logs', ['event_id'], unique=False)
    op.create_index(op.f('ix_messaging_logs_status'), 'messaging_logs', ['status'], unique=False)
    op.create_index(op.f('ix_messaging_logs_next_retry_at'), 'messaging_logs', ['next_retry_at'], unique=False)
    op.create_index(op.f('ix_messaging_logs_created_at'), 'messaging_logs', ['created_at'], unique=False)

    # Create messaging_event_locks table
    op.create_table(
        'messaging_event_locks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('event_name', sa.String(length=255), nullable=False),
        sa.Column('template_id', sa.Integer(), nullable=False),
        sa.Column('triggered_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['template_id'], ['messaging_templates.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messaging_event_locks_id'), 'messaging_event_locks', ['id'], unique=False)
    op.create_index('ix_event_lock_unique', 'messaging_event_locks', ['project_id', 'user_id', 'event_name', 'template_id'], unique=True)


def downgrade() -> None:
    # Drop tables in reverse order (due to foreign key constraints)
    op.drop_index('ix_event_lock_unique', table_name='messaging_event_locks')
    op.drop_index(op.f('ix_messaging_event_locks_id'), table_name='messaging_event_locks')
    op.drop_table('messaging_event_locks')

    op.drop_index(op.f('ix_messaging_logs_created_at'), table_name='messaging_logs')
    op.drop_index(op.f('ix_messaging_logs_next_retry_at'), table_name='messaging_logs')
    op.drop_index(op.f('ix_messaging_logs_status'), table_name='messaging_logs')
    op.drop_index(op.f('ix_messaging_logs_event_id'), table_name='messaging_logs')
    op.drop_index(op.f('ix_messaging_logs_user_id'), table_name='messaging_logs')
    op.drop_index(op.f('ix_messaging_logs_channel_id'), table_name='messaging_logs')
    op.drop_index(op.f('ix_messaging_logs_template_id'), table_name='messaging_logs')
    op.drop_index(op.f('ix_messaging_logs_project_id'), table_name='messaging_logs')
    op.drop_index(op.f('ix_messaging_logs_id'), table_name='messaging_logs')
    op.drop_table('messaging_logs')

    op.drop_index(op.f('ix_messaging_events_created_at'), table_name='messaging_events')
    op.drop_index(op.f('ix_messaging_events_processed'), table_name='messaging_events')
    op.drop_index(op.f('ix_messaging_events_event_name'), table_name='messaging_events')
    op.drop_index(op.f('ix_messaging_events_anonymous_id'), table_name='messaging_events')
    op.drop_index(op.f('ix_messaging_events_user_id'), table_name='messaging_events')
    op.drop_index(op.f('ix_messaging_events_project_id'), table_name='messaging_events')
    op.drop_index(op.f('ix_messaging_events_id'), table_name='messaging_events')
    op.drop_table('messaging_events')

    op.drop_index('ix_messaging_users_project_external', table_name='messaging_users')
    op.drop_index(op.f('ix_messaging_users_email'), table_name='messaging_users')
    op.drop_index(op.f('ix_messaging_users_external_id'), table_name='messaging_users')
    op.drop_index(op.f('ix_messaging_users_project_id'), table_name='messaging_users')
    op.drop_index(op.f('ix_messaging_users_id'), table_name='messaging_users')
    op.drop_table('messaging_users')

    op.drop_index('ix_messaging_templates_project_slug', table_name='messaging_templates')
    op.drop_index(op.f('ix_messaging_templates_slug'), table_name='messaging_templates')
    op.drop_index(op.f('ix_messaging_templates_channel_id'), table_name='messaging_templates')
    op.drop_index(op.f('ix_messaging_templates_project_id'), table_name='messaging_templates')
    op.drop_index(op.f('ix_messaging_templates_id'), table_name='messaging_templates')
    op.drop_table('messaging_templates')

    op.drop_index('ix_messaging_channels_project_slug', table_name='messaging_channels')
    op.drop_index(op.f('ix_messaging_channels_slug'), table_name='messaging_channels')
    op.drop_index(op.f('ix_messaging_channels_project_id'), table_name='messaging_channels')
    op.drop_index(op.f('ix_messaging_channels_id'), table_name='messaging_channels')
    op.drop_table('messaging_channels')

    op.drop_index(op.f('ix_messaging_api_keys_project_id'), table_name='messaging_api_keys')
    op.drop_index(op.f('ix_messaging_api_keys_id'), table_name='messaging_api_keys')
    op.drop_table('messaging_api_keys')

    op.drop_index(op.f('ix_messaging_domains_write_key'), table_name='messaging_domains')
    op.drop_index(op.f('ix_messaging_domains_domain'), table_name='messaging_domains')
    op.drop_index(op.f('ix_messaging_domains_project_id'), table_name='messaging_domains')
    op.drop_index(op.f('ix_messaging_domains_id'), table_name='messaging_domains')
    op.drop_table('messaging_domains')

    # Drop enums
    op.execute('DROP TYPE IF EXISTS messagestatus')
    op.execute('DROP TYPE IF EXISTS authtype')
    op.execute('DROP TYPE IF EXISTS channeltype')
