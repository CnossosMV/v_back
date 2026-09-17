"""003_add_whatsapp_instance_tbls

Revision ID: 7908258cd6fe
Revises: 90089128408a
Create Date: 2025-08-25 02:13:26.839914

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7908258cd6fe'
down_revision = '90089128408a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create whatsapp_instances table
    op.create_table(
        'whatsapp_instances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('instance_name', sa.String(length=100), nullable=False),
        sa.Column('instance_key', sa.String(length=255), nullable=True),
        sa.Column('evolution_instance_id', sa.String(length=100), nullable=True),
        sa.Column('connection_status', sa.String(length=20), nullable=True),
        sa.Column('phone_number', sa.String(length=20), nullable=True),
        sa.Column('qr_code_data', sa.Text(), nullable=True),
        sa.Column('qr_expires_at', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('webhook_url', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('last_connected_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_whatsapp_instances_id'), 'whatsapp_instances', ['id'], unique=False)
    op.create_index(op.f('ix_whatsapp_instances_instance_name'), 'whatsapp_instances', ['instance_name'], unique=True)
    
    # Create foreign key constraints
    op.create_foreign_key(None, 'whatsapp_instances', 'users', ['user_id'], ['id'])
    op.create_foreign_key(None, 'whatsapp_instances', 'workspaces', ['workspace_id'], ['id'])

    # Create whatsapp_messages table
    op.create_table(
        'whatsapp_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('instance_id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.String(length=100), nullable=False),
        sa.Column('from_number', sa.String(length=20), nullable=False),
        sa.Column('to_number', sa.String(length=20), nullable=False),
        sa.Column('message_type', sa.String(length=20), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('media_url', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=True),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_whatsapp_messages_id'), 'whatsapp_messages', ['id'], unique=False)
    op.create_index(op.f('ix_whatsapp_messages_message_id'), 'whatsapp_messages', ['message_id'], unique=False)
    
    # Create foreign key constraint
    op.create_foreign_key(None, 'whatsapp_messages', 'whatsapp_instances', ['instance_id'], ['id'])


def downgrade() -> None:
    # Drop whatsapp_messages table
    op.drop_table('whatsapp_messages')
    
    # Drop whatsapp_instances table
    op.drop_table('whatsapp_instances')