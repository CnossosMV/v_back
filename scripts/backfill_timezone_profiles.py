"""Backfill contact and anonymous profile timezone from existing event snapshots."""
import json

from app.database import SessionLocal
from app.models.messaging import (
    MessagingAnonymousProfile,
    MessagingEvent,
    MessagingUser,
)
from app.services.messaging.timezone_context import (
    apply_timezone_to_properties,
    apply_timezone_to_user,
    normalize_timezone_context,
)


def main() -> None:
    db = SessionLocal()
    updated_users = set()
    updated_anonymous = set()
    try:
        events = (
            db.query(MessagingEvent)
            .filter(MessagingEvent.properties.isnot(None))
            .order_by(MessagingEvent.created_at.desc(), MessagingEvent.id.desc())
            .yield_per(1000)
        )
        for event in events:
            properties = event.properties or {}
            snapshot = normalize_timezone_context(
                None,
                properties,
                trusted_properties=True,
                received_at=event.created_at,
            )
            if not snapshot:
                continue

            if event.user_id and event.user_id not in updated_users:
                user = db.query(MessagingUser).filter(
                    MessagingUser.id == event.user_id
                ).one_or_none()
                if user:
                    apply_timezone_to_user(user, snapshot)
                    updated_users.add(event.user_id)

            anonymous_key = (event.project_id, event.anonymous_id)
            if event.anonymous_id and anonymous_key not in updated_anonymous:
                profile = db.query(MessagingAnonymousProfile).filter(
                    MessagingAnonymousProfile.project_id == event.project_id,
                    MessagingAnonymousProfile.anonymous_id == event.anonymous_id,
                ).one_or_none()
                if profile:
                    profile.properties = apply_timezone_to_properties(
                        profile.properties,
                        snapshot,
                    )
                    updated_anonymous.add(anonymous_key)

        db.commit()
        print(json.dumps({
            "users_updated": len(updated_users),
            "anonymous_profiles_updated": len(updated_anonymous),
        }, sort_keys=True))
    finally:
        db.close()


if __name__ == "__main__":
    main()
