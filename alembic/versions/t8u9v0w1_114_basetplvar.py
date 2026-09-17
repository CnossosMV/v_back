"""Add template_components to base_cell_messages (template variables)

Revision ID: t8u9v0w1_114_basetplvar
Revises: s7t8u9v0_113_basetpl
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

revision = 't8u9v0w1_114_basetplvar'
down_revision = 's7t8u9v0_113_basetpl'
branch_labels = None
depends_on = None


def _columns(table: str):
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "template_components" not in _columns("base_cell_messages"):
        op.add_column("base_cell_messages", sa.Column("template_components", JSONB(), nullable=True))


def downgrade() -> None:
    if "template_components" in _columns("base_cell_messages"):
        op.drop_column("base_cell_messages", "template_components")
