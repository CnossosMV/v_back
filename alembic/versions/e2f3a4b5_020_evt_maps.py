"""019_add_event_mappings

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-01-18 10:06:00.000000

Creates messaging_event_mappings table for mapping
Versya events to destination-specific events.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'messaging_event_mappings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('event_schema_id', sa.Integer(), nullable=False),
        sa.Column('destination_id', sa.Integer(), nullable=False),
        sa.Column('destination_event_name', sa.String(255), nullable=False),
        sa.Column('property_mappings', sa.JSON(), nullable=True),  # Map Versya props to dest props
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['event_schema_id'], ['messaging_event_schemas.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['destination_id'], ['messaging_destinations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index('ix_event_mappings_project', 'messaging_event_mappings', ['project_id'])
    op.create_index('ix_event_mappings_schema', 'messaging_event_mappings', ['event_schema_id'])
    op.create_index('ix_event_mappings_dest', 'messaging_event_mappings', ['destination_id'])

    # Unique constraint: one mapping per event-destination pair
    op.create_index(
        'ix_event_mappings_schema_dest_unique',
        'messaging_event_mappings',
        ['event_schema_id', 'destination_id'],
        unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_event_mappings_schema_dest_unique', table_name='messaging_event_mappings')
    op.drop_index('ix_event_mappings_dest', table_name='messaging_event_mappings')
    op.drop_index('ix_event_mappings_schema', table_name='messaging_event_mappings')
    op.drop_index('ix_event_mappings_project', table_name='messaging_event_mappings')
    op.drop_table('messaging_event_mappings')
