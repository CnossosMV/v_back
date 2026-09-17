"""014_add_user_consent

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-01-18 10:01:00.000000

Adds consent and PII hash fields to messaging_users table.
Enables GDPR/LGPD compliant consent tracking.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f7a8b9c0d1e2'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add PII hash columns
    op.add_column(
        'messaging_users',
        sa.Column('email_hash', sa.String(64), nullable=True)
    )
    op.add_column(
        'messaging_users',
        sa.Column('phone_hash', sa.String(64), nullable=True)
    )

    # Add consent columns
    op.add_column(
        'messaging_users',
        sa.Column('consent_marketing', sa.Boolean(), nullable=True, default=False)
    )
    op.add_column(
        'messaging_users',
        sa.Column('consent_analytics', sa.Boolean(), nullable=True, default=False)
    )
    op.add_column(
        'messaging_users',
        sa.Column('consent_given_at', sa.DateTime(), nullable=True)
    )
    op.add_column(
        'messaging_users',
        sa.Column('consent_ip', sa.String(45), nullable=True)
    )
    op.add_column(
        'messaging_users',
        sa.Column('consent_version', sa.String(20), nullable=True)
    )

    # Create indexes for hash lookups
    op.create_index(
        'ix_messaging_users_email_hash',
        'messaging_users',
        ['email_hash']
    )
    op.create_index(
        'ix_messaging_users_phone_hash',
        'messaging_users',
        ['phone_hash']
    )

    # Create composite indexes for project-scoped lookups
    op.create_index(
        'ix_msg_users_proj_email_hash',
        'messaging_users',
        ['project_id', 'email_hash']
    )
    op.create_index(
        'ix_msg_users_proj_phone_hash',
        'messaging_users',
        ['project_id', 'phone_hash']
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index('ix_msg_users_proj_phone_hash', table_name='messaging_users')
    op.drop_index('ix_msg_users_proj_email_hash', table_name='messaging_users')
    op.drop_index('ix_messaging_users_phone_hash', table_name='messaging_users')
    op.drop_index('ix_messaging_users_email_hash', table_name='messaging_users')

    # Drop columns
    op.drop_column('messaging_users', 'consent_version')
    op.drop_column('messaging_users', 'consent_ip')
    op.drop_column('messaging_users', 'consent_given_at')
    op.drop_column('messaging_users', 'consent_analytics')
    op.drop_column('messaging_users', 'consent_marketing')
    op.drop_column('messaging_users', 'phone_hash')
    op.drop_column('messaging_users', 'email_hash')
