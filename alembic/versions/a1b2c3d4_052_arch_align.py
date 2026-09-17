"""052 architecture alignment

Revision ID: a1b2c3d4_052_arch_align
Revises: z3b4c5d6_042_inbound_rtr
Create Date: 2026-02-28 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4_052_arch_align'
down_revision = 'z3b4c5d6_042_inbound_rtr'
branch_labels = None
depends_on = None


def table_exists(bind, table_name):
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def column_exists(bind, table_name, column_name):
    insp = sa.inspect(bind)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def constraint_exists(bind, table_name, constraint_name):
    insp = sa.inspect(bind)
    constraints = insp.get_check_constraints(table_name)
    return any(c['name'] == constraint_name for c in constraints)


def upgrade() -> None:
    bind = op.get_bind()

    # ── Phase A: SupportTicket.escalation_origin ───────────────────────
    if table_exists(bind, 'support_tickets'):
        if not column_exists(bind, 'support_tickets', 'escalation_origin'):
            op.add_column('support_tickets', sa.Column('escalation_origin', sa.JSON(), nullable=True))

        # Add 'expired' to status constraint
        if constraint_exists(bind, 'support_tickets', 'valid_ticket_status'):
            op.drop_constraint('valid_ticket_status', 'support_tickets', type_='check')
        op.create_check_constraint(
            'valid_ticket_status', 'support_tickets',
            "status IN ('open', 'in_progress', 'waiting_customer', 'resolved', 'closed', 'expired')"
        )

    # ── Phase B: send_whatsapp → send_message ──────────────────────────
    if table_exists(bind, 'funnel_steps'):
        # Update step_type CHECK constraint
        if constraint_exists(bind, 'funnel_steps', 'valid_funnel_step_type'):
            op.drop_constraint('valid_funnel_step_type', 'funnel_steps', type_='check')
        op.create_check_constraint(
            'valid_funnel_step_type', 'funnel_steps',
            "step_type IN ('wait', 'condition', 'action', 'exit', 'wait_for_reply', 'wait_until', 'fork', 'send_message')"
        )

        # Rename existing send_whatsapp → send_message
        op.execute(
            "UPDATE funnel_steps SET step_type = 'send_message' WHERE step_type = 'send_whatsapp'"
        )

        # Add channel: 'whatsapp' to step_config for migrated steps
        op.execute("""
            UPDATE funnel_steps
            SET step_config = jsonb_set(
                COALESCE(step_config::jsonb, '{}'::jsonb),
                '{channel}', '"whatsapp"'
            )
            WHERE step_type = 'send_message'
              AND (step_config IS NULL OR (step_config::jsonb)->>'channel' IS NULL)
        """)

    # Rename funnel_whatsapp → funnel_send in routing states
    if table_exists(bind, 'contact_routing_states'):
        op.execute(
            "UPDATE contact_routing_states SET handler_type = 'funnel_send' "
            "WHERE handler_type = 'funnel_whatsapp'"
        )

    # ── Phase C: Funnel.debug_mode ─────────────────────────────────────
    if table_exists(bind, 'funnels'):
        if not column_exists(bind, 'funnels', 'debug_mode'):
            op.add_column('funnels', sa.Column(
                'debug_mode', sa.Boolean(), nullable=False, server_default='false'
            ))

    # Add pending_approval to chat_messages.delivery_status if column exists
    # (delivery_status is a free-form string, no constraint to update)

    # ── Phase D: Silent actions on InboxAssignmentRule ──────────────────
    if table_exists(bind, 'inbox_assignment_rules'):
        if not column_exists(bind, 'inbox_assignment_rules', 'is_silent'):
            op.add_column('inbox_assignment_rules', sa.Column(
                'is_silent', sa.Boolean(), nullable=False, server_default='false'
            ))
        if not column_exists(bind, 'inbox_assignment_rules', 'silent_actions'):
            op.add_column('inbox_assignment_rules', sa.Column(
                'silent_actions', sa.JSON(), nullable=True
            ))

    # Project TTL columns
    if table_exists(bind, 'projects'):
        if not column_exists(bind, 'projects', 'inbox_ttl_waiting_agent'):
            op.add_column('projects', sa.Column(
                'inbox_ttl_waiting_agent', sa.Integer(), nullable=True
            ))
        if not column_exists(bind, 'projects', 'inbox_ttl_waiting_customer'):
            op.add_column('projects', sa.Column(
                'inbox_ttl_waiting_customer', sa.Integer(), nullable=True
            ))

    # ── Phase E: Opt-out fields on MessagingUser ───────────────────────
    if table_exists(bind, 'messaging_users'):
        if not column_exists(bind, 'messaging_users', 'opted_out_channels'):
            op.add_column('messaging_users', sa.Column(
                'opted_out_channels', sa.JSON(), nullable=True
            ))
        if not column_exists(bind, 'messaging_users', 'global_opt_out'):
            op.add_column('messaging_users', sa.Column(
                'global_opt_out', sa.Boolean(), nullable=False, server_default='false'
            ))

    # ── Phase E: ContactRateWindow table ───────────────────────────────
    if not table_exists(bind, 'contact_rate_windows'):
        op.create_table(
            'contact_rate_windows',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('contact_identifier', sa.String(255), nullable=False),
            sa.Column('channel', sa.String(50), nullable=False),
            sa.Column('window_start', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('message_count', sa.Integer(), nullable=False, server_default='1'),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        )
        op.create_index(
            'ix_rate_window_lookup',
            'contact_rate_windows',
            ['project_id', 'contact_identifier', 'channel', 'window_start'],
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Drop contact_rate_windows
    if table_exists(bind, 'contact_rate_windows'):
        op.drop_table('contact_rate_windows')

    # Remove opt-out fields
    if table_exists(bind, 'messaging_users'):
        if column_exists(bind, 'messaging_users', 'global_opt_out'):
            op.drop_column('messaging_users', 'global_opt_out')
        if column_exists(bind, 'messaging_users', 'opted_out_channels'):
            op.drop_column('messaging_users', 'opted_out_channels')

    # Remove Project TTL columns
    if table_exists(bind, 'projects'):
        if column_exists(bind, 'projects', 'inbox_ttl_waiting_customer'):
            op.drop_column('projects', 'inbox_ttl_waiting_customer')
        if column_exists(bind, 'projects', 'inbox_ttl_waiting_agent'):
            op.drop_column('projects', 'inbox_ttl_waiting_agent')

    # Remove silent action columns
    if table_exists(bind, 'inbox_assignment_rules'):
        if column_exists(bind, 'inbox_assignment_rules', 'silent_actions'):
            op.drop_column('inbox_assignment_rules', 'silent_actions')
        if column_exists(bind, 'inbox_assignment_rules', 'is_silent'):
            op.drop_column('inbox_assignment_rules', 'is_silent')

    # Remove debug_mode
    if table_exists(bind, 'funnels'):
        if column_exists(bind, 'funnels', 'debug_mode'):
            op.drop_column('funnels', 'debug_mode')

    # Revert send_message → send_whatsapp
    if table_exists(bind, 'funnel_steps'):
        op.execute(
            "UPDATE funnel_steps SET step_type = 'send_whatsapp' WHERE step_type = 'send_message'"
        )
        if constraint_exists(bind, 'funnel_steps', 'valid_funnel_step_type'):
            op.drop_constraint('valid_funnel_step_type', 'funnel_steps', type_='check')
        op.create_check_constraint(
            'valid_funnel_step_type', 'funnel_steps',
            "step_type IN ('wait', 'condition', 'action', 'exit', 'wait_for_reply', 'wait_until', 'fork', 'send_whatsapp')"
        )

    # Revert funnel_send → funnel_whatsapp
    if table_exists(bind, 'contact_routing_states'):
        op.execute(
            "UPDATE contact_routing_states SET handler_type = 'funnel_whatsapp' "
            "WHERE handler_type = 'funnel_send'"
        )

    # Revert ticket status constraint
    if table_exists(bind, 'support_tickets'):
        if constraint_exists(bind, 'support_tickets', 'valid_ticket_status'):
            op.drop_constraint('valid_ticket_status', 'support_tickets', type_='check')
        op.create_check_constraint(
            'valid_ticket_status', 'support_tickets',
            "status IN ('open', 'in_progress', 'waiting_customer', 'resolved', 'closed')"
        )
        if column_exists(bind, 'support_tickets', 'escalation_origin'):
            op.drop_column('support_tickets', 'escalation_origin')
