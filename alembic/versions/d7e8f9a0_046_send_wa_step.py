"""046 add send_whatsapp step type

Revision ID: d7e8f9a0_046_send_wa_step
Revises: c6d7e8f9_045_wait_fork
Create Date: 2026-02-24 12:00:00.000000

"""
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'd7e8f9a0_046_send_wa_step'
down_revision = 'c6d7e8f9_045_wait_fork'
branch_labels = None
depends_on = None


def constraint_exists(table_name: str, constraint_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    constraints = inspector.get_check_constraints(table_name)
    return any(c['name'] == constraint_name for c in constraints)


def upgrade() -> None:
    # Update step_type CHECK constraint to include 'send_whatsapp'
    if constraint_exists('funnel_steps', 'valid_funnel_step_type'):
        op.drop_constraint('valid_funnel_step_type', 'funnel_steps', type_='check')
    op.create_check_constraint(
        'valid_funnel_step_type',
        'funnel_steps',
        "step_type IN ('wait', 'condition', 'action', 'exit', 'wait_for_reply', 'wait_until', 'fork', 'send_whatsapp')"
    )


def downgrade() -> None:
    if constraint_exists('funnel_steps', 'valid_funnel_step_type'):
        op.drop_constraint('valid_funnel_step_type', 'funnel_steps', type_='check')
    op.create_check_constraint(
        'valid_funnel_step_type',
        'funnel_steps',
        "step_type IN ('wait', 'condition', 'action', 'exit', 'wait_for_reply', 'wait_until', 'fork')"
    )
