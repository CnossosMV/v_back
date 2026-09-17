"""017_add_event_schemas

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-01-18 10:04:00.000000

Creates messaging_event_schemas table for defining
event structure and validation rules.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

# revision identifiers, used by Alembic.
revision = 'c0d1e2f3a4b5'
down_revision = 'b9c0d1e2f3a4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'messaging_event_schemas',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('event_name', sa.String(255), nullable=False),
        sa.Column('display_name', sa.String(255), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(100), nullable=True),
        sa.Column('properties_schema', sa.JSON(), nullable=True),  # JSON Schema format
        sa.Column('required_properties', ARRAY(sa.String), nullable=True),
        sa.Column('is_standard', sa.Boolean(), default=False, nullable=False),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index('ix_event_schemas_project_id', 'messaging_event_schemas', ['project_id'])
    op.create_index('ix_event_schemas_event_name', 'messaging_event_schemas', ['event_name'])
    op.create_index('ix_event_schemas_category', 'messaging_event_schemas', ['category'])

    # Unique constraint on project_id + event_name
    op.create_index(
        'ix_event_schemas_proj_name_unique',
        'messaging_event_schemas',
        ['project_id', 'event_name'],
        unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_event_schemas_proj_name_unique', table_name='messaging_event_schemas')
    op.drop_index('ix_event_schemas_category', table_name='messaging_event_schemas')
    op.drop_index('ix_event_schemas_event_name', table_name='messaging_event_schemas')
    op.drop_index('ix_event_schemas_project_id', table_name='messaging_event_schemas')
    op.drop_table('messaging_event_schemas')
