"""Event graph journey tables

Revision ID: b2c3d4e5_096_journey
Revises: a1b2c3d4_095_mes
Create Date: 2026-04-09

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'b2c3d4e5_096_journey'
down_revision = 'a1b2c3d4_095_mes'
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


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    # ── 1. Add columns to messaging_events ──
    if not column_exists('messaging_events', 'session_id'):
        op.add_column('messaging_events', sa.Column('session_id', sa.String(100), nullable=True))

    if not column_exists('messaging_events', 'client_ts'):
        op.add_column('messaging_events', sa.Column('client_ts', sa.DateTime(timezone=True), nullable=True))

    if not index_exists('ix_msg_events_proj_session', 'messaging_events'):
        op.create_index('ix_msg_events_proj_session', 'messaging_events', ['project_id', 'session_id'])

    # ── 2. Add goal_event to projects ──
    if not column_exists('projects', 'goal_event'):
        op.add_column('projects', sa.Column('goal_event', sa.String(255), nullable=True))

    # ── 3. journey_nodes ──
    if not table_exists('journey_nodes'):
        op.create_table(
            'journey_nodes',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('event_name', sa.String(255), nullable=False),
            sa.Column('period_start', sa.Date(), nullable=False),
            sa.Column('period_end', sa.Date(), nullable=False),
            sa.Column('frequency', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('unique_users', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('current_occupancy', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('hot_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('warm_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('cold_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('dead_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('throughput_rate', sa.Float(), nullable=True),
            sa.Column('avg_dwell_seconds', sa.Float(), nullable=True),
            sa.Column('conversion_rate', sa.Float(), nullable=False, server_default=sa.text('0')),
            sa.Column('churned_from_here', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('timed_out_here', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('project_id', 'event_name', 'period_start', 'period_end', name='uq_jn_proj_evt_period'),
        )
        op.create_index('ix_jn_proj_id', 'journey_nodes', ['project_id'])

    # ── 4. journey_edges ──
    if not table_exists('journey_edges'):
        op.create_table(
            'journey_edges',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('source_event', sa.String(255), nullable=False),
            sa.Column('target_event', sa.String(255), nullable=False),
            sa.Column('period_start', sa.Date(), nullable=False),
            sa.Column('period_end', sa.Date(), nullable=False),
            sa.Column('total_transitions', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('converted_transitions', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('unique_users', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('median_seconds', sa.Float(), nullable=True),
            sa.Column('p90_seconds', sa.Float(), nullable=True),
            sa.Column('beta_alpha', sa.Float(), nullable=False, server_default=sa.text('1.0')),
            sa.Column('beta_beta', sa.Float(), nullable=False, server_default=sa.text('1.0')),
            sa.Column('drop_off_rate', sa.Float(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('project_id', 'source_event', 'target_event', 'period_start', 'period_end', name='uq_je_proj_src_tgt_period'),
        )
        op.create_index('ix_je_proj_id', 'journey_edges', ['project_id'])

    # ── 5. journey_interventions ──
    if not table_exists('journey_interventions'):
        op.create_table(
            'journey_interventions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('period_start', sa.Date(), nullable=False),
            sa.Column('period_end', sa.Date(), nullable=False),
            sa.Column('pre_event', sa.String(255), nullable=False),
            sa.Column('post_event', sa.String(255), nullable=True),
            sa.Column('channel', sa.String(30), nullable=False),
            sa.Column('source_type', sa.String(50), nullable=False),
            sa.Column('template_id', sa.Integer(), nullable=True),
            sa.Column('arm_id', sa.String(100), nullable=False),
            sa.Column('total_sent', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('total_responded', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('total_converted', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('median_response_seconds', sa.Float(), nullable=True),
            sa.Column('beta_alpha', sa.Float(), nullable=False, server_default=sa.text('1.0')),
            sa.Column('beta_beta', sa.Float(), nullable=False, server_default=sa.text('1.0')),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('project_id', 'pre_event', 'arm_id', 'period_start', 'period_end', name='uq_ji_proj_pre_arm_period'),
        )
        op.create_index('ix_ji_proj_id', 'journey_interventions', ['project_id'])

    # ── 6. journey_snapshots ──
    if not table_exists('journey_snapshots'):
        op.create_table(
            'journey_snapshots',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('messaging_users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('current_event', sa.String(255), nullable=True),
            sa.Column('current_event_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('thermal_state', sa.String(10), nullable=True),
            sa.Column('terminal_state', sa.String(20), nullable=True),
            sa.Column('last_intervention_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_intervention_arm', sa.String(100), nullable=True),
            sa.Column('responded_to_last', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('project_id', 'user_id', name='uq_js_proj_user'),
        )
        op.create_index('ix_js_proj_id', 'journey_snapshots', ['project_id'])
        op.create_index('ix_js_proj_thermal', 'journey_snapshots', ['project_id', 'thermal_state'])

    # ── 7. journey_backfill_jobs ──
    if not table_exists('journey_backfill_jobs'):
        op.create_table(
            'journey_backfill_jobs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('job_type', sa.String(30), nullable=False),
            sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
            sa.Column('total_rows', sa.Integer(), nullable=True),
            sa.Column('processed_rows', sa.Integer(), nullable=False, server_default=sa.text('0')),
            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_jbj_proj_id', 'journey_backfill_jobs', ['project_id'])


def downgrade() -> None:
    op.drop_table('journey_backfill_jobs')
    op.drop_table('journey_snapshots')
    op.drop_table('journey_interventions')
    op.drop_table('journey_edges')
    op.drop_table('journey_nodes')

    if column_exists('projects', 'goal_event'):
        op.drop_column('projects', 'goal_event')

    if index_exists('ix_msg_events_proj_session', 'messaging_events'):
        op.drop_index('ix_msg_events_proj_session', table_name='messaging_events')
    if column_exists('messaging_events', 'client_ts'):
        op.drop_column('messaging_events', 'client_ts')
    if column_exists('messaging_events', 'session_id'):
        op.drop_column('messaging_events', 'session_id')
