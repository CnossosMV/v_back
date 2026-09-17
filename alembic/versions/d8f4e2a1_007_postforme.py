"""005_add_postforme_tables

Revision ID: d8f4e2a1b9c0
Revises: a98586d0d00c
Create Date: 2025-10-31 14:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'd8f4e2a1b9c0'
down_revision = 'a98586d0d00c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create postforme_credentials table
    op.create_table('postforme_credentials',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('api_key_encrypted', sa.Text(), nullable=False),
        sa.Column('postforme_user_id', sa.String(length=255), nullable=True),
        sa.Column('account_email', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('last_validated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id')
    )
    op.create_index(op.f('ix_postforme_credentials_id'), 'postforme_credentials', ['id'], unique=False)

    # Create postforme_social_accounts table
    op.create_table('postforme_social_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('credential_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('postforme_account_id', sa.String(length=255), nullable=False),
        sa.Column('platform', sa.String(length=50), nullable=False),
        sa.Column('account_name', sa.String(length=255), nullable=True),
        sa.Column('account_username', sa.String(length=255), nullable=True),
        sa.Column('account_profile_url', sa.Text(), nullable=True),
        sa.Column('is_connected', sa.Boolean(), nullable=False),
        sa.Column('last_sync_at', sa.DateTime(), nullable=True),
        sa.Column('connection_status', sa.String(length=50), nullable=False),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("platform IN ('facebook', 'instagram', 'twitter', 'tiktok', 'youtube', 'pinterest', 'linkedin', 'bluesky', 'threads')", name='valid_platform'),
        sa.ForeignKeyConstraint(['credential_id'], ['postforme_credentials.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('postforme_account_id')
    )
    op.create_index(op.f('ix_postforme_social_accounts_credential_id'), 'postforme_social_accounts', ['credential_id'], unique=False)
    op.create_index(op.f('ix_postforme_social_accounts_id'), 'postforme_social_accounts', ['id'], unique=False)
    op.create_index(op.f('ix_postforme_social_accounts_platform'), 'postforme_social_accounts', ['platform'], unique=False)
    op.create_index(op.f('ix_postforme_social_accounts_postforme_account_id'), 'postforme_social_accounts', ['postforme_account_id'], unique=False)
    op.create_index(op.f('ix_postforme_social_accounts_project_id'), 'postforme_social_accounts', ['project_id'], unique=False)

    # Create postforme_posts table
    op.create_table('postforme_posts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('postforme_post_id', sa.String(length=255), nullable=True),
        sa.Column('content', sa.JSON(), nullable=False),
        sa.Column('target_account_ids', postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('scheduled_time', sa.DateTime(), nullable=True),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('external_id', sa.String(length=255), nullable=True),
        sa.Column('tags', postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('draft', 'scheduled', 'publishing', 'published', 'failed', 'cancelled')", name='valid_post_status'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('postforme_post_id')
    )
    op.create_index(op.f('ix_postforme_posts_created_by'), 'postforme_posts', ['created_by'], unique=False)
    op.create_index(op.f('ix_postforme_posts_external_id'), 'postforme_posts', ['external_id'], unique=False)
    op.create_index(op.f('ix_postforme_posts_id'), 'postforme_posts', ['id'], unique=False)
    op.create_index(op.f('ix_postforme_posts_postforme_post_id'), 'postforme_posts', ['postforme_post_id'], unique=False)
    op.create_index(op.f('ix_postforme_posts_project_id'), 'postforme_posts', ['project_id'], unique=False)
    op.create_index(op.f('ix_postforme_posts_scheduled_time'), 'postforme_posts', ['scheduled_time'], unique=False)
    op.create_index(op.f('ix_postforme_posts_status'), 'postforme_posts', ['status'], unique=False)

    # Create postforme_post_results table
    op.create_table('postforme_post_results',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('postforme_result_id', sa.String(length=255), nullable=True),
        sa.Column('social_account_id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('platform_post_id', sa.String(length=255), nullable=True),
        sa.Column('platform_post_url', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('error_code', sa.String(length=50), nullable=True),
        sa.Column('platform_response', sa.JSON(), nullable=True),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'published', 'failed')", name='valid_result_status'),
        sa.ForeignKeyConstraint(['post_id'], ['postforme_posts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['social_account_id'], ['postforme_social_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('postforme_result_id')
    )
    op.create_index(op.f('ix_postforme_post_results_id'), 'postforme_post_results', ['id'], unique=False)
    op.create_index(op.f('ix_postforme_post_results_post_id'), 'postforme_post_results', ['post_id'], unique=False)
    op.create_index(op.f('ix_postforme_post_results_postforme_result_id'), 'postforme_post_results', ['postforme_result_id'], unique=False)
    op.create_index(op.f('ix_postforme_post_results_social_account_id'), 'postforme_post_results', ['social_account_id'], unique=False)
    op.create_index(op.f('ix_postforme_post_results_status'), 'postforme_post_results', ['status'], unique=False)

    # Create postforme_media table
    op.create_table('postforme_media',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('postforme_media_id', sa.String(length=255), nullable=True),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('file_type', sa.String(length=50), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('mime_type', sa.String(length=100), nullable=True),
        sa.Column('upload_url', sa.Text(), nullable=True),
        sa.Column('permanent_url', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('upload_expires_at', sa.DateTime(), nullable=True),
        sa.Column('used_in_posts', postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('duration', sa.Integer(), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('uploaded_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'uploaded', 'processing', 'ready', 'failed')", name='valid_media_status'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('postforme_media_id')
    )
    op.create_index(op.f('ix_postforme_media_id'), 'postforme_media', ['id'], unique=False)
    op.create_index(op.f('ix_postforme_media_postforme_media_id'), 'postforme_media', ['postforme_media_id'], unique=False)
    op.create_index(op.f('ix_postforme_media_project_id'), 'postforme_media', ['project_id'], unique=False)
    op.create_index(op.f('ix_postforme_media_status'), 'postforme_media', ['status'], unique=False)
    op.create_index(op.f('ix_postforme_media_uploaded_by'), 'postforme_media', ['uploaded_by'], unique=False)

    # Create postforme_webhooks table
    op.create_table('postforme_webhooks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('credential_id', sa.Integer(), nullable=False),
        sa.Column('postforme_webhook_id', sa.String(length=255), nullable=True),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('events', postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column('secret', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('last_triggered_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['credential_id'], ['postforme_credentials.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('postforme_webhook_id')
    )
    op.create_index(op.f('ix_postforme_webhooks_id'), 'postforme_webhooks', ['id'], unique=False)
    op.create_index(op.f('ix_postforme_webhooks_is_active'), 'postforme_webhooks', ['is_active'], unique=False)
    op.create_index(op.f('ix_postforme_webhooks_postforme_webhook_id'), 'postforme_webhooks', ['postforme_webhook_id'], unique=False)
    op.create_index(op.f('ix_postforme_webhooks_project_id'), 'postforme_webhooks', ['project_id'], unique=False)

    # Create postforme_webhook_events table
    op.create_table('postforme_webhook_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('webhook_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('processed', sa.Boolean(), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('processing_error', sa.Text(), nullable=True),
        sa.Column('received_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['webhook_id'], ['postforme_webhooks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_postforme_webhook_events_event_type'), 'postforme_webhook_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_postforme_webhook_events_id'), 'postforme_webhook_events', ['id'], unique=False)
    op.create_index(op.f('ix_postforme_webhook_events_processed'), 'postforme_webhook_events', ['processed'], unique=False)
    op.create_index(op.f('ix_postforme_webhook_events_project_id'), 'postforme_webhook_events', ['project_id'], unique=False)
    op.create_index(op.f('ix_postforme_webhook_events_received_at'), 'postforme_webhook_events', ['received_at'], unique=False)
    op.create_index(op.f('ix_postforme_webhook_events_webhook_id'), 'postforme_webhook_events', ['webhook_id'], unique=False)


def downgrade() -> None:
    # Drop tables in reverse order
    op.drop_index(op.f('ix_postforme_webhook_events_webhook_id'), table_name='postforme_webhook_events')
    op.drop_index(op.f('ix_postforme_webhook_events_received_at'), table_name='postforme_webhook_events')
    op.drop_index(op.f('ix_postforme_webhook_events_project_id'), table_name='postforme_webhook_events')
    op.drop_index(op.f('ix_postforme_webhook_events_processed'), table_name='postforme_webhook_events')
    op.drop_index(op.f('ix_postforme_webhook_events_id'), table_name='postforme_webhook_events')
    op.drop_index(op.f('ix_postforme_webhook_events_event_type'), table_name='postforme_webhook_events')
    op.drop_table('postforme_webhook_events')

    op.drop_index(op.f('ix_postforme_webhooks_project_id'), table_name='postforme_webhooks')
    op.drop_index(op.f('ix_postforme_webhooks_postforme_webhook_id'), table_name='postforme_webhooks')
    op.drop_index(op.f('ix_postforme_webhooks_is_active'), table_name='postforme_webhooks')
    op.drop_index(op.f('ix_postforme_webhooks_id'), table_name='postforme_webhooks')
    op.drop_table('postforme_webhooks')

    op.drop_index(op.f('ix_postforme_media_uploaded_by'), table_name='postforme_media')
    op.drop_index(op.f('ix_postforme_media_status'), table_name='postforme_media')
    op.drop_index(op.f('ix_postforme_media_project_id'), table_name='postforme_media')
    op.drop_index(op.f('ix_postforme_media_postforme_media_id'), table_name='postforme_media')
    op.drop_index(op.f('ix_postforme_media_id'), table_name='postforme_media')
    op.drop_table('postforme_media')

    op.drop_index(op.f('ix_postforme_post_results_status'), table_name='postforme_post_results')
    op.drop_index(op.f('ix_postforme_post_results_social_account_id'), table_name='postforme_post_results')
    op.drop_index(op.f('ix_postforme_post_results_postforme_result_id'), table_name='postforme_post_results')
    op.drop_index(op.f('ix_postforme_post_results_post_id'), table_name='postforme_post_results')
    op.drop_index(op.f('ix_postforme_post_results_id'), table_name='postforme_post_results')
    op.drop_table('postforme_post_results')

    op.drop_index(op.f('ix_postforme_posts_status'), table_name='postforme_posts')
    op.drop_index(op.f('ix_postforme_posts_scheduled_time'), table_name='postforme_posts')
    op.drop_index(op.f('ix_postforme_posts_project_id'), table_name='postforme_posts')
    op.drop_index(op.f('ix_postforme_posts_postforme_post_id'), table_name='postforme_posts')
    op.drop_index(op.f('ix_postforme_posts_id'), table_name='postforme_posts')
    op.drop_index(op.f('ix_postforme_posts_external_id'), table_name='postforme_posts')
    op.drop_index(op.f('ix_postforme_posts_created_by'), table_name='postforme_posts')
    op.drop_table('postforme_posts')

    op.drop_index(op.f('ix_postforme_social_accounts_project_id'), table_name='postforme_social_accounts')
    op.drop_index(op.f('ix_postforme_social_accounts_postforme_account_id'), table_name='postforme_social_accounts')
    op.drop_index(op.f('ix_postforme_social_accounts_platform'), table_name='postforme_social_accounts')
    op.drop_index(op.f('ix_postforme_social_accounts_id'), table_name='postforme_social_accounts')
    op.drop_index(op.f('ix_postforme_social_accounts_credential_id'), table_name='postforme_social_accounts')
    op.drop_table('postforme_social_accounts')

    op.drop_index(op.f('ix_postforme_credentials_id'), table_name='postforme_credentials')
    op.drop_table('postforme_credentials')
