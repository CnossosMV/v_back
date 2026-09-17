"""045 add wait_until and fork step types

Revision ID: c6d7e8f9_045_wait_fork
Revises: b5c6d7e8_044_spa_track
Create Date: 2026-02-23 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'c6d7e8f9_045_wait_fork'
down_revision = 'b5c6d7e8_044_spa_track'
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


def constraint_exists(table_name: str, constraint_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    constraints = inspector.get_check_constraints(table_name)
    return any(c['name'] == constraint_name for c in constraints)


def upgrade() -> None:
    # 1a. New table: funnel_enrollment_threads
    if not table_exists('funnel_enrollment_threads'):
        op.create_table(
            'funnel_enrollment_threads',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('enrollment_id', sa.Integer(), nullable=False),
            sa.Column('thread_index', sa.Integer(), nullable=False),
            sa.Column('label', sa.String(100), nullable=True),
            sa.Column('status', sa.String(20), nullable=False, server_default='active'),
            sa.Column('current_step_id', sa.Integer(), nullable=True),
            sa.Column('current_branch', sa.String(20), nullable=False, server_default='main'),
            sa.Column('entered_step_at', sa.DateTime(), server_default=sa.func.now()),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['enrollment_id'], ['funnel_enrollments.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['current_step_id'], ['funnel_steps.id'], ondelete='SET NULL'),
            sa.UniqueConstraint('enrollment_id', 'thread_index', name='uq_enrollment_thread_idx'),
            sa.CheckConstraint(
                "status IN ('active', 'completed', 'exited')",
                name='valid_thread_status'
            ),
        )
        op.create_index('ix_enrollment_threads_enrollment', 'funnel_enrollment_threads', ['enrollment_id'])

    # 1b. Add thread_count column to funnel_enrollments
    if not column_exists('funnel_enrollments', 'thread_count'):
        op.add_column(
            'funnel_enrollments',
            sa.Column('thread_count', sa.Integer(), nullable=True)
        )

    # 1c. Drop ALL CHECK constraints on ALL funnel tables FIRST — they reference ENUM types
    # funnel_steps (2 constraints)
    if constraint_exists('funnel_steps', 'valid_funnel_step_type'):
        op.drop_constraint('valid_funnel_step_type', 'funnel_steps', type_='check')
    if constraint_exists('funnel_steps', 'valid_funnel_step_branch'):
        op.drop_constraint('valid_funnel_step_branch', 'funnel_steps', type_='check')
    # funnel_enrollments (1 constraint)
    if constraint_exists('funnel_enrollments', 'valid_enrollment_status'):
        op.drop_constraint('valid_enrollment_status', 'funnel_enrollments', type_='check')
    # funnels (2 constraints)
    if constraint_exists('funnels', 'valid_funnel_status'):
        op.drop_constraint('valid_funnel_status', 'funnels', type_='check')
    if constraint_exists('funnels', 'valid_funnel_trigger_type'):
        op.drop_constraint('valid_funnel_trigger_type', 'funnels', type_='check')

    # 1d. Helper: detect if a column is an ENUM and convert to VARCHAR
    connection = op.get_bind()

    def _is_enum(table, column):
        row = connection.execute(sa.text(
            f"SELECT udt_name FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND column_name = '{column}'"
        )).fetchone()
        return row and row[0].endswith('_enum')

    def _convert_enum_to_varchar(table, column, length):
        if _is_enum(table, column):
            op.alter_column(table, column,
                            type_=sa.String(length),
                            existing_nullable=False,
                            postgresql_using=f'{column}::text')
            # Reset server default so it no longer references the ENUM type
            op.alter_column(table, column,
                            server_default=sa.text("'main'") if column in ('branch', 'current_branch')
                            else sa.text("'active'") if column == 'status'
                            else None,
                            existing_type=sa.String(length))

    # Convert all funnel ENUM columns to VARCHAR
    # funnel_steps: step_type (funnel_step_type_enum), branch (funnel_branch_enum)
    _convert_enum_to_varchar('funnel_steps', 'step_type', 30)
    _convert_enum_to_varchar('funnel_steps', 'branch', 20)

    # funnel_enrollments: status (enrollment_status_enum), current_branch (funnel_branch_enum)
    _convert_enum_to_varchar('funnel_enrollments', 'status', 20)
    _convert_enum_to_varchar('funnel_enrollments', 'current_branch', 20)

    # funnels: status (funnel_status_enum), trigger_type (funnel_trigger_enum)
    _convert_enum_to_varchar('funnels', 'status', 20)
    _convert_enum_to_varchar('funnels', 'trigger_type', 20)

    # Drop all old funnel ENUM types
    for enum_name in ['funnel_branch_enum', 'funnel_step_type_enum',
                      'enrollment_status_enum', 'funnel_status_enum', 'funnel_trigger_enum']:
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))

    # 1e. Recreate ALL CHECK constraints (now VARCHAR, no ENUM conflicts)
    # funnel_steps
    op.create_check_constraint(
        'valid_funnel_step_type',
        'funnel_steps',
        "step_type IN ('wait', 'condition', 'action', 'exit', 'wait_for_reply', 'wait_until', 'fork')"
    )
    op.create_check_constraint(
        'valid_funnel_step_branch',
        'funnel_steps',
        "branch IN ('main', 'yes', 'no', 'path_0', 'path_1', 'path_2', 'path_3', 'path_4', 'exit_0', 'exit_1', 'exit_2', 'exit_3', 'exit_4')"
    )
    # funnel_enrollments
    op.create_check_constraint(
        'valid_enrollment_status',
        'funnel_enrollments',
        "status IN ('active', 'completed', 'exited')"
    )
    # funnels
    op.create_check_constraint(
        'valid_funnel_status',
        'funnels',
        "status IN ('draft', 'active', 'paused', 'archived')"
    )
    op.create_check_constraint(
        'valid_funnel_trigger_type',
        'funnels',
        "trigger_type IN ('event', 'segment')"
    )


def downgrade() -> None:
    # Restore CHECK constraints
    if constraint_exists('funnel_steps', 'valid_funnel_step_branch'):
        op.drop_constraint('valid_funnel_step_branch', 'funnel_steps', type_='check')
    op.create_check_constraint(
        'valid_funnel_step_branch',
        'funnel_steps',
        "branch IN ('main', 'yes', 'no')"
    )

    if constraint_exists('funnel_steps', 'valid_funnel_step_type'):
        op.drop_constraint('valid_funnel_step_type', 'funnel_steps', type_='check')
    op.create_check_constraint(
        'valid_funnel_step_type',
        'funnel_steps',
        "step_type IN ('wait', 'condition', 'action', 'exit')"
    )

    # Restore column widths
    op.alter_column(
        'funnel_enrollments',
        'current_branch',
        type_=sa.String(10),
        existing_type=sa.String(20),
        existing_nullable=False,
        existing_server_default='main',
    )

    op.alter_column(
        'funnel_steps',
        'branch',
        type_=sa.String(10),
        existing_type=sa.String(20),
        existing_nullable=False,
        existing_server_default='main',
    )

    # Drop thread_count column
    if column_exists('funnel_enrollments', 'thread_count'):
        op.drop_column('funnel_enrollments', 'thread_count')

    # Drop threads table
    if table_exists('funnel_enrollment_threads'):
        op.drop_index('ix_enrollment_threads_enrollment', table_name='funnel_enrollment_threads')
        op.drop_table('funnel_enrollment_threads')
