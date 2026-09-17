"""Backfill still-valid paid attribution touches for one project.

Dry-run is the default. This script never queues or replays destination deliveries.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.messaging import MessagingEvent, MessagingUser
from app.services.messaging.attribution_resolver import record_attribution_touches


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, min(args.days, 180)))
    db = SessionLocal()
    event_count = 0
    touch_ids = set()
    try:
        events = db.query(MessagingEvent).filter(
            MessagingEvent.project_id == args.project_id,
            MessagingEvent.created_at >= cutoff,
            MessagingEvent.attribution.isnot(None),
        ).order_by(MessagingEvent.created_at.asc(), MessagingEvent.id.asc()).yield_per(500)

        for event in events:
            user = db.query(MessagingUser).filter(
                MessagingUser.id == event.user_id,
                MessagingUser.project_id == args.project_id,
            ).first() if event.user_id else None
            touches = record_attribution_touches(
                db,
                event=event,
                user=user,
                anonymous_id=event.anonymous_id,
                require_valid_at=now,
            )
            if touches:
                event_count += 1
                touch_ids.update(
                    (touch.provider, touch.identifier_type, touch.identifier_hash)
                    for touch in touches
                )

        if args.apply:
            db.commit()
        else:
            db.rollback()
        mode = "applied" if args.apply else "dry-run"
        print(
            f"{mode}: project={args.project_id} events_with_touches={event_count} "
            f"deduplicated_touches={len(touch_ids)}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
