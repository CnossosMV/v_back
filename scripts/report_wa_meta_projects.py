"""
Read-only pre-migration report for 130_wa_meta_proj.

Lists every meta_cloud_api WhatsAppInstance with project_id IS NULL and
classifies how the migration would resolve it:

  - handler_link : unambiguous project via handler_channel_links
  - single_project : workspace has exactly one active project
  - AMBIGUOUS : would be QUARANTINED (deactivated, reconnect required)

Run BEFORE applying the migration (dev or prod):
    python -m scripts.report_wa_meta_projects
"""
import sys

from sqlalchemy import text

from app.database import SessionLocal


REPORT_SQL = """
SELECT
    wi.id,
    wi.instance_name,
    wi.meta_phone_number_id,
    wi.workspace_id,
    wi.is_active,
    (
        SELECT COUNT(*) FROM projects p
        WHERE p.workspace_id = wi.workspace_id AND p.is_active = true
    ) AS active_projects,
    (
        SELECT COALESCE(c.project_id, at.project_id)
        FROM handler_channel_links hcl
        LEFT JOIN chatbots c
          ON hcl.handler_type = 'chatbot' AND hcl.handler_id = c.id
        LEFT JOIN agent_teams at
          ON hcl.handler_type = 'agent_team' AND hcl.handler_id = at.id
        WHERE hcl.channel = 'whatsapp' AND hcl.instance_id = wi.id
        ORDER BY hcl.created_at DESC
        LIMIT 1
    ) AS handler_project_id
FROM whatsapp_instances wi
WHERE wi.provider_type = 'meta_cloud_api'
  AND wi.project_id IS NULL
ORDER BY wi.id
"""


def main() -> int:
    db = SessionLocal()
    try:
        rows = db.execute(text(REPORT_SQL)).fetchall()
        if not rows:
            print("OK: no meta_cloud_api instances with project_id IS NULL.")
            return 0

        ambiguous = 0
        print(f"{len(rows)} meta_cloud_api instance(s) with project_id IS NULL:\n")
        for r in rows:
            (iid, name, phone_id, ws_id, active, n_projects, handler_pid) = r
            if handler_pid is not None:
                cls = f"handler_link -> project {handler_pid}"
            elif n_projects == 1:
                cls = "single_project"
            else:
                cls = f"AMBIGUOUS ({n_projects} active projects) -> WILL BE QUARANTINED"
                ambiguous += 1
            print(
                f"  instance={iid} name={name!r} phone_number_id={phone_id} "
                f"workspace={ws_id} active={active} :: {cls}"
            )
        print(
            f"\nSummary: {len(rows)} unresolved, {ambiguous} would be quarantined. "
            "Review with the affected tenants before migrating."
        )
        return 1 if ambiguous else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
