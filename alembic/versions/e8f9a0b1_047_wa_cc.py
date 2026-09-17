"""047 add default_country_code to WA

Revision ID: e8f9a0b1_047_wa_cc
Revises: d7e8f9a0_046_send_wa_step
Create Date: 2026-02-24 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'e8f9a0b1_047_wa_cc'
down_revision = 'd7e8f9a0_046_send_wa_step'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('whatsapp_instances', 'default_country_code'):
        op.add_column(
            'whatsapp_instances',
            sa.Column('default_country_code', sa.String(5), nullable=True),
        )


def downgrade() -> None:
    if column_exists('whatsapp_instances', 'default_country_code'):
        op.drop_column('whatsapp_instances', 'default_country_code')
