"""076 project variables

Revision ID: e5f6a7b8_076_proj_vars
Revises: d4e5f6a7_075_evt_proc_note
Create Date: 2026-03-13 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = 'e5f6a7b8_076_proj_vars'
down_revision = 'd4e5f6a7_075_evt_proc_note'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not table_exists('project_variables'):
        op.create_table(
            'project_variables',
            sa.Column('id', sa.Integer, primary_key=True, index=True),
            sa.Column('project_id', sa.Integer,
                      sa.ForeignKey('projects.id', ondelete='CASCADE'),
                      nullable=False, index=True),
            sa.Column('key', sa.String(100), nullable=False),
            sa.Column('var_type', sa.String(10), nullable=False,
                      server_default='text'),
            sa.Column('value', sa.Text, nullable=True),
            sa.Column('url_params', JSONB, nullable=True),
            sa.Column('description', sa.Text, nullable=True),
            sa.Column('created_at', sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
            sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(),
                      onupdate=sa.func.now(), nullable=False),
            sa.UniqueConstraint('project_id', 'key',
                                name='uq_proj_var_key'),
            sa.CheckConstraint("var_type IN ('text', 'url')",
                               name='ck_proj_var_type'),
        )


def downgrade() -> None:
    if table_exists('project_variables'):
        op.drop_table('project_variables')
