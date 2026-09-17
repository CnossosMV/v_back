"""Add project_media_assets table

Revision ID: b5c6d7e8_051_media_assets
Revises: a4b5c6d7_050_phone_e164
Create Date: 2026-02-26

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'b5c6d7e8_051_media_assets'
down_revision = 'a4b5c6d7_050_phone_e164'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if table_exists('project_media_assets'):
        return

    op.create_table(
        'project_media_assets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('chatbot_id', sa.Integer(), nullable=True),
        sa.Column('specialist_id', sa.Integer(), nullable=True),
        sa.Column('label', sa.String(200), nullable=False),
        sa.Column('slug', sa.String(100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('media_type', sa.String(30), nullable=False),
        sa.Column('media_url', sa.String(1000), nullable=True),
        sa.Column('file_path', sa.String(500), nullable=True),
        sa.Column('file_name', sa.String(200), nullable=True),
        sa.Column('mime_type', sa.String(100), nullable=True),
        sa.Column('tags', sa.ARRAY(sa.String()), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['chatbot_id'], ['chatbots.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['specialist_id'], ['specialist_agents.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('project_id', 'slug', name='uq_media_asset_project_slug'),
    )
    op.create_index('ix_project_media_assets_id', 'project_media_assets', ['id'])
    op.create_index('ix_project_media_assets_project_id', 'project_media_assets', ['project_id'])
    op.create_index('ix_project_media_assets_chatbot_id', 'project_media_assets', ['chatbot_id'])
    op.create_index('ix_project_media_assets_specialist_id', 'project_media_assets', ['specialist_id'])
    op.create_index('ix_media_asset_scope', 'project_media_assets', ['project_id', 'chatbot_id', 'specialist_id'])


def downgrade() -> None:
    op.drop_table('project_media_assets')
