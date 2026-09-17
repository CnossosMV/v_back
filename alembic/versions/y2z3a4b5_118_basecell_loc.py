"""Add locale to base_cell_messages (i18n content variant)

Revision ID: y2z3a4b5_118_basecell_loc
Revises: x1y2z3a4_117_loc_chan
Create Date: 2026-06-16

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'y2z3a4b5_118_basecell_loc'
down_revision = 'x1y2z3a4_117_loc_chan'
branch_labels = None
depends_on = None


def _columns(table: str):
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "base_cell_messages" not in inspect(op.get_bind()).get_table_names():
        return
    if "locale" not in _columns("base_cell_messages"):
        op.add_column("base_cell_messages", sa.Column("locale", sa.String(length=10), nullable=True))
    # Backfill: prefer the Meta template_language (pt_BR → pt-BR), else the project default.
    op.execute("""
        UPDATE base_cell_messages b
           SET locale = COALESCE(
                 b.locale,
                 NULLIF(REPLACE(b.template_language, '_', '-'), ''),
                 p.default_locale
               )
          FROM projects p
         WHERE b.project_id = p.id
           AND b.locale IS NULL
    """)


def downgrade() -> None:
    if "base_cell_messages" in inspect(op.get_bind()).get_table_names() \
            and "locale" in _columns("base_cell_messages"):
        op.drop_column("base_cell_messages", "locale")
