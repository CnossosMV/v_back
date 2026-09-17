"""Add project API connections

Revision ID: u8c9d0e1_037_api_conn
Revises: t7b8c9d0_036_meta_cloud
Create Date: 2026-02-13 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'u8c9d0e1_037_api_conn'
down_revision = 't7b8c9d0_036_meta_cloud'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # --- Table 1: project_api_connections ---
    if not table_exists('project_api_connections'):
        op.create_table(
            'project_api_connections',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('status', sa.String(20), server_default='active', nullable=False),
            sa.Column('base_url', sa.String(500), nullable=False),
            sa.Column('auth_type', sa.String(30), server_default='none', nullable=False),
            sa.Column('auth_config_encrypted', sa.Text(), nullable=True),
            sa.Column('auth_header_name', sa.String(100), nullable=True),
            sa.Column('auth_header_prefix', sa.String(50), nullable=True),
            sa.Column('default_headers', sa.JSON(), nullable=True),
            sa.Column('api_documentation', sa.Text(), nullable=True),
            sa.Column('parsed_spec', sa.JSON(), nullable=True),
            sa.Column('timeout_ms', sa.Integer(), server_default='30000', nullable=False),
            sa.Column('rate_limit_rpm', sa.Integer(), nullable=True),
            sa.Column('connection_metadata', sa.JSON(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
            sa.CheckConstraint("status IN ('active', 'inactive')", name='valid_api_conn_status'),
            sa.CheckConstraint(
                "auth_type IN ('api_key_header', 'bearer_token', 'basic_auth', 'custom_header', 'none')",
                name='valid_api_conn_auth_type',
            ),
        )
        op.create_index('ix_project_api_connections_id', 'project_api_connections', ['id'])
        op.create_index('ix_project_api_connections_project_id', 'project_api_connections', ['project_id'])
        op.create_index('ix_project_api_connections_status', 'project_api_connections', ['status'])

    # --- Table 2: api_conn_endpoints ---
    if not table_exists('api_conn_endpoints'):
        op.create_table(
            'api_conn_endpoints',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('connection_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('slug', sa.String(100), nullable=False),
            sa.Column('method', sa.String(10), nullable=False),
            sa.Column('path', sa.String(500), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('parameters_schema', sa.JSON(), nullable=True),
            sa.Column('request_body_schema', sa.JSON(), nullable=True),
            sa.Column('response_example', sa.JSON(), nullable=True),
            sa.Column('when_to_use', sa.Text(), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
            sa.Column('endpoint_metadata', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['connection_id'], ['project_api_connections.id'], ondelete='CASCADE'),
            sa.UniqueConstraint('connection_id', 'slug', name='uq_api_conn_endpoint_slug'),
        )
        op.create_index('ix_api_conn_endpoints_id', 'api_conn_endpoints', ['id'])
        op.create_index('ix_api_conn_endpoints_connection_id', 'api_conn_endpoints', ['connection_id'])

    # --- Table 3: api_conn_executions ---
    if not table_exists('api_conn_executions'):
        op.create_table(
            'api_conn_executions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('connection_id', sa.Integer(), nullable=False),
            sa.Column('endpoint_id', sa.Integer(), nullable=True),
            sa.Column('trigger_source', sa.String(30), nullable=False),
            sa.Column('trigger_source_id', sa.String(100), nullable=True),
            sa.Column('session_id', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(20), nullable=False),
            sa.Column('request_data', sa.JSON(), nullable=True),
            sa.Column('response_data', sa.JSON(), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('duration_ms', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['connection_id'], ['project_api_connections.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['endpoint_id'], ['api_conn_endpoints.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='SET NULL'),
        )
        op.create_index('ix_api_conn_executions_id', 'api_conn_executions', ['id'])
        op.create_index('ix_api_conn_executions_connection_id', 'api_conn_executions', ['connection_id'])
        op.create_index('ix_api_conn_executions_endpoint_id', 'api_conn_executions', ['endpoint_id'])
        op.create_index('ix_api_conn_executions_trigger_source', 'api_conn_executions', ['trigger_source'])
        op.create_index('ix_api_conn_executions_status', 'api_conn_executions', ['status'])
        op.create_index('ix_api_conn_executions_created_at', 'api_conn_executions', ['created_at'])

    # --- Add context_sources column to agent_teams ---
    if not column_exists('agent_teams', 'context_sources'):
        op.add_column('agent_teams', sa.Column('context_sources', sa.JSON(), nullable=True))


def downgrade() -> None:
    # Remove column from agent_teams
    if column_exists('agent_teams', 'context_sources'):
        op.drop_column('agent_teams', 'context_sources')

    # Drop tables in reverse order
    if table_exists('api_conn_executions'):
        op.drop_table('api_conn_executions')
    if table_exists('api_conn_endpoints'):
        op.drop_table('api_conn_endpoints')
    if table_exists('project_api_connections'):
        op.drop_table('project_api_connections')
