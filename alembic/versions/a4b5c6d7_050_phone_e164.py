"""050 add phone_e164 to messaging_users

Revision ID: a4b5c6d7_050_phone_e164
Revises: g0a1b2c3_049_multi_route
Create Date: 2026-02-26 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'a4b5c6d7_050_phone_e164'
down_revision = 'g0a1b2c3_049_multi_route'
branch_labels = None
depends_on = None


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
    # Add phone_e164 column
    if not column_exists('messaging_users', 'phone_e164'):
        op.add_column(
            'messaging_users',
            sa.Column('phone_e164', sa.String(30), nullable=True),
        )

    # Add phone_norm_status column
    if not column_exists('messaging_users', 'phone_norm_status'):
        op.add_column(
            'messaging_users',
            sa.Column('phone_norm_status', sa.String(20), nullable=True),
        )

    # Composite index for project + phone_e164 lookups
    if not index_exists('ix_msg_users_proj_phone_e164', 'messaging_users'):
        op.create_index(
            'ix_msg_users_proj_phone_e164',
            'messaging_users',
            ['project_id', 'phone_e164'],
        )

    # Backfill existing rows:
    # - Phone starts with '+' or cleaned digits >= 11 → copy to phone_e164, status = valid_e164
    # - Shorter non-empty phone → status = missing
    op.execute(sa.text("""
        UPDATE messaging_users
        SET phone_e164 = regexp_replace(phone, '[^0-9]', '', 'g'),
            phone_norm_status = 'valid_e164'
        WHERE phone IS NOT NULL
          AND phone != ''
          AND (
              phone LIKE '+%'
              OR length(regexp_replace(phone, '[^0-9]', '', 'g')) >= 11
          )
    """))

    op.execute(sa.text("""
        UPDATE messaging_users
        SET phone_norm_status = 'missing'
        WHERE phone IS NOT NULL
          AND phone != ''
          AND phone_norm_status IS NULL
    """))


def downgrade() -> None:
    if index_exists('ix_msg_users_proj_phone_e164', 'messaging_users'):
        op.drop_index('ix_msg_users_proj_phone_e164', table_name='messaging_users')
    if column_exists('messaging_users', 'phone_norm_status'):
        op.drop_column('messaging_users', 'phone_norm_status')
    if column_exists('messaging_users', 'phone_e164'):
        op.drop_column('messaging_users', 'phone_e164')
