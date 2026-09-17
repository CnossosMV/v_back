"""020_add_gtm_containers

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-01-18 10:07:00.000000

Creates messaging_gtm_containers table for storing
GTM container generation configurations.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'messaging_gtm_containers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('domain_id', sa.Integer(), nullable=True),
        sa.Column('container_name', sa.String(255), nullable=False),
        sa.Column('gtm_container_id', sa.String(50), nullable=True),  # GTM-XXXXX
        sa.Column('last_generated_at', sa.DateTime(), nullable=True),
        sa.Column('generated_json', sa.JSON(), nullable=True),  # Cached generated container
        sa.Column('version', sa.Integer(), default=1, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['domain_id'], ['messaging_domains.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index('ix_gtm_containers_project', 'messaging_gtm_containers', ['project_id'])
    op.create_index('ix_gtm_containers_domain', 'messaging_gtm_containers', ['domain_id'])


def downgrade() -> None:
    op.drop_index('ix_gtm_containers_domain', table_name='messaging_gtm_containers')
    op.drop_index('ix_gtm_containers_project', table_name='messaging_gtm_containers')
    op.drop_table('messaging_gtm_containers')
