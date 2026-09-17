"""Add system LLM providers, usage tracking, chatbot embedding model

Revision ID: l3m4n5o6_082_llm_multi
Revises: k2l3m4n5_081_email_inst
Create Date: 2026-03-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'l3m4n5o6_082_llm_multi'
down_revision = 'k2l3m4n5_081_email_inst'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    # 1. system_llm_providers
    if not table_exists('system_llm_providers'):
        op.create_table(
            'system_llm_providers',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('provider', sa.String(50), nullable=False),
            sa.Column('api_key_encrypted', sa.Text(), nullable=False),
            sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
            sa.Column('rate_limit_rpm', sa.Integer(), nullable=True),
            sa.Column('rate_limit_tpm', sa.Integer(), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('provider', name='uq_system_llm_provider'),
        )
        op.create_index('ix_system_llm_providers_provider', 'system_llm_providers', ['provider'])

    # 2. llm_usage_records
    if not table_exists('llm_usage_records'):
        op.create_table(
            'llm_usage_records',
            sa.Column('id', sa.BigInteger(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('provider', sa.String(50), nullable=False),
            sa.Column('model', sa.String(100), nullable=False),
            sa.Column('purpose', sa.String(50), nullable=False),
            sa.Column('key_source', sa.String(20), nullable=False),
            sa.Column('input_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
            sa.Column('output_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
            sa.Column('total_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
            sa.Column('cost_estimate', sa.Float(), server_default=sa.text('0.0'), nullable=False),
            sa.Column('call_count', sa.Integer(), server_default=sa.text('1'), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_llm_usage_proj_date', 'llm_usage_records', ['project_id', 'created_at'])
        op.create_index('ix_llm_usage_proj_provider', 'llm_usage_records', ['project_id', 'provider'])

    # 3. chatbots.embedding_model
    if table_exists('chatbots') and not column_exists('chatbots', 'embedding_model'):
        op.add_column('chatbots', sa.Column('embedding_model', sa.String(100), nullable=True))


def downgrade() -> None:
    if column_exists('chatbots', 'embedding_model'):
        op.drop_column('chatbots', 'embedding_model')
    if table_exists('llm_usage_records'):
        op.drop_table('llm_usage_records')
    if table_exists('system_llm_providers'):
        op.drop_table('system_llm_providers')
