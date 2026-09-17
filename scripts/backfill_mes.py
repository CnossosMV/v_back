"""
One-time MES backfill script.

Run once after deployment to create MES records for all historical SendLogs
and compute scores with the current algorithm.

Usage:
    docker exec customer-backend-api-1 python scripts/backfill_mes.py
    docker exec customer-backend-api-1 python scripts/backfill_mes.py --days 90
    docker exec customer-backend-api-1 python scripts/backfill_mes.py --project-id 3
"""
import sys
import os
import argparse
import logging
from datetime import datetime, timedelta

# Ensure app modules are importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.database import SessionLocal
from app.models import SendLog, MessageEffectivenessScore
from sqlalchemy.orm import aliased

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backfill_mes] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_missing_records(db, project_id=None, days=90, batch_size=500):
    """Create MES rows for SendLogs that don't have one yet."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    mes_alias = aliased(MessageEffectivenessScore)

    q = (
        db.query(SendLog)
        .outerjoin(mes_alias, mes_alias.send_log_id == SendLog.id)
        .filter(
            SendLog.status.notin_(["queued"]),
            SendLog.queued_at >= cutoff,
            mes_alias.id.is_(None),
        )
    )
    if project_id:
        q = q.filter(SendLog.project_id == project_id)

    total_created = 0
    offset = 0

    while True:
        batch = q.order_by(SendLog.id).offset(offset).limit(batch_size).all()
        if not batch:
            break

        for sl in batch:
            mes = MessageEffectivenessScore(
                project_id=sl.project_id,
                send_log_id=sl.id,
                user_id=sl.user_id,
                channel=sl.channel or sl.resolved_channel or "unknown",
                template_id=sl.template_id,
                source_type=sl.source_type or "unknown",
                source_id=sl.source_id,
                stale=True,
            )
            db.add(mes)
            total_created += 1

        db.commit()
        logger.info(f"  Created {total_created} MES records so far...")
        offset += batch_size

    return total_created


def mark_all_stale(db, project_id=None):
    """Mark all existing MES records as stale for recomputation."""
    q = db.query(MessageEffectivenessScore)
    if project_id:
        q = q.filter(MessageEffectivenessScore.project_id == project_id)

    updated = q.update({"stale": True}, synchronize_session=False)
    db.commit()
    return updated


def compute_scores(db, project_id=None, batch_size=200):
    """Compute scores for all stale MES records."""
    from app.services.scoring.mes_engine import MESEngine

    q = db.query(MessageEffectivenessScore).filter(
        MessageEffectivenessScore.stale == True,
    )
    if project_id:
        q = q.filter(MessageEffectivenessScore.project_id == project_id)

    total = q.count()
    logger.info(f"  {total} stale records to compute")

    computed = 0
    errors = 0
    offset = 0

    while True:
        batch = (
            q.order_by(MessageEffectivenessScore.id)
            .offset(0)  # always 0 because stale=False removes them
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        engine = MESEngine(db)
        for mes in batch:
            try:
                engine.compute_score(mes.send_log_id)
                computed += 1
            except Exception as e:
                errors += 1
                if errors <= 10:
                    logger.error(f"  Error computing send_log {mes.send_log_id}: {e}")

        db.commit()
        logger.info(f"  Computed {computed}/{total} scores ({errors} errors)")

    return computed, errors


def main():
    parser = argparse.ArgumentParser(description="Backfill MES scores for historical messages")
    parser.add_argument("--days", type=int, default=90, help="How many days back to look (default: 90)")
    parser.add_argument("--project-id", type=int, default=None, help="Limit to a specific project")
    parser.add_argument("--skip-create", action="store_true", help="Skip creating new records, only recompute existing")
    parser.add_argument("--skip-compute", action="store_true", help="Only create records, don't compute scores")
    args = parser.parse_args()

    db = SessionLocal()

    try:
        scope = f"project {args.project_id}" if args.project_id else "all projects"
        logger.info(f"=== MES Backfill starting ({scope}, {args.days} days) ===")

        # Step 1: Create missing MES records
        if not args.skip_create:
            logger.info("Step 1: Creating MES records for SendLogs without one...")
            created = create_missing_records(db, args.project_id, args.days)
            logger.info(f"Step 1 done: {created} records created")
        else:
            logger.info("Step 1: Skipped (--skip-create)")

        # Step 2: Mark all stale
        logger.info("Step 2: Marking all MES records as stale...")
        staled = mark_all_stale(db, args.project_id)
        logger.info(f"Step 2 done: {staled} records marked stale")

        # Step 3: Compute scores
        if not args.skip_compute:
            logger.info("Step 3: Computing scores...")
            computed, errors = compute_scores(db, args.project_id)
            logger.info(f"Step 3 done: {computed} computed, {errors} errors")
        else:
            logger.info("Step 3: Skipped (--skip-compute). Scheduler will pick up stale records.")

        logger.info("=== MES Backfill complete ===")

    except Exception as e:
        logger.error(f"Backfill failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
