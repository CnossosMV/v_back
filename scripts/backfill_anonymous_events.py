"""
One-time backfill: attribute pre-identification events to their identified user.

Before the "instant identify match" fix, when versya.identify() was called the
MessagingAnonymousProfile got merged_to_user_id set, but the historical
messaging_events rows kept user_id = NULL. This script walks every merged
anonymous profile and sets user_id on its prior anonymous events.

After this runs, the journey of every previously-identified contact will
include the activity they had before they identified themselves.

Usage:
    docker exec customer-backend python scripts/backfill_anonymous_events.py
    docker exec customer-backend python scripts/backfill_anonymous_events.py --dry-run
    docker exec customer-backend python scripts/backfill_anonymous_events.py --project-id 2
"""
import sys
import os
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from app.database import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backfill_anon_events] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


COUNT_SQL = """
SELECT COUNT(*)
FROM messaging_events e
JOIN messaging_anonymous_profiles p
  ON p.anonymous_id = e.anonymous_id
 AND p.project_id   = e.project_id
WHERE e.user_id IS NULL
  AND p.merged_to_user_id IS NOT NULL
  {project_filter}
"""

UPDATE_SQL = """
UPDATE messaging_events AS e
SET user_id = p.merged_to_user_id
FROM messaging_anonymous_profiles AS p
WHERE e.anonymous_id = p.anonymous_id
  AND e.project_id   = p.project_id
  AND e.user_id IS NULL
  AND p.merged_to_user_id IS NOT NULL
  {project_filter}
"""


def run(project_id: int | None, dry_run: bool) -> None:
    project_filter = "AND e.project_id = :project_id" if project_id else ""
    params: dict = {}
    if project_id:
        params["project_id"] = project_id

    db = SessionLocal()
    try:
        affected = db.execute(
            text(COUNT_SQL.format(project_filter=project_filter)),
            params,
        ).scalar() or 0

        scope = f"project {project_id}" if project_id else "all projects"
        logger.info("Found %d orphaned anonymous events in %s", affected, scope)

        if affected == 0:
            logger.info("Nothing to backfill.")
            return

        if dry_run:
            logger.info("--dry-run: skipping update")
            return

        result = db.execute(
            text(UPDATE_SQL.format(project_filter=project_filter)),
            params,
        )
        db.commit()
        logger.info("Updated %d events", result.rowcount)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=int, default=None,
                        help="Limit backfill to a single project")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report counts without writing")
    args = parser.parse_args()
    run(args.project_id, args.dry_run)


if __name__ == "__main__":
    main()
