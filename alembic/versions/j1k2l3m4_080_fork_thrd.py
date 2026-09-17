"""Add fork_step_id, parent_thread_id to threads

Revision ID: j1k2l3m4_080_fork_thrd
Revises: i9j0k1l2_079_snd_prio
Create Date: 2026-03-18 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'j1k2l3m4_080_fork_thrd'
down_revision = 'i9j0k1l2_079_snd_prio'
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    checks = inspector.get_check_constraints(table_name)
    return any(c['name'] == constraint_name for c in checks)


def _index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    # -- 1. Add fork_step_id column to funnel_enrollment_threads --
    if not _column_exists('funnel_enrollment_threads', 'fork_step_id'):
        op.add_column(
            'funnel_enrollment_threads',
            sa.Column('fork_step_id', sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            'fk_thread_fork_step',
            'funnel_enrollment_threads', 'funnel_steps',
            ['fork_step_id'], ['id'],
            ondelete='SET NULL',
        )

    # -- 2. Add parent_thread_id column to funnel_enrollment_threads --
    if not _column_exists('funnel_enrollment_threads', 'parent_thread_id'):
        op.add_column(
            'funnel_enrollment_threads',
            sa.Column('parent_thread_id', sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            'fk_thread_parent_thread',
            'funnel_enrollment_threads', 'funnel_enrollment_threads',
            ['parent_thread_id'], ['id'],
            ondelete='SET NULL',
        )

    # -- 3. Index on fork_step_id for join check queries --
    if not _index_exists('ix_thread_fork_step_id', 'funnel_enrollment_threads'):
        op.create_index(
            'ix_thread_fork_step_id',
            'funnel_enrollment_threads',
            ['fork_step_id'],
        )

    # -- 4. Replace valid_thread_status CHECK to include 'forked' --
    if _constraint_exists('funnel_enrollment_threads', 'valid_thread_status'):
        op.drop_constraint('valid_thread_status', 'funnel_enrollment_threads', type_='check')
    op.create_check_constraint(
        'valid_thread_status',
        'funnel_enrollment_threads',
        "status IN ('active', 'completed', 'exited', 'forked')",
    )

    # -- 5. Replace valid_funnel_step_branch CHECK with regex pattern --
    #    This allows path_N and exit_N for arbitrary N (not just 0-4)
    if _constraint_exists('funnel_steps', 'valid_funnel_step_branch'):
        op.drop_constraint('valid_funnel_step_branch', 'funnel_steps', type_='check')
    op.create_check_constraint(
        'valid_funnel_step_branch',
        'funnel_steps',
        "branch ~ '^(main|yes|no|path_[0-9]+|exit_[0-9]+)$'",
    )

    # -- 6. Backfill fork_step_id for existing threads --
    # For each thread, find the fork step in the same funnel whose id matches
    # the parent_step_id of the thread's path steps. Since nested forks didn't
    # exist before this migration, each thread belongs to exactly one fork step.
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE funnel_enrollment_threads t
        SET fork_step_id = sub.fork_step_id
        FROM (
            SELECT
                t2.id AS thread_id,
                fs_fork.id AS fork_step_id
            FROM funnel_enrollment_threads t2
            JOIN funnel_enrollments fe ON fe.id = t2.enrollment_id
            JOIN funnel_steps fs_fork ON fs_fork.funnel_id = fe.funnel_id
                AND fs_fork.step_type = 'fork'
            WHERE t2.fork_step_id IS NULL
              AND t2.current_branch LIKE 'path_%'
        ) sub
        WHERE t.id = sub.thread_id
    """))


def downgrade() -> None:
    # Restore original branch constraint
    if _constraint_exists('funnel_steps', 'valid_funnel_step_branch'):
        op.drop_constraint('valid_funnel_step_branch', 'funnel_steps', type_='check')
    op.create_check_constraint(
        'valid_funnel_step_branch',
        'funnel_steps',
        "branch IN ('main', 'yes', 'no', 'path_0', 'path_1', 'path_2', 'path_3', 'path_4', 'exit_0', 'exit_1', 'exit_2', 'exit_3', 'exit_4')",
    )

    # Restore original thread status constraint
    if _constraint_exists('funnel_enrollment_threads', 'valid_thread_status'):
        op.drop_constraint('valid_thread_status', 'funnel_enrollment_threads', type_='check')
    op.create_check_constraint(
        'valid_thread_status',
        'funnel_enrollment_threads',
        "status IN ('active', 'completed', 'exited')",
    )

    # Drop index
    if _index_exists('ix_thread_fork_step_id', 'funnel_enrollment_threads'):
        op.drop_index('ix_thread_fork_step_id', 'funnel_enrollment_threads')

    # Drop columns
    if _column_exists('funnel_enrollment_threads', 'parent_thread_id'):
        op.drop_constraint('fk_thread_parent_thread', 'funnel_enrollment_threads', type_='foreignkey')
        op.drop_column('funnel_enrollment_threads', 'parent_thread_id')

    if _column_exists('funnel_enrollment_threads', 'fork_step_id'):
        op.drop_constraint('fk_thread_fork_step', 'funnel_enrollment_threads', type_='foreignkey')
        op.drop_column('funnel_enrollment_threads', 'fork_step_id')
