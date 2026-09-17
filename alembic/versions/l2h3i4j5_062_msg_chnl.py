"""Add channel column to chat_messages

Revision ID: l2h3i4j5_062_msg_chnl
Revises: k1g2h3i4_061_contact_fk
Create Date: 2026-03-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'l2h3i4j5_062_msg_chnl'
down_revision = 'k1g2h3i4_061_contact_fk'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    if not column_exists('chat_messages', 'channel'):
        op.add_column('chat_messages', sa.Column(
            'channel', sa.String(50), nullable=True,
        ))
    if not index_exists('ix_chat_messages_channel', 'chat_messages'):
        op.create_index('ix_chat_messages_channel', 'chat_messages', ['channel'])


def downgrade() -> None:
    op.drop_index('ix_chat_messages_channel', table_name='chat_messages')
    op.drop_column('chat_messages', 'channel')
