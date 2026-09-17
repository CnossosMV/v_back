"""add_user_and_workspace_tables

Revision ID: 90089128408a
Revises: 1389870fd761
Create Date: 2025-08-24 23:13:03.185980

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '90089128408a'
down_revision = '1389870fd761'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create users table
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('keycloak_id', sa.String(length=100), nullable=True),
        sa.Column('role', sa.String(length=50), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('is_verified', sa.Boolean(), nullable=False),
        sa.Column('social_provider', sa.String(length=50), nullable=True),
        sa.Column('workspace_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_login', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for users table
    op.create_index(op.f('ix_users_id'), 'users', ['id'], unique=False)
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_keycloak_id'), 'users', ['keycloak_id'], unique=True)
    
    # Create workspaces table
    op.create_table(
        'workspaces',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], )
    )
    
    # Create indexes for workspaces table
    op.create_index(op.f('ix_workspaces_id'), 'workspaces', ['id'], unique=False)
    
    # Add foreign key constraint for workspace_id in users table
    op.create_foreign_key(None, 'users', 'workspaces', ['workspace_id'], ['id'])


def downgrade() -> None:
    # Drop foreign key constraint
    op.drop_constraint(None, 'users', type_='foreignkey')
    
    # Drop indexes
    op.drop_index(op.f('ix_workspaces_id'), table_name='workspaces')
    op.drop_index(op.f('ix_users_keycloak_id'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_index(op.f('ix_users_id'), table_name='users')
    
    # Drop tables
    op.drop_table('workspaces')
    op.drop_table('users')