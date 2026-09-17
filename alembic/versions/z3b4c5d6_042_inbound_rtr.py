"""042 inbound message router

Revision ID: z3b4c5d6_042_inbound_rtr
Revises: y2a3b4c5_041_seg_rules
Create Date: 2026-02-17 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'z3b4c5d6_042_inbound_rtr'
down_revision = 'y2a3b4c5_041_seg_rules'
branch_labels = None
depends_on = None


def table_exists(bind, table_name):
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def column_exists(bind, table_name, column_name):
    insp = sa.inspect(bind)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def index_exists(bind, index_name):
    insp = sa.inspect(bind)
    all_indexes = []
    for tbl in insp.get_table_names():
        for idx in insp.get_indexes(tbl):
            all_indexes.append(idx['name'])
    return index_name in all_indexes


def upgrade() -> None:
    bind = op.get_bind()

    # ── contact_routing_states ──────────────────────────────────────────
    if not table_exists(bind, 'contact_routing_states'):
        op.create_table(
            'contact_routing_states',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('contact_identifier', sa.String(255), nullable=False),
            sa.Column('channel', sa.String(50), nullable=False),
            sa.Column('handler_type', sa.String(50), nullable=False, server_default='idle'),
            sa.Column('handler_id', sa.Integer(), nullable=True),
            sa.Column('handler_priority', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('session_id', sa.Integer(), nullable=True),
            sa.Column('assigned_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('metadata', sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='SET NULL'),
            sa.UniqueConstraint('project_id', 'contact_identifier', 'channel', name='uq_routing_state_contact'),
        )
        op.create_index('ix_routing_state_project_handler', 'contact_routing_states', ['project_id', 'handler_type'])
        op.create_index('ix_contact_routing_states_id', 'contact_routing_states', ['id'])

    # ── inbox_assignment_rules ──────────────────────────────────────────
    if not table_exists(bind, 'inbox_assignment_rules'):
        op.create_table(
            'inbox_assignment_rules',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('conditions', sa.JSON(), nullable=True),
            sa.Column('match_mode', sa.String(10), nullable=False, server_default='all'),
            sa.Column('destination_type', sa.String(50), nullable=False),
            sa.Column('destination_id', sa.Integer(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_inbox_rules_project_prio', 'inbox_assignment_rules', ['project_id', 'priority'])
        op.create_index('ix_inbox_assignment_rules_id', 'inbox_assignment_rules', ['id'])

    # ── Add columns to chat_messages ────────────────────────────────────
    if table_exists(bind, 'chat_messages'):
        if not column_exists(bind, 'chat_messages', 'content_pieces'):
            op.add_column('chat_messages', sa.Column('content_pieces', sa.JSON(), nullable=True))
        if not column_exists(bind, 'chat_messages', 'resolved_text'):
            op.add_column('chat_messages', sa.Column('resolved_text', sa.Text(), nullable=True))

    # ── Add columns to chat_sessions ────────────────────────────────────
    if table_exists(bind, 'chat_sessions'):
        if not column_exists(bind, 'chat_sessions', 'handler_type'):
            op.add_column('chat_sessions', sa.Column('handler_type', sa.String(50), nullable=True))
        if not column_exists(bind, 'chat_sessions', 'thread_timeout_minutes'):
            op.add_column('chat_sessions', sa.Column('thread_timeout_minutes', sa.Integer(), nullable=True))
        if not column_exists(bind, 'chat_sessions', 'last_inbound_at'):
            op.add_column('chat_sessions', sa.Column('last_inbound_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()

    # Remove columns from chat_sessions
    if table_exists(bind, 'chat_sessions'):
        if column_exists(bind, 'chat_sessions', 'last_inbound_at'):
            op.drop_column('chat_sessions', 'last_inbound_at')
        if column_exists(bind, 'chat_sessions', 'thread_timeout_minutes'):
            op.drop_column('chat_sessions', 'thread_timeout_minutes')
        if column_exists(bind, 'chat_sessions', 'handler_type'):
            op.drop_column('chat_sessions', 'handler_type')

    # Remove columns from chat_messages
    if table_exists(bind, 'chat_messages'):
        if column_exists(bind, 'chat_messages', 'resolved_text'):
            op.drop_column('chat_messages', 'resolved_text')
        if column_exists(bind, 'chat_messages', 'content_pieces'):
            op.drop_column('chat_messages', 'content_pieces')

    # Drop tables
    if table_exists(bind, 'inbox_assignment_rules'):
        op.drop_table('inbox_assignment_rules')
    if table_exists(bind, 'contact_routing_states'):
        op.drop_table('contact_routing_states')
