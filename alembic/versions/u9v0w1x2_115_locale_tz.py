"""Add locale + timezone to projects and messaging_users

Revision ID: u9v0w1x2_115_locale_tz
Revises: t8u9v0w1_114_basetplvar
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'u9v0w1x2_115_locale_tz'
down_revision = 't8u9v0w1_114_basetplvar'
branch_labels = None
depends_on = None


def _columns(table: str):
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    proj = _columns("projects")
    if "default_locale" not in proj:
        op.add_column("projects",
                      sa.Column("default_locale", sa.String(length=10), nullable=False, server_default="pt-BR"))
    if "supported_locales" not in proj:
        # null ⇒ app treats as [default_locale]
        op.add_column("projects", sa.Column("supported_locales", sa.JSON(), nullable=True))
    if "default_timezone" not in proj:
        op.add_column("projects",
                      sa.Column("default_timezone", sa.String(length=40), nullable=False,
                                server_default="America/Sao_Paulo"))

    users = _columns("messaging_users")
    if "locale" not in users:
        op.add_column("messaging_users", sa.Column("locale", sa.String(length=10), nullable=True))
    if "timezone" not in users:
        op.add_column("messaging_users", sa.Column("timezone", sa.String(length=40), nullable=True))


def downgrade() -> None:
    users = _columns("messaging_users")
    for name in ("timezone", "locale"):
        if name in users:
            op.drop_column("messaging_users", name)
    proj = _columns("projects")
    for name in ("default_timezone", "supported_locales", "default_locale"):
        if name in proj:
            op.drop_column("projects", name)
