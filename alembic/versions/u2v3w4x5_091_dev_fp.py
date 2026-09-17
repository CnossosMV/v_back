"""Add device_fingerprint to anonymous profiles

Revision ID: u2v3w4x5_091_dev_fp
Revises: t1u2v3w4_090_ref_tkn
Create Date: 2026-03-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'u2v3w4x5_091_dev_fp'
down_revision = 't1u2v3w4_090_ref_tkn'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    indexes = insp.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade() -> None:
    if not column_exists('messaging_anonymous_profiles', 'device_fingerprint'):
        op.add_column(
            'messaging_anonymous_profiles',
            sa.Column('device_fingerprint', sa.String(20), nullable=True)
        )

    if not index_exists('ix_msg_anon_proj_devfp', 'messaging_anonymous_profiles'):
        op.create_index(
            'ix_msg_anon_proj_devfp',
            'messaging_anonymous_profiles',
            ['project_id', 'device_fingerprint']
        )


def downgrade() -> None:
    if index_exists('ix_msg_anon_proj_devfp', 'messaging_anonymous_profiles'):
        op.drop_index('ix_msg_anon_proj_devfp', table_name='messaging_anonymous_profiles')

    if column_exists('messaging_anonymous_profiles', 'device_fingerprint'):
        op.drop_column('messaging_anonymous_profiles', 'device_fingerprint')
