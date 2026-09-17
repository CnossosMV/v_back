"""Message effectiveness scoring tables

Revision ID: a1b2c3d4_095_mes
Revises: x5y6z7a8_094_undo_merge
Create Date: 2026-04-01

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import inspect

revision = 'a1b2c3d4_095_mes'
down_revision = 'x5y6z7a8_094_undo_merge'
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
    if not table_exists('message_effectiveness_scores'):
        op.create_table(
            'message_effectiveness_scores',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('send_log_id', sa.Integer(), sa.ForeignKey('send_logs.id', ondelete='CASCADE'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('messaging_users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('channel', sa.String(50), nullable=False),
            sa.Column('template_id', sa.Integer(), sa.ForeignKey('messaging_templates.id', ondelete='SET NULL'), nullable=True),
            sa.Column('source_type', sa.String(50), nullable=False),
            sa.Column('source_id', sa.Integer(), nullable=True),
            sa.Column('funnel_id', sa.Integer(), sa.ForeignKey('funnels.id', ondelete='SET NULL'), nullable=True),
            sa.Column('funnel_step_id', sa.Integer(), sa.ForeignKey('funnel_steps.id', ondelete='SET NULL'), nullable=True),
            sa.Column('reach_score', sa.Float(), nullable=False, server_default=sa.text('0')),
            sa.Column('reach_signals', JSONB, nullable=True),
            sa.Column('reengagement_score', sa.Float(), nullable=False, server_default=sa.text('0')),
            sa.Column('reengagement_signals', JSONB, nullable=True),
            sa.Column('combined_score', sa.Float(), nullable=False, server_default=sa.text('0')),
            sa.Column('score_grade', sa.String(20), nullable=False, server_default='pending'),
            sa.Column('algorithm_version', sa.Integer(), nullable=False, server_default=sa.text('1')),
            sa.Column('attribution_window_h', sa.Integer(), nullable=False, server_default=sa.text('24')),
            sa.Column('computed_at', sa.DateTime(), nullable=True),
            sa.Column('stale', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('send_log_id', name='uq_mes_send_log'),
        )

        op.create_index('ix_mes_proj_tpl', 'message_effectiveness_scores', ['project_id', 'template_id'])
        op.create_index('ix_mes_proj_channel', 'message_effectiveness_scores', ['project_id', 'channel'])
        op.create_index('ix_mes_proj_source', 'message_effectiveness_scores', ['project_id', 'source_type'])
        op.create_index('ix_mes_stale', 'message_effectiveness_scores', ['stale', 'computed_at'])
        op.create_index('ix_mes_proj_fnl_step', 'message_effectiveness_scores', ['project_id', 'funnel_step_id'])
        op.create_index('ix_mes_proj_grade', 'message_effectiveness_scores', ['project_id', 'score_grade'])
        op.create_index('ix_mes_proj_user', 'message_effectiveness_scores', ['project_id', 'user_id'])

    if not table_exists('mes_config'):
        op.create_table(
            'mes_config',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('attribution_window_hours', sa.Integer(), nullable=False, server_default=sa.text('24')),
            sa.Column('pre_session_window_min', sa.Integer(), nullable=False, server_default=sa.text('15')),
            sa.Column('reach_weight', sa.Float(), nullable=False, server_default=sa.text('0.5')),
            sa.Column('reengagement_weight', sa.Float(), nullable=False, server_default=sa.text('0.5')),
            sa.Column('grade_thresholds', JSONB, nullable=True),
            sa.Column('reengagement_event_weights', JSONB, nullable=True),
            sa.Column('exclude_event_patterns', JSONB, nullable=True),
            sa.Column('speed_decay_rate', sa.Float(), nullable=False, server_default=sa.text('0.08')),
            sa.Column('event_decay_rate', sa.Float(), nullable=False, server_default=sa.text('0.1')),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('project_id', name='uq_mes_config_project'),
        )


def downgrade() -> None:
    op.drop_table('mes_config')
    op.drop_table('message_effectiveness_scores')
