"""016_add_dsar_requests

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-01-18 10:03:00.000000

Creates messaging_dsar_requests table for GDPR/LGPD
Data Subject Access Request tracking.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'b9c0d1e2f3a4'
down_revision = 'a8b9c0d1e2f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types using raw SQL with IF NOT EXISTS for reliability
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE dsar_request_type AS ENUM ('export', 'delete', 'rectify', 'withdraw_consent');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE dsar_request_status AS ENUM ('pending', 'processing', 'completed', 'failed');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Use postgresql.ENUM with create_type=False for reliable behavior
    request_type_col = postgresql.ENUM(
        'export', 'delete', 'rectify', 'withdraw_consent',
        name='dsar_request_type',
        create_type=False
    )
    request_status_col = postgresql.ENUM(
        'pending', 'processing', 'completed', 'failed',
        name='dsar_request_status',
        create_type=False
    )

    op.create_table(
        'messaging_dsar_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('request_type', request_type_col, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('external_id', sa.String(255), nullable=True),
        sa.Column('email_hash', sa.String(64), nullable=True),
        sa.Column('status', request_status_col, nullable=False, server_default='pending'),
        sa.Column('requested_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('result_url', sa.String(500), nullable=True),
        sa.Column('result_data', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('requested_by_ip', sa.String(45), nullable=True),
        sa.Column('processed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['messaging_users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['processed_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index('ix_dsar_requests_project_id', 'messaging_dsar_requests', ['project_id'])
    op.create_index('ix_dsar_requests_status', 'messaging_dsar_requests', ['status'])
    op.create_index('ix_dsar_requests_user_id', 'messaging_dsar_requests', ['user_id'])
    op.create_index('ix_dsar_requests_email_hash', 'messaging_dsar_requests', ['email_hash'])


def downgrade() -> None:
    op.drop_index('ix_dsar_requests_email_hash', table_name='messaging_dsar_requests')
    op.drop_index('ix_dsar_requests_user_id', table_name='messaging_dsar_requests')
    op.drop_index('ix_dsar_requests_status', table_name='messaging_dsar_requests')
    op.drop_index('ix_dsar_requests_project_id', table_name='messaging_dsar_requests')
    op.drop_table('messaging_dsar_requests')

    # Drop enums using raw SQL for reliability
    op.execute("DROP TYPE IF EXISTS dsar_request_status")
    op.execute("DROP TYPE IF EXISTS dsar_request_type")
