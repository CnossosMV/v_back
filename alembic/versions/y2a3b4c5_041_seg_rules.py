"""041 segment rules

Revision ID: y2a3b4c5_041_seg_rules
Revises: x1f2g3h4_040_perso_cfg
Create Date: 2026-02-17 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'y2a3b4c5_041_seg_rules'
down_revision = 'x1f2g3h4_040_perso_cfg'
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
    for table_name in insp.get_table_names():
        indexes = insp.get_indexes(table_name)
        for idx in indexes:
            if idx['name'] == index_name:
                return True
    return False


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create segment_rules table
    if not table_exists(bind, 'segment_rules'):
        op.create_table(
            'segment_rules',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('conditions', sa.JSON(), nullable=True),
            sa.Column('match_mode', sa.String(10), nullable=False, server_default='all'),
            sa.Column('is_catch_all', sa.Boolean(), nullable=False, server_default='false'),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint('project_id', 'name', name='uq_segment_rule_project_name'),
            sa.Index('ix_segment_rules_project_priority', 'project_id', 'priority'),
        )

    # 2. Add segment columns to messaging_users
    if not column_exists(bind, 'messaging_users', 'segment_rule_id'):
        op.add_column('messaging_users', sa.Column(
            'segment_rule_id',
            sa.Integer(),
            sa.ForeignKey('segment_rules.id', ondelete='SET NULL'),
            nullable=True,
        ))

    if not column_exists(bind, 'messaging_users', 'segment_name'):
        op.add_column('messaging_users', sa.Column(
            'segment_name',
            sa.String(255),
            nullable=True,
        ))

    if not column_exists(bind, 'messaging_users', 'segment_updated_at'):
        op.add_column('messaging_users', sa.Column(
            'segment_updated_at',
            sa.DateTime(),
            nullable=True,
        ))

    # 3. Add index on messaging_users for segment lookups
    if not index_exists(bind, 'ix_msg_users_project_segment'):
        op.create_index(
            'ix_msg_users_project_segment',
            'messaging_users',
            ['project_id', 'segment_rule_id'],
        )


def downgrade() -> None:
    op.drop_index('ix_msg_users_project_segment', table_name='messaging_users')
    op.drop_column('messaging_users', 'segment_updated_at')
    op.drop_column('messaging_users', 'segment_name')
    op.drop_column('messaging_users', 'segment_rule_id')
    op.drop_table('segment_rules')
