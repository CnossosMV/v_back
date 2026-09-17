"""011_add_messaging_security

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-01-18 00:00:00.000000

Adds security enhancement fields to messaging tables:
- messaging_api_keys.allowed_ips: IP allowlist for backend API keys
- messaging_domains.rate_limit_per_minute: Rate limit per minute for domain SDK
- messaging_domains.rate_limit_per_day: Rate limit per day for domain SDK
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add allowed_ips column to messaging_api_keys
    # Supports IPv4, IPv6, and CIDR notation (e.g., 192.168.1.0/24)
    op.add_column(
        'messaging_api_keys',
        sa.Column('allowed_ips', postgresql.ARRAY(sa.String(45)), nullable=True)
    )

    # Add rate limit columns to messaging_domains
    op.add_column(
        'messaging_domains',
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=True, server_default='100')
    )
    op.add_column(
        'messaging_domains',
        sa.Column('rate_limit_per_day', sa.Integer(), nullable=True, server_default='10000')
    )


def downgrade() -> None:
    # Remove rate limit columns from messaging_domains
    op.drop_column('messaging_domains', 'rate_limit_per_day')
    op.drop_column('messaging_domains', 'rate_limit_per_minute')

    # Remove allowed_ips column from messaging_api_keys
    op.drop_column('messaging_api_keys', 'allowed_ips')
