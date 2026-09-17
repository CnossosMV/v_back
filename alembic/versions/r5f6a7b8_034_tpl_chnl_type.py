"""Add channel_type and from_email to templates

Revision ID: r5f6a7b8_034_tpl_chnl_type
Revises: q4e5f6a7_033_funnel_eng
Create Date: 2026-02-09 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'r5f6a7b8_034_tpl_chnl_type'
down_revision = 'q4e5f6a7_033_funnel_eng'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    # channeltype enum already exists in DB (used by messaging_channels)
    channel_enum = sa.Enum(
        'email', 'sms', 'whatsapp', 'inapp', 'push', 'webhook',
        name='channeltype', create_type=False
    )

    if not column_exists('messaging_templates', 'channel_type'):
        op.add_column(
            'messaging_templates',
            sa.Column('channel_type', channel_enum, nullable=True)
        )
        # Backfill existing rows to 'email'
        op.execute("UPDATE messaging_templates SET channel_type = 'email' WHERE channel_type IS NULL")

    if not column_exists('messaging_templates', 'from_email'):
        op.add_column(
            'messaging_templates',
            sa.Column('from_email', sa.String(255), nullable=True)
        )

    if not column_exists('messaging_templates', 'from_name'):
        op.add_column(
            'messaging_templates',
            sa.Column('from_name', sa.String(255), nullable=True)
        )

    if not column_exists('messaging_templates', 'reply_to'):
        op.add_column(
            'messaging_templates',
            sa.Column('reply_to', sa.String(255), nullable=True)
        )


def downgrade() -> None:
    if column_exists('messaging_templates', 'reply_to'):
        op.drop_column('messaging_templates', 'reply_to')
    if column_exists('messaging_templates', 'from_name'):
        op.drop_column('messaging_templates', 'from_name')
    if column_exists('messaging_templates', 'from_email'):
        op.drop_column('messaging_templates', 'from_email')
    if column_exists('messaging_templates', 'channel_type'):
        op.drop_column('messaging_templates', 'channel_type')
