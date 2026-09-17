"""PostgreSQL smoke for the campaign-readiness administrative backfill."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from uuid import uuid4

from app.database import SessionLocal
from app.models import ContactLedger, Project, ProjectPolicy, SendLog, User, Workspace
from app.models.campaigns import ContactEndpoint, ContactPermissionEvidence
from app.models.messaging import ContactVerificationState, MessagingUser
from app.routers.engine import _counts as engine_counts
from app.services.channels.send_service import SendService
from scripts.prepare_campaign_rollout import prepare


def main() -> None:
    if not os.getenv("DATABASE_URL", "").startswith("postgresql"):
        raise RuntimeError("This smoke requires an explicitly selected PostgreSQL database")
    db = SessionLocal()
    suffix = uuid4().hex[:10]
    now = datetime.utcnow().replace(microsecond=0)
    try:
        owner = User(email=f"prep-{suffix}@example.test", name="Prep smoke", is_active=True)
        db.add(owner); db.flush()
        workspace = Workspace(name=f"Prep {suffix}", owner_id=owner.id, is_active=True)
        db.add(workspace); db.flush(); owner.workspace_id = workspace.id
        project = Project(name=f"Tabloide {suffix}", workspace_id=workspace.id, is_active=True)
        db.add(project); db.flush()
        db.add(ProjectPolicy(
            project_id=project.id,
            is_active=True,
            contact_caps={"monthly": {"email": 10}},
            channel_cooldowns={"email": 3600},
        ))

        contact = MessagingUser(
            project_id=project.id,
            external_id=f"subscriber-{suffix}",
            email=f"subscriber-{suffix}@example.test",
            is_subscribed=True,
            status="active",
        )
        withdrawn = MessagingUser(
            project_id=project.id,
            external_id=f"withdrawn-{suffix}",
            email=f"withdrawn-{suffix}@example.test",
            is_subscribed=True,
            status="active",
        )
        db.add_all([contact, withdrawn]); db.flush()
        endpoint = ContactEndpoint(
            project_id=project.id, user_id=contact.id, endpoint_type="email",
            value=contact.email, normalized_value=contact.email,
            value_hash="a" * 64, is_primary=True, status="active", source="smoke",
        )
        withdrawn_endpoint = ContactEndpoint(
            project_id=project.id, user_id=withdrawn.id, endpoint_type="email",
            value=withdrawn.email, normalized_value=withdrawn.email,
            value_hash="b" * 64, is_primary=True, status="active", source="smoke",
        )
        db.add_all([endpoint, withdrawn_endpoint]); db.flush()
        db.add(ContactPermissionEvidence(
            project_id=project.id, user_id=withdrawn.id,
            endpoint_id=withdrawn_endpoint.id, channel="email",
            permission_type="marketing", status="withdrawn", source="contact",
            captured_at=now - timedelta(hours=1),
        ))
        db.add(ContactVerificationState(
            project_id=project.id, user_id=contact.id, endpoint_id=endpoint.id,
            verification_type="email", identifier_hash=endpoint.value_hash,
            provider="smoke", canonical_status="valid", provider_status="ok",
            checked_at=now, expires_at=now + timedelta(days=60),
            last_attempt_status="succeeded", last_attempt_at=now,
        ))
        send_log = SendLog(
            project_id=project.id, user_id=contact.id, channel="email",
            recipient=contact.email, content_type="text", source_type="event_action",
            source_id=42, status="delivered", queued_at=now - timedelta(hours=2),
            sent_at=now - timedelta(hours=2), delivered_at=now - timedelta(hours=2, minutes=-1),
        )
        db.add(send_log); db.flush()
        legacy = ContactLedger(
            project_id=project.id, user_id=contact.id, channel="email",
            source="event_action", source_id="42", sent_at=send_log.sent_at,
        )
        db.add(legacy); db.commit()

        preview = prepare(
            db, actor=owner, project=project,
            attestation_ref=f"subscriber-attestation-{suffix}",
            attestation_note="All active contacts were subscribers who consented.",
            apply=False, activate=True, now=now,
        )
        assert preview["consent"]["new_evidence_endpoints"] == 1
        assert preview["consent"]["excluded_existing_denial_or_withdrawal"] == 1
        assert preview["ledger"]["canonicalize_existing"] == 1
        assert preview["readiness_before"]["campaign_eligible_contacts"] == 0

        applied = prepare(
            db, actor=owner, project=project,
            attestation_ref=f"subscriber-attestation-{suffix}",
            attestation_note="All active contacts were subscribers who consented.",
            apply=True, activate=True, now=now,
        )
        assert applied["readiness_after"]["campaign_eligible_contacts"] == 1
        db.refresh(legacy)
        assert legacy.source_id == str(send_log.id)
        assert db.query(ContactPermissionEvidence).filter(
            ContactPermissionEvidence.endpoint_id == endpoint.id,
            ContactPermissionEvidence.status == "granted",
        ).count() == 1
        assert all(value == "enforce" for value in applied["effective_rollouts"].values())

        # The logical source id is intentionally reused; each actual provider
        # submission must still receive its own ledger entry via SendLog.id.
        second_log = SendLog(
            project_id=project.id, user_id=contact.id, channel="email",
            recipient=contact.email, content_type="text", source_type="event_action",
            source_id=42, status="sent", queued_at=now - timedelta(minutes=30),
            sent_at=now - timedelta(minutes=30),
        )
        db.add(second_log); db.flush()
        SendService(db)._record_ledger_once(
            project_id=project.id, user_id=contact.id, channel="email",
            source_type="event_action", source_id=42,
            send_log_id=second_log.id, sent_at=second_log.sent_at,
        )
        db.commit()
        parity = engine_counts(db, project.id)["ledger_parity"]
        assert parity["expected"] == 2
        assert parity["matched"] == 2
        assert parity["ratio"] == 1.0

        replay = prepare(
            db, actor=owner, project=project,
            attestation_ref=f"subscriber-attestation-{suffix}",
            attestation_note="All active contacts were subscribers who consented.",
            apply=True, activate=True, now=now,
        )
        assert replay["consent"].get("new_evidence_endpoints", 0) == 0
        assert replay["ledger"].get("new_ledger_rows", 0) == 0
        assert db.query(ContactLedger).filter(ContactLedger.project_id == project.id).count() == 2
        print("campaign-rollout-preparation-smoke-ok")
    finally:
        db.close()


if __name__ == "__main__":
    main()
