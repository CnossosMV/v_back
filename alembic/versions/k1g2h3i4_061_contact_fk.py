"""Add contact_id FK to sessions and tickets

Revision ID: k1g2h3i4_061_contact_fk
Revises: j0f1g2h3_060_asset_mode
Create Date: 2026-03-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'k1g2h3i4_061_contact_fk'
down_revision = 'j0f1g2h3_060_asset_mode'
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
    # Add contact_id to chat_sessions
    if not column_exists('chat_sessions', 'contact_id'):
        op.add_column('chat_sessions', sa.Column(
            'contact_id', sa.Integer(),
            sa.ForeignKey('messaging_users.id', ondelete='SET NULL'),
            nullable=True,
        ))
    if not index_exists('ix_chat_sessions_contact_id', 'chat_sessions'):
        op.create_index('ix_chat_sessions_contact_id', 'chat_sessions', ['contact_id'])

    # Add contact_id to support_tickets
    if not column_exists('support_tickets', 'contact_id'):
        op.add_column('support_tickets', sa.Column(
            'contact_id', sa.Integer(),
            sa.ForeignKey('messaging_users.id', ondelete='SET NULL'),
            nullable=True,
        ))
    if not index_exists('ix_support_tickets_contact_id', 'support_tickets'):
        op.create_index('ix_support_tickets_contact_id', 'support_tickets', ['contact_id'])


def downgrade() -> None:
    op.drop_index('ix_support_tickets_contact_id', table_name='support_tickets')
    op.drop_column('support_tickets', 'contact_id')
    op.drop_index('ix_chat_sessions_contact_id', table_name='chat_sessions')
    op.drop_column('chat_sessions', 'contact_id')
