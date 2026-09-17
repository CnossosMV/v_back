"""Add channel directionality + email inbound

Revision ID: u1v2w3x4_071_email_inb
Revises: t0u1v2w3_070_proj_ch_cfg
Create Date: 2026-03-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'u1v2w3x4_071_email_inb'
down_revision = 't0u1v2w3_070_proj_ch_cfg'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    # A) Add directionality columns to channel_capabilities
    if not column_exists('channel_capabilities', 'is_inbound_capable'):
        op.add_column('channel_capabilities', sa.Column(
            'is_inbound_capable', sa.Boolean(), nullable=False, server_default=sa.text('false')
        ))
    if not column_exists('channel_capabilities', 'inbound_requires_setup'):
        op.add_column('channel_capabilities', sa.Column(
            'inbound_requires_setup', sa.Boolean(), nullable=False, server_default=sa.text('false')
        ))

    # Seed directionality values
    connection = op.get_bind()
    connection.execute(sa.text("""
        UPDATE channel_capabilities SET is_inbound_capable = true, inbound_requires_setup = false
        WHERE channel IN ('whatsapp', 'sms', 'web')
    """))
    connection.execute(sa.text("""
        UPDATE channel_capabilities SET is_inbound_capable = true, inbound_requires_setup = true
        WHERE channel = 'email'
    """))
    connection.execute(sa.text("""
        UPDATE channel_capabilities SET is_inbound_capable = false, inbound_requires_setup = false
        WHERE channel IN ('inapp', 'push')
    """))

    # B) Add inbound columns to customer_smtp_configs
    if not column_exists('customer_smtp_configs', 'inbound_enabled'):
        op.add_column('customer_smtp_configs', sa.Column(
            'inbound_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')
        ))
    if not column_exists('customer_smtp_configs', 'inbound_provider'):
        op.add_column('customer_smtp_configs', sa.Column(
            'inbound_provider', sa.String(30), nullable=True
        ))
    if not column_exists('customer_smtp_configs', 'inbound_webhook_secret'):
        op.add_column('customer_smtp_configs', sa.Column(
            'inbound_webhook_secret', sa.Text(), nullable=True
        ))
    if not column_exists('customer_smtp_configs', 'mx_record_status'):
        op.add_column('customer_smtp_configs', sa.Column(
            'mx_record_status', sa.String(20), nullable=True
        ))
    if not column_exists('customer_smtp_configs', 'mx_record_verified_at'):
        op.add_column('customer_smtp_configs', sa.Column(
            'mx_record_verified_at', sa.DateTime(), nullable=True
        ))

    # C) Create email_inbound_addresses table
    if not table_exists('email_inbound_addresses'):
        op.create_table(
            'email_inbound_addresses',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('instance_id', sa.Integer(), sa.ForeignKey('customer_smtp_configs.id', ondelete='CASCADE'), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('address', sa.String(255), nullable=False),
            sa.Column('label', sa.String(100), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('default_handler_type', sa.String(50), nullable=True),
            sa.Column('default_handler_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.UniqueConstraint('instance_id', 'address', name='uq_inb_addr_inst_addr'),
        )
        op.create_index('ix_email_inb_addr_project', 'email_inbound_addresses', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_email_inb_addr_project', table_name='email_inbound_addresses')
    op.drop_table('email_inbound_addresses')
    op.drop_column('customer_smtp_configs', 'mx_record_verified_at')
    op.drop_column('customer_smtp_configs', 'mx_record_status')
    op.drop_column('customer_smtp_configs', 'inbound_webhook_secret')
    op.drop_column('customer_smtp_configs', 'inbound_provider')
    op.drop_column('customer_smtp_configs', 'inbound_enabled')
    op.drop_column('channel_capabilities', 'inbound_requires_setup')
    op.drop_column('channel_capabilities', 'is_inbound_capable')
