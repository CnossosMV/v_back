"""Add knowledge_assets, knowledge_collections, collection_assets, consumer_knowledge_bindings

Revision ID: h8d9e0f1_058_asset_lib
Revises: g7c8d9e0_057_push_subs
Create Date: 2026-03-01 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

# revision identifiers, used by Alembic.
revision = 'h8d9e0f1_058_asset_lib'
down_revision = 'g7c8d9e0_057_push_subs'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # ── knowledge_assets ─────────────────────────────────────────────────
    if not table_exists('knowledge_assets'):
        op.create_table(
            'knowledge_assets',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('asset_type', sa.String(20), nullable=False),
            sa.Column('name', sa.String(300), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('tags', ARRAY(sa.String), server_default='{}', nullable=False),
            sa.Column('language', sa.String(10), nullable=True),
            sa.Column('source_data', JSONB(), nullable=False, server_default='{}'),
            sa.Column('processing_status', sa.String(20), nullable=False, server_default='pending'),
            sa.Column('processing_error', sa.Text(), nullable=True),
            sa.Column('chunk_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('embedding_model', sa.String(100), nullable=True),
            sa.Column('last_processed_at', sa.DateTime(), nullable=True),
            sa.Column('extracted_text', sa.Text(), nullable=True),
            sa.Column('content_hash', sa.String(64), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('previous_version_id', sa.Integer(), sa.ForeignKey('knowledge_assets.id', ondelete='SET NULL'), nullable=True),
            sa.Column('status', sa.String(20), nullable=False, server_default='active'),
            sa.Column('created_by', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_knowledge_assets_project_id', 'knowledge_assets', ['project_id'])
        op.create_index('ix_ka_proj_status', 'knowledge_assets', ['project_id', 'status'])
        op.create_index('ix_ka_proj_type', 'knowledge_assets', ['project_id', 'asset_type'])

    # ── knowledge_collections ────────────────────────────────────────────
    if not table_exists('knowledge_collections'):
        op.create_table(
            'knowledge_collections',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('visibility', sa.String(20), nullable=False, server_default='selective'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('project_id', 'name', name='uq_kc_project_name'),
        )
        op.create_index('ix_knowledge_collections_project_id', 'knowledge_collections', ['project_id'])

    # ── collection_assets (M2M) ──────────────────────────────────────────
    if not table_exists('collection_assets'):
        op.create_table(
            'collection_assets',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('collection_id', sa.Integer(), sa.ForeignKey('knowledge_collections.id', ondelete='CASCADE'), nullable=False),
            sa.Column('asset_id', sa.Integer(), sa.ForeignKey('knowledge_assets.id', ondelete='CASCADE'), nullable=False),
            sa.Column('added_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('added_by', sa.String(200), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('collection_id', 'asset_id', name='uq_ca_coll_asset'),
        )
        op.create_index('ix_collection_assets_collection_id', 'collection_assets', ['collection_id'])
        op.create_index('ix_collection_assets_asset_id', 'collection_assets', ['asset_id'])

    # ── consumer_knowledge_bindings ──────────────────────────────────────
    if not table_exists('consumer_knowledge_bindings'):
        op.create_table(
            'consumer_knowledge_bindings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
            sa.Column('consumer_type', sa.String(30), nullable=False),
            sa.Column('consumer_id', sa.Integer(), nullable=False),
            sa.Column('collection_id', sa.Integer(), sa.ForeignKey('knowledge_collections.id', ondelete='CASCADE'), nullable=False),
            sa.Column('permission', sa.String(20), nullable=False, server_default='rag'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('consumer_type', 'consumer_id', 'collection_id', name='uq_ckb_consumer_coll'),
        )
        op.create_index('ix_ckb_consumer', 'consumer_knowledge_bindings', ['consumer_type', 'consumer_id'])
        op.create_index('ix_ckb_project_id', 'consumer_knowledge_bindings', ['project_id'])


def downgrade() -> None:
    for t in ('consumer_knowledge_bindings', 'collection_assets', 'knowledge_collections', 'knowledge_assets'):
        if table_exists(t):
            op.drop_table(t)
