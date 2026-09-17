"""009_add_whitelist_signups_table

Revision ID: a2b3c4d5e6f7
Revises: 71c0c263c580
Create Date: 2025-12-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a2b3c4d5e6f7'
down_revision = '71c0c263c580'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create whitelist_signups table
    op.create_table(
        'whitelist_signups',
        sa.Column('id', sa.Integer(), nullable=False),

        # Contact information
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('phone', sa.String(length=50), nullable=True),
        sa.Column('country', sa.String(length=100), nullable=True),

        # Additional fields
        sa.Column('name', sa.String(length=200), nullable=True),
        sa.Column('company', sa.String(length=200), nullable=True),
        sa.Column('suggestion', sa.Text(), nullable=True),

        # Tracking
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('referrer', sa.String(length=500), nullable=True),
        sa.Column('utm_source', sa.String(length=100), nullable=True),
        sa.Column('utm_medium', sa.String(length=100), nullable=True),
        sa.Column('utm_campaign', sa.String(length=100), nullable=True),

        # Status
        sa.Column('is_verified', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('verified_at', sa.DateTime(), nullable=True),

        # Timestamps
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),

        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index(op.f('ix_whitelist_signups_id'), 'whitelist_signups', ['id'], unique=False)
    op.create_index(op.f('ix_whitelist_signups_email'), 'whitelist_signups', ['email'], unique=False)


def downgrade() -> None:
    # Drop indexes
    op.drop_index(op.f('ix_whitelist_signups_email'), table_name='whitelist_signups')
    op.drop_index(op.f('ix_whitelist_signups_id'), table_name='whitelist_signups')

    # Drop table
    op.drop_table('whitelist_signups')
