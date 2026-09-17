"""Add Meta Cloud API support + contact windows

Revision ID: t7b8c9d0_036_meta_cloud
Revises: s6a7b8c9_035_evt_idx_jb
Create Date: 2026-02-11 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 't7b8c9d0_036_meta_cloud'
down_revision = 's6a7b8c9_035_evt_idx_jb'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    # --- Add columns to whatsapp_instances ---
    if not column_exists('whatsapp_instances', 'provider_type'):
        op.add_column('whatsapp_instances',
            sa.Column('provider_type', sa.String(30), server_default='evolution_api', nullable=False))

    if not column_exists('whatsapp_instances', 'meta_phone_number_id'):
        op.add_column('whatsapp_instances',
            sa.Column('meta_phone_number_id', sa.String(100), nullable=True))

    if not column_exists('whatsapp_instances', 'meta_waba_id'):
        op.add_column('whatsapp_instances',
            sa.Column('meta_waba_id', sa.String(100), nullable=True))

    if not column_exists('whatsapp_instances', 'meta_access_token_enc'):
        op.add_column('whatsapp_instances',
            sa.Column('meta_access_token_enc', sa.Text, nullable=True))

    if not column_exists('whatsapp_instances', 'meta_app_secret_enc'):
        op.add_column('whatsapp_instances',
            sa.Column('meta_app_secret_enc', sa.Text, nullable=True))

    if not column_exists('whatsapp_instances', 'meta_webhook_verify_token'):
        op.add_column('whatsapp_instances',
            sa.Column('meta_webhook_verify_token', sa.String(255), nullable=True))

    if not column_exists('whatsapp_instances', 'meta_business_name'):
        op.add_column('whatsapp_instances',
            sa.Column('meta_business_name', sa.String(255), nullable=True))

    # Backfill existing rows
    op.execute("UPDATE whatsapp_instances SET provider_type = 'evolution_api' WHERE provider_type IS NULL")

    # --- Create whatsapp_contact_windows table ---
    if not table_exists('whatsapp_contact_windows'):
        op.create_table(
            'whatsapp_contact_windows',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('instance_id', sa.Integer(),
                       sa.ForeignKey('whatsapp_instances.id', ondelete='CASCADE'), nullable=False),
            sa.Column('contact_phone', sa.String(50), nullable=False),
            sa.Column('window_opens_at', sa.DateTime(), nullable=False),
            sa.Column('window_expires_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )

        if not index_exists('uq_instance_contact', 'whatsapp_contact_windows'):
            op.create_index('uq_instance_contact', 'whatsapp_contact_windows',
                            ['instance_id', 'contact_phone'], unique=True)

        if not index_exists('ix_window_expires', 'whatsapp_contact_windows'):
            op.create_index('ix_window_expires', 'whatsapp_contact_windows',
                            ['window_expires_at'])


def downgrade() -> None:
    op.drop_table('whatsapp_contact_windows')
    op.drop_column('whatsapp_instances', 'meta_business_name')
    op.drop_column('whatsapp_instances', 'meta_webhook_verify_token')
    op.drop_column('whatsapp_instances', 'meta_app_secret_enc')
    op.drop_column('whatsapp_instances', 'meta_access_token_enc')
    op.drop_column('whatsapp_instances', 'meta_waba_id')
    op.drop_column('whatsapp_instances', 'meta_phone_number_id')
    op.drop_column('whatsapp_instances', 'provider_type')
