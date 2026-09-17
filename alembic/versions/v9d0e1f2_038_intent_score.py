"""Add intent scoring tables

Revision ID: v9d0e1f2_038_intent_score
Revises: u8c9d0e1_037_api_conn
Create Date: 2026-02-14 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'v9d0e1f2_038_intent_score'
down_revision = 'u8c9d0e1_037_api_conn'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # --- Table 1: score_definitions ---
    if not table_exists('score_definitions'):
        op.create_table(
            'score_definitions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('slug', sa.String(100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('score_type', sa.String(30), nullable=False),
            sa.Column('version', sa.Integer(), server_default='1', nullable=False),
            sa.Column('status', sa.String(20), server_default='draft', nullable=False),
            sa.Column('signals', sa.JSON(), nullable=False, server_default='[]'),
            sa.Column('decay_config', sa.JSON(), nullable=True),
            sa.Column('thresholds', sa.JSON(), nullable=True),
            sa.Column('normalization_max', sa.Float(), server_default='100', nullable=False),
            sa.Column('recalc_on_event', sa.Boolean(), server_default='true', nullable=False),
            sa.Column('recalc_interval_minutes', sa.Integer(), server_default='0', nullable=False),
            sa.Column('last_recalc_at', sa.DateTime(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
            sa.UniqueConstraint('project_id', 'slug', name='uq_score_def_project_slug'),
            sa.CheckConstraint(
                "score_type IN ('intent', 'friction', 'churn_risk', 'custom')",
                name='valid_score_type',
            ),
            sa.CheckConstraint(
                "status IN ('draft', 'active', 'archived')",
                name='valid_score_def_status',
            ),
        )
        op.create_index('ix_score_definitions_id', 'score_definitions', ['id'])
        op.create_index('ix_score_definitions_project_id', 'score_definitions', ['project_id'])
        op.create_index('ix_score_definitions_status', 'score_definitions', ['status'])

    # --- Table 2: user_feature_store ---
    if not table_exists('user_feature_store'):
        op.create_table(
            'user_feature_store',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('event_name', sa.String(200), nullable=False),
            sa.Column('count_total', sa.Integer(), server_default='0', nullable=False),
            sa.Column('count_1d', sa.Integer(), server_default='0', nullable=False),
            sa.Column('count_7d', sa.Integer(), server_default='0', nullable=False),
            sa.Column('count_30d', sa.Integer(), server_default='0', nullable=False),
            sa.Column('sum_value', sa.Float(), server_default='0', nullable=False),
            sa.Column('last_value', sa.JSON(), nullable=True),
            sa.Column('first_seen_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='CASCADE'),
            sa.UniqueConstraint('project_id', 'user_id', 'event_name', name='uq_feature_store_user_event'),
        )
        op.create_index('ix_user_feature_store_id', 'user_feature_store', ['id'])
        op.create_index('ix_user_feature_store_project_user', 'user_feature_store', ['project_id', 'user_id'])
        op.create_index('ix_user_feature_store_event', 'user_feature_store', ['event_name'])

    # --- Table 3: user_score_snapshots ---
    if not table_exists('user_score_snapshots'):
        op.create_table(
            'user_score_snapshots',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('score_definition_id', sa.Integer(), nullable=False),
            sa.Column('score', sa.Float(), nullable=False),
            sa.Column('tier', sa.String(20), nullable=False),
            sa.Column('explanation', sa.JSON(), nullable=True),
            sa.Column('previous_score', sa.Float(), nullable=True),
            sa.Column('score_delta', sa.Float(), nullable=True),
            sa.Column('calculated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['score_definition_id'], ['score_definitions.id'], ondelete='CASCADE'),
            sa.UniqueConstraint('user_id', 'score_definition_id', name='uq_score_snapshot_user_def'),
        )
        op.create_index('ix_user_score_snapshots_id', 'user_score_snapshots', ['id'])
        op.create_index('ix_user_score_snapshots_project_user', 'user_score_snapshots', ['project_id', 'user_id'])
        op.create_index('ix_user_score_snapshots_def', 'user_score_snapshots', ['score_definition_id'])
        op.create_index('ix_user_score_snapshots_tier', 'user_score_snapshots', ['tier'])


def downgrade() -> None:
    if table_exists('user_score_snapshots'):
        op.drop_table('user_score_snapshots')
    if table_exists('user_feature_store'):
        op.drop_table('user_feature_store')
    if table_exists('score_definitions'):
        op.drop_table('score_definitions')
