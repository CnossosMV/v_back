"""Configure destination attribution enrichment for an explicit project."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.messaging import DestinationType, MessagingDestination
from app.services.messaging.destination_config import destination_config


ADS_TYPES = {
    DestinationType.meta_pixel,
    DestinationType.google_ads,
    DestinationType.tiktok,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument(
        "--destination-type",
        choices=[item.value for item in ADS_TYPES],
        required=True,
    )
    parser.add_argument(
        "--mode",
        choices=["current_only", "historical"],
        default="historical",
    )
    diagnostic = parser.add_mutually_exclusive_group()
    diagnostic.add_argument("--diagnostic-only", action="store_true", dest="diagnostic_only")
    diagnostic.add_argument("--payload-enabled", action="store_false", dest="diagnostic_only")
    parser.set_defaults(diagnostic_only=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        destination_type = DestinationType(args.destination_type)
        rows = db.query(MessagingDestination).filter(
            MessagingDestination.project_id == args.project_id,
            MessagingDestination.destination_type == destination_type,
            MessagingDestination.is_active == True,
        ).all()
        for row in rows:
            config = destination_config.decrypt_config(row.config_encrypted) or {}
            config["attribution_enrichment"] = args.mode
            config["attribution_diagnostic_only"] = args.diagnostic_only
            row.config_encrypted = destination_config.encrypt_config(config)

        if args.apply:
            db.commit()
        else:
            db.rollback()
        mode = "applied" if args.apply else "dry-run"
        print(
            f"{mode}: project={args.project_id} destination_type={args.destination_type} "
            f"destinations={len(rows)} enrichment={args.mode} "
            f"diagnostic_only={args.diagnostic_only}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
