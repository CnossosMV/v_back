"""Add locale variant fields to messaging_templates (template families)

Revision ID: w0x1y2z3_116_tpl_locale
Revises: u9v0w1x2_115_locale_tz
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'w0x1y2z3_116_tpl_locale'
down_revision = 'u9v0w1x2_115_locale_tz'
branch_labels = None
depends_on = None

_OLD_UQ = "ix_messaging_templates_project_slug"
_NEW_UQ = "ix_msg_tpl_proj_slug_locale"


def _columns(table: str):
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str):
    return {i["name"] for i in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    cols = _columns("messaging_templates")
    if "locale" not in cols:
        op.add_column("messaging_templates", sa.Column("locale", sa.String(length=10), nullable=True))
    if "source_locale" not in cols:
        op.add_column("messaging_templates", sa.Column("source_locale", sa.String(length=10), nullable=True))
    if "translation_status" not in cols:
        op.add_column("messaging_templates",
                      sa.Column("translation_status", sa.String(length=20), nullable=False, server_default="source"))
    if "translated_at" not in cols:
        op.add_column("messaging_templates", sa.Column("translated_at", sa.DateTime(), nullable=True))

    # Backfill existing rows to each project's default locale (idempotent).
    op.execute("""
        UPDATE messaging_templates t
           SET locale = COALESCE(t.locale, p.default_locale),
               source_locale = COALESCE(t.source_locale, p.default_locale)
          FROM projects p
         WHERE t.project_id = p.id
           AND (t.locale IS NULL OR t.source_locale IS NULL)
    """)

    idx = _indexes("messaging_templates")
    if _OLD_UQ in idx:
        op.drop_index(_OLD_UQ, table_name="messaging_templates")
    if _NEW_UQ not in idx:
        op.create_index(_NEW_UQ, "messaging_templates",
                        ["project_id", "slug", "locale"], unique=True)


def downgrade() -> None:
    idx = _indexes("messaging_templates")
    if _NEW_UQ in idx:
        op.drop_index(_NEW_UQ, table_name="messaging_templates")
    if _OLD_UQ not in idx:
        op.create_index(_OLD_UQ, "messaging_templates", ["project_id", "slug"], unique=True)
    cols = _columns("messaging_templates")
    for name in ("translated_at", "translation_status", "source_locale", "locale"):
        if name in cols:
            op.drop_column("messaging_templates", name)
