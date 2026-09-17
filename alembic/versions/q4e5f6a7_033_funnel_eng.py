"""Funnel Engine — 4 tables, drop 2 old

Revision ID: q4e5f6a7_033_funnel_eng
Revises: p3d4e5f6_032_proj_llm_cfg
Create Date: 2026-02-08 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'q4e5f6a7_033_funnel_eng'
down_revision = 'p3d4e5f6_032_proj_llm_cfg'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # --- Create ENUM types using raw SQL for reliability ---
    for stmt in [
        "CREATE TYPE funnel_status_enum AS ENUM ('draft', 'active', 'paused', 'archived')",
        "CREATE TYPE funnel_trigger_enum AS ENUM ('event', 'segment')",
        "CREATE TYPE funnel_step_type_enum AS ENUM ('wait', 'condition', 'action', 'exit')",
        "CREATE TYPE funnel_branch_enum AS ENUM ('main', 'yes', 'no')",
        "CREATE TYPE enrollment_status_enum AS ENUM ('active', 'completed', 'exited')",
    ]:
        op.execute(f"DO $$ BEGIN {stmt}; EXCEPTION WHEN duplicate_object THEN null; END $$;")

    # Use postgresql.ENUM with create_type=False per ALEMBIC_MIGRATION_RULES.md
    status_col = postgresql.ENUM('draft', 'active', 'paused', 'archived', name='funnel_status_enum', create_type=False)
    trigger_col = postgresql.ENUM('event', 'segment', name='funnel_trigger_enum', create_type=False)
    step_type_col = postgresql.ENUM('wait', 'condition', 'action', 'exit', name='funnel_step_type_enum', create_type=False)
    branch_col = postgresql.ENUM('main', 'yes', 'no', name='funnel_branch_enum', create_type=False)
    enroll_status_col = postgresql.ENUM('active', 'completed', 'exited', name='enrollment_status_enum', create_type=False)

    # --- 1. funnels ---
    if not table_exists('funnels'):
        op.create_table(
            'funnels',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('status', status_col, nullable=False, server_default='draft'),
            sa.Column('trigger_type', trigger_col, nullable=False),
            sa.Column('trigger_config', sa.JSON(), nullable=True, server_default='{}'),
            sa.Column('global_exit_config', sa.JSON(), nullable=True, server_default='{}'),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
            sa.CheckConstraint("status IN ('draft','active','paused','archived')", name='valid_funnel_status'),
            sa.CheckConstraint("trigger_type IN ('event','segment')", name='valid_funnel_trigger_type'),
        )
        op.create_index('ix_funnels_id', 'funnels', ['id'])
        op.create_index('ix_funnels_project_id', 'funnels', ['project_id'])
        op.create_index('ix_funnels_status', 'funnels', ['status'])
        op.create_index('ix_funnels_trigger_type', 'funnels', ['trigger_type'])

    # --- 2. funnel_steps ---
    if not table_exists('funnel_steps'):
        op.create_table(
            'funnel_steps',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('funnel_id', sa.Integer(), nullable=False),
            sa.Column('step_type', step_type_col, nullable=False),
            sa.Column('step_config', sa.JSON(), nullable=True, server_default='{}'),
            sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('parent_step_id', sa.Integer(), nullable=True),
            sa.Column('branch', branch_col, nullable=False, server_default='main'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['funnel_id'], ['funnels.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['parent_step_id'], ['funnel_steps.id'], ondelete='SET NULL'),
            sa.CheckConstraint("step_type IN ('wait','condition','action','exit')", name='valid_funnel_step_type'),
            sa.CheckConstraint("branch IN ('main','yes','no')", name='valid_funnel_step_branch'),
        )
        op.create_index('ix_funnel_steps_id', 'funnel_steps', ['id'])
        op.create_index('ix_funnel_steps_funnel_id', 'funnel_steps', ['funnel_id'])
        op.create_index('ix_funnel_steps_step_type', 'funnel_steps', ['step_type'])
        op.create_index('ix_funnel_steps_parent_step_id', 'funnel_steps', ['parent_step_id'])
        op.create_index('ix_funnel_steps_funnel_position', 'funnel_steps', ['funnel_id', 'position'])

    # --- 3. funnel_enrollments ---
    if not table_exists('funnel_enrollments'):
        op.create_table(
            'funnel_enrollments',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('funnel_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('status', enroll_status_col, nullable=False, server_default='active'),
            sa.Column('current_step_id', sa.Integer(), nullable=True),
            sa.Column('current_branch', branch_col, nullable=False, server_default='main'),
            sa.Column('messages_sent', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('exit_reason', sa.String(30), nullable=True),
            sa.Column('exited_at', sa.DateTime(), nullable=True),
            sa.Column('enrollment_metadata', sa.JSON(), nullable=True, server_default='{}'),
            sa.Column('enrolled_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('entered_step_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['funnel_id'], ['funnels.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['current_step_id'], ['funnel_steps.id'], ondelete='SET NULL'),
            sa.CheckConstraint("status IN ('active','completed','exited')", name='valid_enrollment_status'),
        )
        op.create_index('ix_funnel_enrollments_id', 'funnel_enrollments', ['id'])
        op.create_index('ix_funnel_enrollments_funnel_id', 'funnel_enrollments', ['funnel_id'])
        op.create_index('ix_funnel_enrollments_user_id', 'funnel_enrollments', ['user_id'])
        op.create_index('ix_funnel_enrollments_status', 'funnel_enrollments', ['status'])
        op.create_index('ix_funnel_enrollments_current_step_id', 'funnel_enrollments', ['current_step_id'])
        op.create_index('ix_funnel_enrollments_funnel_user', 'funnel_enrollments', ['funnel_id', 'user_id'])
        op.create_index('ix_funnel_enrollments_enrolled_at', 'funnel_enrollments', ['enrolled_at'])

    # --- 4. funnel_enrollment_logs ---
    if not table_exists('funnel_enrollment_logs'):
        op.create_table(
            'funnel_enrollment_logs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('enrollment_id', sa.Integer(), nullable=False),
            sa.Column('step_id', sa.Integer(), nullable=True),
            sa.Column('action', sa.String(30), nullable=False),
            sa.Column('details', sa.JSON(), nullable=True, server_default='{}'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['enrollment_id'], ['funnel_enrollments.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['step_id'], ['funnel_steps.id'], ondelete='SET NULL'),
        )
        op.create_index('ix_funnel_enrollment_logs_id', 'funnel_enrollment_logs', ['id'])
        op.create_index('ix_funnel_enrollment_logs_enrollment_id', 'funnel_enrollment_logs', ['enrollment_id'])

    # --- Drop old tables ---
    if table_exists('funnel_executions'):
        op.drop_table('funnel_executions')
    if table_exists('email_funnels'):
        op.drop_table('email_funnels')


def downgrade() -> None:
    # Drop new tables
    if table_exists('funnel_enrollment_logs'):
        op.drop_table('funnel_enrollment_logs')
    if table_exists('funnel_enrollments'):
        op.drop_table('funnel_enrollments')
    if table_exists('funnel_steps'):
        op.drop_table('funnel_steps')
    if table_exists('funnels'):
        op.drop_table('funnels')

    # Drop enum types
    for name in ['enrollment_status_enum', 'funnel_branch_enum', 'funnel_step_type_enum',
                 'funnel_trigger_enum', 'funnel_status_enum']:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)

    # Recreate old tables (basic structure for rollback)
    op.create_table(
        'email_funnels',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('trigger_type', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), server_default='draft', nullable=False),
        sa.Column('email_subject', sa.String(200), nullable=False),
        sa.Column('email_body', sa.Text(), nullable=False),
        sa.Column('email_from_name', sa.String(100), nullable=True),
        sa.Column('webhook_endpoint', sa.String(500), nullable=True),
        sa.Column('webhook_secret', sa.String(100), nullable=True),
        sa.Column('cron_schedule', sa.String(50), nullable=True),
        sa.Column('cron_timezone', sa.String(50), server_default='UTC', nullable=True),
        sa.Column('n8n_workflow_id', sa.String(100), nullable=True),
        sa.Column('n8n_webhook_id', sa.String(100), nullable=True),
        sa.Column('recipient_source', sa.String(20), server_default='manual', nullable=False),
        sa.Column('recipient_list', sa.JSON(), nullable=True),
        sa.Column('template_variables', sa.JSON(), nullable=True),
        sa.Column('emails_sent', sa.Integer(), server_default='0', nullable=False),
        sa.Column('emails_delivered', sa.Integer(), server_default='0', nullable=False),
        sa.Column('emails_opened', sa.Integer(), server_default='0', nullable=False),
        sa.Column('emails_clicked', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
    )
    op.create_table(
        'funnel_executions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('funnel_id', sa.Integer(), nullable=False),
        sa.Column('recipient_email', sa.String(255), nullable=False),
        sa.Column('recipient_data', sa.JSON(), nullable=True),
        sa.Column('execution_type', sa.String(20), nullable=True),
        sa.Column('email_subject', sa.String(200), nullable=True),
        sa.Column('email_body', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), server_default='queued', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('n8n_execution_id', sa.String(100), nullable=True),
        sa.Column('queued_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.Column('opened_at', sa.DateTime(), nullable=True),
        sa.Column('clicked_at', sa.DateTime(), nullable=True),
        sa.Column('failed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['funnel_id'], ['email_funnels.id']),
    )
