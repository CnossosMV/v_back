"""
Backfill system funnels for existing EventAction rows.

Run after migration 097_unify_ea. Idempotent.
Usage:
    docker exec -w /app customer-backend python -m scripts.backfill_event_action_funnels
"""
import logging

from app.database import SessionLocal
from app.models import EventAction
from app.services.event_actions.funnel_compiler import compile_event_action

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    db = SessionLocal()
    try:
        rules = db.query(EventAction).all()
        logger.info("Found %d EventAction rows to backfill", len(rules))

        ok = 0
        failed = 0
        for rule in rules:
            try:
                compile_event_action(db, rule)
                db.commit()
                ok += 1
            except Exception as e:
                db.rollback()
                logger.error("Failed to compile EventAction %s: %s", rule.id, e)
                failed += 1

        logger.info("Backfill complete: %d ok, %d failed", ok, failed)
    finally:
        db.close()


if __name__ == "__main__":
    main()
