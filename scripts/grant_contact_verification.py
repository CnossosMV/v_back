"""Idempotently configure workspace contact-verification access by user email."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import User
from app.models.messaging import WorkspaceVerificationEntitlement


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-email", required=True)
    parser.add_argument(
        "--mode",
        choices=["disabled", "unmetered", "credits"],
        default="unmetered",
    )
    parser.add_argument("--notes", default="Administrative rollout")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        email = args.user_email.strip().lower()
        user = db.query(User).filter(User.email == email, User.is_active == True).first()
        if not user or not user.workspace_id:
            raise SystemExit(f"Active workspace user not found: {email}")
        row = db.query(WorkspaceVerificationEntitlement).filter(
            WorkspaceVerificationEntitlement.workspace_id == user.workspace_id,
        ).first()
        action = "updated"
        if not row:
            action = "created"
            row = WorkspaceVerificationEntitlement(workspace_id=user.workspace_id)
            db.add(row)
        row.access_mode = args.mode
        row.is_active = args.mode != "disabled"
        row.granted_by_user_id = user.id
        row.notes = args.notes
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print(
            f"{'applied' if args.apply else 'dry-run'}: {action} workspace={user.workspace_id} "
            f"resolved_by={email} mode={args.mode}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
