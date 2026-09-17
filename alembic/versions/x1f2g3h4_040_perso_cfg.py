"""040 personalization config

Revision ID: x1f2g3h4_040_perso_cfg
Revises: w0e1f2a3_039_policies
Create Date: 2026-02-17 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'x1f2g3h4_040_perso_cfg'
down_revision = 'w0e1f2a3_039_policies'
branch_labels = None
depends_on = None


def table_exists(bind, table_name):
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not table_exists(bind, 'project_personalization_configs'):
        op.create_table(
            'project_personalization_configs',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True),
            sa.Column('enabled', sa.Boolean(), server_default='false', nullable=False),
            sa.Column('exposed_traits', sa.JSON(), nullable=True),
            sa.Column('expose_scores', sa.Boolean(), server_default='false', nullable=False),
            sa.Column('expose_name', sa.Boolean(), server_default='false', nullable=False),
            sa.Column('expose_email', sa.Boolean(), server_default='false', nullable=False),
            sa.Column('cache_ttl_seconds', sa.Integer(), server_default='300', nullable=False),
            sa.Column('require_analytics_consent', sa.Boolean(), server_default='true', nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint('project_id', name='uq_project_perso_cfg'),
        )


def downgrade() -> None:
    op.drop_table('project_personalization_configs')
