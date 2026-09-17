"""Quarantine dup Meta pages + unique active idx

Revision ID: 129_meta_page_uq
Revises: 128_dynamic_us_monthly_intro
Create Date: 2026-07-18 10:00:00.000000

Multi-tenant ownership rule: one ACTIVE meta_page_connections row per
page_id (and per ig_account_id) globally. Existing duplicates are NOT
auto-resolved — every conflicting active row is quarantined
(is_active=false, status='quarantined') so the webhook safely no-ops
until the confirmed owning tenant reconnects via OAuth.

Downgrade drops the indexes only; quarantined rows are not reactivated
(intentional — reactivation requires human confirmation of ownership).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "129_meta_page_uq"
down_revision = "128_dynamic_us_monthly_intro"
branch_labels = None
depends_on = None

QUARANTINE_MSG = (
    "Quarantined: page connected in multiple projects - "
    "reconnect in the owning project"
)


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _index_exists(index_name: str, table_name: str) -> bool:
    indexes = inspect(op.get_bind()).get_indexes(table_name)
    return any(idx["name"] == index_name for idx in indexes)


def upgrade() -> None:
    if not _table_exists("meta_page_connections"):
        return

    bind = op.get_bind()

    # 1) Quarantine ALL active rows whose page_id / ig_account_id is
    #    active in more than one connection. No winner is auto-picked.
    quarantined = bind.execute(
        sa.text(
            """
            UPDATE meta_page_connections
            SET is_active = false,
                status = 'quarantined',
                last_error = :msg
            WHERE is_active = true
              AND (
                page_id IN (
                    SELECT page_id FROM meta_page_connections
                    WHERE is_active = true
                    GROUP BY page_id HAVING COUNT(*) > 1
                )
                OR (
                  ig_account_id IS NOT NULL
                  AND ig_account_id IN (
                    SELECT ig_account_id FROM meta_page_connections
                    WHERE is_active = true AND ig_account_id IS NOT NULL
                    GROUP BY ig_account_id HAVING COUNT(*) > 1
                  )
                )
              )
            RETURNING id, project_id, page_id, ig_account_id
            """
        ),
        {"msg": QUARANTINE_MSG},
    ).fetchall()
    for row in quarantined:
        print(
            f"[129_meta_page_uq] QUARANTINED connection id={row[0]} "
            f"project_id={row[1]} page_id={row[2]} ig_account_id={row[3]}"
        )

    # 2) Partial unique indexes (PostgreSQL only; app-level checks cover
    #    other dialects, e.g. the SQLite test DB).
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_page_conn_active_page "
            "ON meta_page_connections (page_id) WHERE is_active"
        )
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_page_conn_active_ig "
            "ON meta_page_connections (ig_account_id) "
            "WHERE is_active AND ig_account_id IS NOT NULL"
        )


def downgrade() -> None:
    if not _table_exists("meta_page_connections"):
        return
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_meta_page_conn_active_page")
        op.execute("DROP INDEX IF EXISTS uq_meta_page_conn_active_ig")
    # Quarantined rows intentionally left inactive (see module docstring).
