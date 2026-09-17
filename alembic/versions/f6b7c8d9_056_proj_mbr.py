"""Add project_members table for role-based access

Revision ID: f6b7c8d9_056_proj_mbr
Revises: e4f5a6b7_055_send_layer
Create Date: 2026-03-01 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'f6b7c8d9_056_proj_mbr'
down_revision = 'e4f5a6b7_055_send_layer'
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
    if table_exists('project_members'):
        return

    op.create_table(
        'project_members',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(30), nullable=False, server_default='viewer'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('invited_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'user_id', name='uq_proj_member_proj_user'),
    )

    op.create_index('ix_proj_mbr_proj_role', 'project_members', ['project_id', 'role'])
    op.create_index('ix_proj_mbr_user_active', 'project_members', ['user_id', 'is_active'])

    # Seed: auto-create owner membership for each workspace owner's projects
    connection = op.get_bind()
    connection.execute(sa.text("""
        INSERT INTO project_members (project_id, user_id, role, is_active, created_at, updated_at)
        SELECT p.id, w.owner_id, 'owner', true, NOW(), NOW()
        FROM projects p
        JOIN workspaces w ON p.workspace_id = w.id
        WHERE NOT EXISTS (
            SELECT 1 FROM project_members pm
            WHERE pm.project_id = p.id AND pm.user_id = w.owner_id
        )
    """))


def downgrade() -> None:
    if table_exists('project_members'):
        op.drop_table('project_members')
