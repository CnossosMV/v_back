"""
One-off script to re-normalize existing phone_e164 values after fixing
PhoneNormalizer step order (locale/fallback now checked before len>=11).

Fixes rows where phone_e164 is missing the country code prefix —
e.g. "11963776966" → "5511963776966" for Brazilian numbers.

Also patches contact_routing_states and chat_sessions that used the
old (wrong) phone value as their contact identifier.

Usage:
    # Dry run (default) — shows what would change
    python scripts/renormalize_phones.py

    # Apply changes
    python scripts/renormalize_phones.py --apply
"""

import argparse
import os
import sys

# Add backend root to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import WhatsAppInstance, Project
from app.models.messaging import MessagingUser
from app.services.messaging.phone_normalizer import PhoneNormalizer


def get_project_fallback_cc(db, project_id: int, cache: dict) -> str | None:
    """Get default_country_code from project's first active WhatsApp instance."""
    if project_id in cache:
        return cache[project_id]

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        cache[project_id] = None
        return None

    instance = db.query(WhatsAppInstance).filter(
        WhatsAppInstance.workspace_id == project.workspace_id,
        WhatsAppInstance.is_active == True,
        WhatsAppInstance.default_country_code != None,
    ).first()

    cc = instance.default_country_code if instance else None
    cache[project_id] = cc
    return cc


def main():
    parser = argparse.ArgumentParser(description="Re-normalize phone_e164 values")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes (default is dry run)")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is required")
        sys.exit(1)

    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    cc_cache: dict[int, str | None] = {}
    fixed_users = 0
    fixed_routing = 0
    fixed_sessions = 0
    skipped = 0

    try:
        # ── Phase 1: Re-normalize messaging_users.phone_e164 ──
        users = db.query(MessagingUser).filter(
            MessagingUser.phone != None,
            MessagingUser.phone != "",
        ).all()

        print(f"Found {len(users)} users with phone numbers")

        identifier_map: dict[tuple[int, str], str] = {}  # (project_id, old_e164) → new_e164

        for user in users:
            fallback_cc = get_project_fallback_cc(db, user.project_id, cc_cache)

            # Re-normalize using the raw phone (the original input)
            # We pass locale=None because we don't have the original locale stored,
            # but fallback_cc covers the main case
            new_e164, new_status = PhoneNormalizer.normalize(
                user.phone,
                locale=None,
                fallback_country_code=fallback_cc,
            )

            old_e164 = user.phone_e164

            if new_e164 == old_e164 and new_status == user.phone_norm_status:
                skipped += 1
                continue

            if old_e164 and new_e164 and old_e164 != new_e164:
                identifier_map[(user.project_id, old_e164)] = new_e164

            action = "APPLY" if args.apply else "DRY"
            print(f"  [{action}] User {user.id} (project {user.project_id}): "
                  f"phone={user.phone} | {old_e164} ({user.phone_norm_status}) → "
                  f"{new_e164} ({new_status})")

            if args.apply:
                user.phone_e164 = new_e164
                user.phone_norm_status = new_status

            fixed_users += 1

        if args.apply and fixed_users > 0:
            db.flush()

        # ── Phase 2: Patch contact_routing_states ──
        if identifier_map:
            print(f"\nPatching contact_routing_states for {len(identifier_map)} identifier changes...")

            for (project_id, old_id), new_id in identifier_map.items():
                result = db.execute(
                    text("""
                        UPDATE contact_routing_states
                        SET contact_identifier = :new_id
                        WHERE project_id = :project_id
                          AND contact_identifier = :old_id
                    """),
                    {"new_id": new_id, "project_id": project_id, "old_id": old_id}
                ) if args.apply else None

                # Count matches for dry run
                count_result = db.execute(
                    text("""
                        SELECT COUNT(*) FROM contact_routing_states
                        WHERE project_id = :project_id
                          AND contact_identifier = :old_id
                    """),
                    {"project_id": project_id, "old_id": old_id}
                )
                count = count_result.scalar() if not args.apply else (result.rowcount if result else 0)

                if count and count > 0:
                    action = "APPLY" if args.apply else "DRY"
                    print(f"  [{action}] project {project_id}: {old_id} → {new_id} ({count} rows)")
                    fixed_routing += count

            # ── Phase 3: Patch chat_sessions.user_identifier ──
            print(f"\nPatching chat_sessions for {len(identifier_map)} identifier changes...")

            for (project_id, old_id), new_id in identifier_map.items():
                # chat_sessions doesn't have project_id directly, but chatbot does
                # Use a join through chatbot → project
                if args.apply:
                    result = db.execute(
                        text("""
                            UPDATE chat_sessions
                            SET user_identifier = :new_id
                            WHERE user_identifier = :old_id
                              AND channel = 'whatsapp'
                              AND chatbot_id IN (
                                  SELECT id FROM chatbots WHERE project_id = :project_id
                              )
                        """),
                        {"new_id": new_id, "old_id": old_id, "project_id": project_id}
                    )
                    count = result.rowcount
                else:
                    count_result = db.execute(
                        text("""
                            SELECT COUNT(*) FROM chat_sessions
                            WHERE user_identifier = :old_id
                              AND channel = 'whatsapp'
                              AND chatbot_id IN (
                                  SELECT id FROM chatbots WHERE project_id = :project_id
                              )
                        """),
                        {"old_id": old_id, "project_id": project_id}
                    )
                    count = count_result.scalar()

                if count and count > 0:
                    action = "APPLY" if args.apply else "DRY"
                    print(f"  [{action}] chat_sessions project {project_id}: {old_id} → {new_id} ({count} rows)")
                    fixed_sessions += count

        if args.apply:
            db.commit()
            print("\nChanges committed.")
        else:
            db.rollback()
            print("\nDry run complete — no changes made. Use --apply to commit.")

        print(f"\nSummary:")
        print(f"  Users re-normalized:       {fixed_users}")
        print(f"  Routing states patched:    {fixed_routing}")
        print(f"  Chat sessions patched:     {fixed_sessions}")
        print(f"  Users unchanged (skipped): {skipped}")

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
