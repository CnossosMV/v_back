"""015_add_anon_profiles

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-01-18 10:02:00.000000

Creates messaging_anonymous_profiles table for tracking
anonymous users before identification (identity resolution).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a8b9c0d1e2f3'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'messaging_anonymous_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('anonymous_id', sa.String(100), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('merged_to_user_id', sa.Integer(), nullable=True),
        sa.Column('merged_at', sa.DateTime(), nullable=True),
        sa.Column('properties', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['merged_to_user_id'], ['messaging_users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index(
        'ix_msg_anon_profiles_project_id',
        'messaging_anonymous_profiles',
        ['project_id']
    )
    op.create_index(
        'ix_msg_anon_profiles_anon_id',
        'messaging_anonymous_profiles',
        ['anonymous_id']
    )

    # Unique constraint on project_id + anonymous_id
    op.create_index(
        'ix_msg_anon_proj_anon_unique',
        'messaging_anonymous_profiles',
        ['project_id', 'anonymous_id'],
        unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_msg_anon_proj_anon_unique', table_name='messaging_anonymous_profiles')
    op.drop_index('ix_msg_anon_profiles_anon_id', table_name='messaging_anonymous_profiles')
    op.drop_index('ix_msg_anon_profiles_project_id', table_name='messaging_anonymous_profiles')
    op.drop_table('messaging_anonymous_profiles')
