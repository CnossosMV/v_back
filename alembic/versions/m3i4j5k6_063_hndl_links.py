"""Handler channel links table

Revision ID: m3i4j5k6_063_hndl_links
Revises: l2h3i4j5_062_msg_chnl
Create Date: 2026-03-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

# revision identifiers, used by Alembic.
revision = 'm3i4j5k6_063_hndl_links'
down_revision = 'l2h3i4j5_062_msg_chnl'
branch_labels = None
depends_on = None


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
    if not table_exists('handler_channel_links'):
        op.create_table(
            'handler_channel_links',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('handler_type', sa.String(50), nullable=False),
            sa.Column('handler_id', sa.Integer(), nullable=False),
            sa.Column('channel', sa.String(50), nullable=False),
            sa.Column('instance_id', sa.Integer(), nullable=True),
            sa.Column('config', sa.JSON(), nullable=True),
            sa.Column('is_primary', sa.Boolean(), server_default='true', nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint('handler_type', 'handler_id', 'channel', name='uq_handler_channel'),
        )
        if not index_exists('ix_handler_channel_links_lookup', 'handler_channel_links'):
            op.create_index(
                'ix_handler_channel_links_lookup',
                'handler_channel_links',
                ['handler_type', 'handler_id'],
            )

    # Data migration: copy existing whatsapp_instance_id from chatbots
    conn = op.get_bind()
    conn.execute(text("""
        INSERT INTO handler_channel_links (handler_type, handler_id, channel, instance_id, is_primary)
        SELECT 'chatbot', c.id, 'whatsapp', c.whatsapp_instance_id, true
        FROM chatbots c
        WHERE c.whatsapp_instance_id IS NOT NULL
        ON CONFLICT (handler_type, handler_id, channel) DO NOTHING
    """))

    # Data migration: copy existing whatsapp_instance_id from agent_teams
    conn.execute(text("""
        INSERT INTO handler_channel_links (handler_type, handler_id, channel, instance_id, is_primary)
        SELECT 'agent_team', t.id, 'whatsapp', t.whatsapp_instance_id, true
        FROM agent_teams t
        WHERE t.whatsapp_instance_id IS NOT NULL
        ON CONFLICT (handler_type, handler_id, channel) DO NOTHING
    """))


def downgrade() -> None:
    op.drop_index('ix_handler_channel_links_lookup', table_name='handler_channel_links')
    op.drop_table('handler_channel_links')
