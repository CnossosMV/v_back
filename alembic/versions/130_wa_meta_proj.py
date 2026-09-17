"""Project-scope Meta Cloud WA instances + phone uniq idx

Revision ID: 130_wa_meta_proj
Revises: 129_meta_page_uq
Create Date: 2026-07-18 11:00:00.000000

Backfills project_id for meta_cloud_api WhatsAppInstance rows:
  1. Unambiguous handler_channel_links -> handler's project.
  2. Workspace with exactly one active project -> that project.
  3. Otherwise QUARANTINE (is_active=false, no auto-assignment) — the
     tenant reconnects inside the correct project; the instance row and
     its per-instance webhook URL are preserved.

Also enforces one ACTIVE meta_cloud_api instance per phone_number_id
globally (partial unique index, PostgreSQL only).

Run scripts/report_wa_meta_projects.py BEFORE applying in production.
Downgrade drops the index only (backfill/quarantine not reverted).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "130_wa_meta_proj"
down_revision = "129_meta_page_uq"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _table_exists("whatsapp_instances"):
        return

    bind = op.get_bind()

    # 1) Backfill from handler_channel_links (most recent whatsapp link)
    bind.execute(
        sa.text(
            """
            UPDATE whatsapp_instances wi
            SET project_id = sub.handler_project_id
            FROM (
                SELECT wi2.id AS instance_id,
                       (
                           SELECT COALESCE(c.project_id, at.project_id)
                           FROM handler_channel_links hcl
                           LEFT JOIN chatbots c
                             ON hcl.handler_type = 'chatbot' AND hcl.handler_id = c.id
                           LEFT JOIN agent_teams at
                             ON hcl.handler_type = 'agent_team' AND hcl.handler_id = at.id
                           WHERE hcl.channel = 'whatsapp' AND hcl.instance_id = wi2.id
                           ORDER BY hcl.created_at DESC
                           LIMIT 1
                       ) AS handler_project_id
                FROM whatsapp_instances wi2
                WHERE wi2.provider_type = 'meta_cloud_api'
                  AND wi2.project_id IS NULL
            ) sub
            WHERE wi.id = sub.instance_id
              AND sub.handler_project_id IS NOT NULL
            """
        )
    )

    # 2) Workspace with exactly one active project
    bind.execute(
        sa.text(
            """
            UPDATE whatsapp_instances wi
            SET project_id = sub.only_project_id
            FROM (
                SELECT wi2.id AS instance_id,
                       (
                           SELECT MIN(p.id) FROM projects p
                           WHERE p.workspace_id = wi2.workspace_id
                             AND p.is_active = true
                       ) AS only_project_id
                FROM whatsapp_instances wi2
                WHERE wi2.provider_type = 'meta_cloud_api'
                  AND wi2.project_id IS NULL
                  AND (
                      SELECT COUNT(*) FROM projects p
                      WHERE p.workspace_id = wi2.workspace_id
                        AND p.is_active = true
                  ) = 1
            ) sub
            WHERE wi.id = sub.instance_id
              AND sub.only_project_id IS NOT NULL
            """
        )
    )

    # 3) Quarantine still-ambiguous rows — never guess between projects
    quarantined = bind.execute(
        sa.text(
            """
            UPDATE whatsapp_instances
            SET is_active = false,
                connection_status = 'disconnected'
            WHERE provider_type = 'meta_cloud_api'
              AND project_id IS NULL
              AND is_active = true
            RETURNING id, instance_name, workspace_id
            """
        )
    ).fetchall()
    for row in quarantined:
        print(
            f"[130_wa_meta_proj] QUARANTINED meta instance id={row[0]} "
            f"name={row[1]!r} workspace={row[2]} — reconnect inside a project"
        )

    # 4) One active meta_cloud_api instance per phone_number_id (PG only;
    #    app-level 409 covers other dialects)
    if bind.dialect.name == "postgresql":
        # Deactivate duplicate active phone ids first (keep none active if
        # conflicting across projects — quarantine semantics, no winner)
        dup = bind.execute(
            sa.text(
                """
                UPDATE whatsapp_instances
                SET is_active = false,
                    connection_status = 'disconnected'
                WHERE provider_type = 'meta_cloud_api'
                  AND is_active = true
                  AND meta_phone_number_id IN (
                      SELECT meta_phone_number_id FROM whatsapp_instances
                      WHERE provider_type = 'meta_cloud_api'
                        AND is_active = true
                        AND meta_phone_number_id IS NOT NULL
                      GROUP BY meta_phone_number_id
                      HAVING COUNT(*) > 1
                  )
                RETURNING id, meta_phone_number_id
                """
            )
        ).fetchall()
        for row in dup:
            print(
                f"[130_wa_meta_proj] QUARANTINED duplicate phone instance "
                f"id={row[0]} phone_number_id={row[1]}"
            )
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_wa_meta_phone_active "
            "ON whatsapp_instances (meta_phone_number_id) "
            "WHERE is_active AND provider_type = 'meta_cloud_api'"
        )


def downgrade() -> None:
    if not _table_exists("whatsapp_instances"):
        return
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_wa_meta_phone_active")
    # Backfilled project_id values and quarantines intentionally kept.
