"""077 template folder column

Revision ID: f6g7h8i9_077_tpl_folder
Revises: e5f6a7b8_076_proj_vars
Create Date: 2026-03-14 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'f6g7h8i9_077_tpl_folder'
down_revision = 'e5f6a7b8_076_proj_vars'
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
    if not column_exists('messaging_templates', 'folder'):
        op.add_column(
            'messaging_templates',
            sa.Column('folder', sa.String(100), nullable=True)
        )

    if not index_exists('ix_msg_tpl_proj_folder', 'messaging_templates'):
        op.create_index(
            'ix_msg_tpl_proj_folder',
            'messaging_templates',
            ['project_id', 'folder']
        )


def downgrade() -> None:
    if index_exists('ix_msg_tpl_proj_folder', 'messaging_templates'):
        op.drop_index('ix_msg_tpl_proj_folder', table_name='messaging_templates')

    if column_exists('messaging_templates', 'folder'):
        op.drop_column('messaging_templates', 'folder')
