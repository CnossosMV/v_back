"""Promote project markets to first-class rows.

Revision ID: 154_project_markets
Revises: 153_project_setup
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "154_project_markets"
down_revision = "153_project_setup"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _index_exists(table: str, name: str) -> bool:
    return any(item["name"] == name for item in inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    if not _table_exists("project_markets"):
        op.create_table(
            "project_markets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "project_id",
                sa.Integer(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("key", sa.String(40), nullable=False),
            sa.Column("country_code", sa.String(2), nullable=False),
            sa.Column(
                "region_codes", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column("timezone", sa.String(64), nullable=False),
            sa.Column(
                "locales", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column(
                "calendar_tags", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("project_id", "key", name="uq_project_market_key"),
            sa.CheckConstraint(
                "status IN ('active','archived')", name="ck_project_market_status"
            ),
            sa.CheckConstraint(
                "is_default = false OR status = 'active'",
                name="ck_project_market_default_active",
            ),
        )

    for name, columns in (
        ("ix_project_markets_project_id", ["project_id"]),
        ("ix_project_markets_country_code", ["country_code"]),
        ("ix_project_markets_status", ["status"]),
        ("ix_project_markets_project_status", ["project_id", "status", "key"]),
    ):
        if not _index_exists("project_markets", name):
            op.create_index(name, "project_markets", columns)
    if not _index_exists("project_markets", "uq_project_markets_default"):
        op.create_index(
            "uq_project_markets_default",
            "project_markets",
            ["project_id"],
            unique=True,
            postgresql_where=sa.text("is_default = true"),
        )

    # Compatibility backfill. Invalid legacy entries are deliberately not
    # invented or repaired here; the existing foundation validator remains the
    # tenant-facing place to correct them.
    op.execute(sa.text("""
        INSERT INTO project_markets (
            project_id, key, country_code, region_codes, timezone, locales,
            calendar_tags, status, is_default
        )
        SELECT
            p.id,
            lower(m.item->>'key'),
            upper(m.item->>'country_code'),
            CASE WHEN jsonb_typeof(m.item->'region_codes') = 'array'
                 THEN m.item->'region_codes' ELSE '[]'::jsonb END,
            m.item->>'timezone',
            CASE WHEN jsonb_typeof(m.item->'locales') = 'array'
                 THEN m.item->'locales' ELSE '[]'::jsonb END,
            CASE WHEN jsonb_typeof(m.item->'calendar_tags') = 'array'
                 THEN m.item->'calendar_tags' ELSE '[]'::jsonb END,
            'active',
            lower(m.item->>'key') = lower(p.market_config->>'default_market_key')
        FROM projects p
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.market_config->'markets') = 'array'
                 THEN p.market_config->'markets' ELSE '[]'::jsonb END
        ) AS m(item)
        WHERE coalesce(m.item->>'key', '') <> ''
          AND length(coalesce(m.item->>'country_code', '')) = 2
          AND coalesce(m.item->>'timezone', '') <> ''
        ON CONFLICT (project_id, key) DO NOTHING
    """))


def downgrade() -> None:
    if _table_exists("project_markets"):
        op.drop_table("project_markets")
