"""add first-party tracking domains

Revision ID: h7i8j9k0_102_tracking_domains
Revises: g7h8i9j0_101_merge_ads
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = 'h7i8j9k0_102_tracking_domains'
down_revision = 'g7h8i9j0_101_merge_ads'
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _create_index_once(index_name: str, table_name: str, columns: list[str], unique: bool = False) -> None:
    if _table_exists(table_name) and not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def _drop_index_once(index_name: str, table_name: str) -> None:
    if _table_exists(table_name) and _index_exists(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    if not _table_exists('messaging_tracking_domains'):
        op.create_table(
            'messaging_tracking_domains',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('domain_id', sa.Integer(), nullable=True),
            sa.Column('hostname', sa.String(length=255), nullable=False),
            sa.Column('mode', sa.String(length=50), server_default='managed_cname', nullable=False),
            sa.Column('cname_target', sa.String(length=255), nullable=False),
            sa.Column('verification_token', sa.String(length=100), nullable=False),
            sa.Column('cloudflare_custom_hostname_id', sa.String(length=255), nullable=True),
            sa.Column('dns_status', sa.String(length=50), server_default='pending', nullable=False),
            sa.Column('ssl_status', sa.String(length=50), server_default='pending', nullable=False),
            sa.Column('proxy_status', sa.String(length=50), server_default='pending', nullable=False),
            sa.Column('cookie_keeper_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(), nullable=True),
            sa.Column('config_version', sa.Integer(), server_default='1', nullable=False),
            sa.Column('status_details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
            sa.ForeignKeyConstraint(['domain_id'], ['messaging_domains.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('hostname'),
        )
    _create_index_once(op.f('ix_messaging_tracking_domains_id'), 'messaging_tracking_domains', ['id'])
    _create_index_once(op.f('ix_messaging_tracking_domains_hostname'), 'messaging_tracking_domains', ['hostname'])
    _create_index_once(op.f('ix_messaging_tracking_domains_project_id'), 'messaging_tracking_domains', ['project_id'])
    _create_index_once(op.f('ix_messaging_tracking_domains_domain_id'), 'messaging_tracking_domains', ['domain_id'])
    _create_index_once(op.f('ix_messaging_tracking_domains_verification_token'), 'messaging_tracking_domains', ['verification_token'])
    _create_index_once(op.f('ix_messaging_tracking_domains_cloudflare_custom_hostname_id'), 'messaging_tracking_domains', ['cloudflare_custom_hostname_id'])
    _create_index_once(op.f('ix_messaging_tracking_domains_dns_status'), 'messaging_tracking_domains', ['dns_status'])
    _create_index_once(op.f('ix_messaging_tracking_domains_ssl_status'), 'messaging_tracking_domains', ['ssl_status'])
    _create_index_once(op.f('ix_messaging_tracking_domains_proxy_status'), 'messaging_tracking_domains', ['proxy_status'])
    _create_index_once('ix_tracking_domains_project_hostname', 'messaging_tracking_domains', ['project_id', 'hostname'], unique=True)

    if not _table_exists('messaging_proxy_request_logs'):
        op.create_table(
            'messaging_proxy_request_logs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('tracking_domain_id', sa.Integer(), nullable=True),
            sa.Column('hostname', sa.String(length=255), nullable=False),
            sa.Column('method', sa.String(length=12), nullable=False),
            sa.Column('path', sa.String(length=500), nullable=False),
            sa.Column('action', sa.String(length=50), nullable=True),
            sa.Column('event_id', sa.String(length=255), nullable=True),
            sa.Column('anonymous_id', sa.String(length=100), nullable=True),
            sa.Column('status_code', sa.Integer(), nullable=True),
            sa.Column('click_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('cookies_refreshed', postgresql.ARRAY(sa.String()), nullable=True),
            sa.Column('request_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['tracking_domain_id'], ['messaging_tracking_domains.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
    _create_index_once(op.f('ix_messaging_proxy_request_logs_id'), 'messaging_proxy_request_logs', ['id'])
    _create_index_once(op.f('ix_messaging_proxy_request_logs_project_id'), 'messaging_proxy_request_logs', ['project_id'])
    _create_index_once(op.f('ix_messaging_proxy_request_logs_tracking_domain_id'), 'messaging_proxy_request_logs', ['tracking_domain_id'])
    _create_index_once(op.f('ix_messaging_proxy_request_logs_hostname'), 'messaging_proxy_request_logs', ['hostname'])
    _create_index_once(op.f('ix_messaging_proxy_request_logs_action'), 'messaging_proxy_request_logs', ['action'])
    _create_index_once(op.f('ix_messaging_proxy_request_logs_event_id'), 'messaging_proxy_request_logs', ['event_id'])
    _create_index_once(op.f('ix_messaging_proxy_request_logs_anonymous_id'), 'messaging_proxy_request_logs', ['anonymous_id'])
    _create_index_once(op.f('ix_messaging_proxy_request_logs_created_at'), 'messaging_proxy_request_logs', ['created_at'])
    _create_index_once('ix_proxy_logs_project_created', 'messaging_proxy_request_logs', ['project_id', 'created_at'])
    _create_index_once('ix_proxy_logs_tracking_created', 'messaging_proxy_request_logs', ['tracking_domain_id', 'created_at'])


def downgrade() -> None:
    _drop_index_once('ix_proxy_logs_tracking_created', 'messaging_proxy_request_logs')
    _drop_index_once('ix_proxy_logs_project_created', 'messaging_proxy_request_logs')
    _drop_index_once(op.f('ix_messaging_proxy_request_logs_created_at'), 'messaging_proxy_request_logs')
    _drop_index_once(op.f('ix_messaging_proxy_request_logs_anonymous_id'), 'messaging_proxy_request_logs')
    _drop_index_once(op.f('ix_messaging_proxy_request_logs_event_id'), 'messaging_proxy_request_logs')
    _drop_index_once(op.f('ix_messaging_proxy_request_logs_action'), 'messaging_proxy_request_logs')
    _drop_index_once(op.f('ix_messaging_proxy_request_logs_hostname'), 'messaging_proxy_request_logs')
    _drop_index_once(op.f('ix_messaging_proxy_request_logs_tracking_domain_id'), 'messaging_proxy_request_logs')
    _drop_index_once(op.f('ix_messaging_proxy_request_logs_project_id'), 'messaging_proxy_request_logs')
    _drop_index_once(op.f('ix_messaging_proxy_request_logs_id'), 'messaging_proxy_request_logs')
    if _table_exists('messaging_proxy_request_logs'):
        op.drop_table('messaging_proxy_request_logs')

    _drop_index_once('ix_tracking_domains_project_hostname', 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_proxy_status'), 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_ssl_status'), 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_dns_status'), 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_cloudflare_custom_hostname_id'), 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_verification_token'), 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_domain_id'), 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_project_id'), 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_hostname'), 'messaging_tracking_domains')
    _drop_index_once(op.f('ix_messaging_tracking_domains_id'), 'messaging_tracking_domains')
    if _table_exists('messaging_tracking_domains'):
        op.drop_table('messaging_tracking_domains')
