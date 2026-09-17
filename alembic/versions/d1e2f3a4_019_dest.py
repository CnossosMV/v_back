"""018_add_destinations

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-01-18 10:05:00.000000

Creates messaging_destinations table for analytics
destination configuration (GA4, Meta Pixel, etc.).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = 'c0d1e2f3a4b5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum type using raw SQL with IF NOT EXISTS for reliability
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE destination_type AS ENUM ('ga4', 'meta_pixel', 'google_ads', 'linkedin', 'tiktok', 'custom');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Use postgresql.ENUM with create_type=False for reliable behavior
    dest_type_col = postgresql.ENUM(
        'ga4', 'meta_pixel', 'google_ads', 'linkedin', 'tiktok', 'custom',
        name='destination_type',
        create_type=False
    )

    op.create_table(
        'messaging_destinations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('destination_type', dest_type_col, nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('config_encrypted', sa.Text(), nullable=True),  # Fernet encrypted
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('consent_required', ARRAY(sa.String), nullable=True),  # e.g., ['analytics', 'marketing']
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index('ix_destinations_project_id', 'messaging_destinations', ['project_id'])
    op.create_index('ix_destinations_type', 'messaging_destinations', ['destination_type'])


def downgrade() -> None:
    op.drop_index('ix_destinations_type', table_name='messaging_destinations')
    op.drop_index('ix_destinations_project_id', table_name='messaging_destinations')
    op.drop_table('messaging_destinations')

    # Drop enum using raw SQL for reliability
    op.execute("DROP TYPE IF EXISTS destination_type")
