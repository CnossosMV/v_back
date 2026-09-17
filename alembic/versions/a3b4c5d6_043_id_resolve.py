"""043 identity resolution tables

Revision ID: a3b4c5d6_043_id_resolve
Revises: z3b4c5d6_042_inbound_rtr
Create Date: 2026-02-28 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'a3b4c5d6_043_id_resolve'
down_revision = 'z3b4c5d6_042_inbound_rtr'
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


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    # ================================================================
    # 1. New columns on messaging_users
    # ================================================================
    if not column_exists('messaging_users', 'status'):
        op.add_column('messaging_users', sa.Column(
            'status', sa.String(20), nullable=False, server_default='active'
        ))
    if not column_exists('messaging_users', 'merged_into'):
        op.add_column('messaging_users', sa.Column(
            'merged_into', sa.Integer(),
            sa.ForeignKey('messaging_users.id', ondelete='SET NULL'),
            nullable=True
        ))
    if not column_exists('messaging_users', 'lifecycle_stage'):
        op.add_column('messaging_users', sa.Column(
            'lifecycle_stage', sa.String(30), nullable=True
        ))
    if not column_exists('messaging_users', 'tags'):
        op.add_column('messaging_users', sa.Column(
            'tags', sa.JSON(), nullable=True
        ))
    if not column_exists('messaging_users', 'primary_channel'):
        op.add_column('messaging_users', sa.Column(
            'primary_channel', sa.JSON(), nullable=True
        ))
    if not column_exists('messaging_users', 'created_via'):
        op.add_column('messaging_users', sa.Column(
            'created_via', sa.String(30), nullable=False, server_default='api'
        ))
    if not column_exists('messaging_users', 'consent_channels'):
        op.add_column('messaging_users', sa.Column(
            'consent_channels', JSONB(), nullable=True
        ))

    # Index for excluding merged/deleted in queries
    if not index_exists('ix_messaging_users_status', 'messaging_users'):
        op.create_index(
            'ix_messaging_users_status',
            'messaging_users',
            ['project_id', 'status']
        )

    # ================================================================
    # 2. contact_identities table
    # ================================================================
    if not table_exists('contact_identities'):
        op.create_table(
            'contact_identities',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('project_id', sa.Integer(),
                       sa.ForeignKey('projects.id', ondelete='CASCADE'),
                       nullable=False, index=True),
            sa.Column('user_id', sa.Integer(),
                       sa.ForeignKey('messaging_users.id', ondelete='CASCADE'),
                       nullable=False),
            sa.Column('identity_type', sa.String(30), nullable=False),
            sa.Column('identity_value', sa.String(500), nullable=False),
            sa.Column('channel_instance_id', sa.Integer(), nullable=True),
            sa.Column('verified', sa.Boolean(), server_default='false',
                       nullable=False),
            sa.Column('source', sa.String(30), nullable=False),
            sa.Column('created_at', sa.DateTime(),
                       server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint('project_id', 'identity_type',
                                'identity_value',
                                name='uq_contact_identity_type_value'),
            sa.Index('ix_contact_identities_user',
                      'project_id', 'user_id'),
        )

    # ================================================================
    # 3. contact_merge_logs table
    # ================================================================
    if not table_exists('contact_merge_logs'):
        op.create_table(
            'contact_merge_logs',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('project_id', sa.Integer(),
                       sa.ForeignKey('projects.id', ondelete='CASCADE'),
                       nullable=False),
            sa.Column('winner_id', sa.Integer(),
                       sa.ForeignKey('messaging_users.id', ondelete='SET NULL'),
                       nullable=True),
            sa.Column('loser_id', sa.Integer(),
                       sa.ForeignKey('messaging_users.id', ondelete='SET NULL'),
                       nullable=True),
            sa.Column('triggered_by', sa.String(30), nullable=False),
            sa.Column('triggered_by_user_id', sa.Integer(),
                       sa.ForeignKey('users.id', ondelete='SET NULL'),
                       nullable=True),
            sa.Column('snapshot_winner', JSONB(), nullable=False),
            sa.Column('snapshot_loser', JSONB(), nullable=False),
            sa.Column('identities_transferred', JSONB(), nullable=True),
            sa.Column('properties_resolved', JSONB(), nullable=True),
            sa.Column('created_at', sa.DateTime(),
                       server_default=sa.func.now(), nullable=False),
            sa.Index('ix_merge_logs_project_date',
                      'project_id', 'created_at'),
        )

    # ================================================================
    # 4. accounts table
    # ================================================================
    if not table_exists('accounts'):
        op.create_table(
            'accounts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('project_id', sa.Integer(),
                       sa.ForeignKey('projects.id', ondelete='CASCADE'),
                       nullable=False, index=True),
            sa.Column('external_id', sa.String(255), nullable=False),
            sa.Column('name', sa.String(255), nullable=True),
            sa.Column('properties', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(),
                       server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(),
                       server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint('project_id', 'external_id',
                                name='uq_account_project_external'),
        )

    # ================================================================
    # 5. contact_account_assoc table
    # ================================================================
    if not table_exists('contact_account_assoc'):
        op.create_table(
            'contact_account_assoc',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('project_id', sa.Integer(),
                       sa.ForeignKey('projects.id', ondelete='CASCADE'),
                       nullable=False),
            sa.Column('user_id', sa.Integer(),
                       sa.ForeignKey('messaging_users.id', ondelete='CASCADE'),
                       nullable=False),
            sa.Column('account_id', sa.Integer(),
                       sa.ForeignKey('accounts.id', ondelete='CASCADE'),
                       nullable=False),
            sa.Column('role', sa.String(50), nullable=True),
            sa.Column('created_at', sa.DateTime(),
                       server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint('project_id', 'user_id', 'account_id',
                                name='uq_contact_account_assoc'),
        )

    # ================================================================
    # 6. merge_suggestions table
    # ================================================================
    if not table_exists('merge_suggestions'):
        op.create_table(
            'merge_suggestions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('project_id', sa.Integer(),
                       sa.ForeignKey('projects.id', ondelete='CASCADE'),
                       nullable=False),
            sa.Column('contact_a_id', sa.Integer(),
                       sa.ForeignKey('messaging_users.id', ondelete='CASCADE'),
                       nullable=False),
            sa.Column('contact_b_id', sa.Integer(),
                       sa.ForeignKey('messaging_users.id', ondelete='CASCADE'),
                       nullable=False),
            sa.Column('match_reason', sa.String(100), nullable=False),
            sa.Column('match_confidence', sa.String(20), nullable=False),
            sa.Column('status', sa.String(20), server_default='pending',
                       nullable=False),
            sa.Column('reviewed_by', sa.Integer(),
                       sa.ForeignKey('users.id', ondelete='SET NULL'),
                       nullable=True),
            sa.Column('reviewed_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(),
                       server_default=sa.func.now(), nullable=False),
            sa.Index('ix_merge_suggestions_status',
                      'project_id', 'status'),
        )

    # ================================================================
    # 7. Update FunnelEnrollment status constraint
    # ================================================================
    # Drop old constraint and add new one including 'merged_out'
    try:
        op.drop_constraint('valid_enrollment_status',
                           'funnel_enrollments', type_='check')
    except Exception:
        pass
    op.create_check_constraint(
        'valid_enrollment_status',
        'funnel_enrollments',
        "status IN ('active', 'completed', 'exited', 'merged_out')"
    )


def downgrade() -> None:
    # Drop new tables
    op.drop_table('merge_suggestions')
    op.drop_table('contact_account_assoc')
    op.drop_table('accounts')
    op.drop_table('contact_merge_logs')
    op.drop_table('contact_identities')

    # Drop new columns from messaging_users
    op.drop_index('ix_messaging_users_status', 'messaging_users')
    op.drop_column('messaging_users', 'consent_channels')
    op.drop_column('messaging_users', 'created_via')
    op.drop_column('messaging_users', 'primary_channel')
    op.drop_column('messaging_users', 'tags')
    op.drop_column('messaging_users', 'lifecycle_stage')
    op.drop_column('messaging_users', 'merged_into')
    op.drop_column('messaging_users', 'status')

    # Restore old enrollment constraint
    try:
        op.drop_constraint('valid_enrollment_status',
                           'funnel_enrollments', type_='check')
    except Exception:
        pass
    op.create_check_constraint(
        'valid_enrollment_status',
        'funnel_enrollments',
        "status IN ('active', 'completed', 'exited')"
    )
