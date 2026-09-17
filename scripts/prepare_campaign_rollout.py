"""Prepare one project for audited promotional campaigns.

The command is dry-run by default.  It resolves the tenant from an active
administrator email plus project name, materializes endpoint-specific consent
evidence from an explicit administrator attestation, canonicalizes/backfills
the recent contact ledger from SendLog, and optionally enables the project
rollouts required by the campaign engine.

No consent timestamp is invented: ``captured_at`` is the time the historical
attestation was recorded, while metadata explicitly says that the original
consent time is unknown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import ContactLedger, Project, ProjectPolicy, SendLog, User
from app.models.campaigns import (
    ChannelDeliveryProfile,
    ContactEndpoint,
    ContactPermissionEvidence,
)
from app.models.messaging import ContactVerificationState, MessagingUser
from app.services.engine_rollout_service import EngineRolloutService
from app.services.authorization_service import check_project_access, is_workspace_admin


SUBMITTED_STATUSES = {"sent", "delivered", "read", "opened", "submission_unknown"}
ROLLOUT_TARGETS = {
    "ledger": "enforce",
    "consent": "enforce",
    "guardian": "enforce",
    "pace": "enforce",
    "candidate": "enforce",
    "selection": "enforce",
    "supersede": "enforce",
    "locale_resolution": "enforce",
}


def _resolve_scope(db, user_email: str, project_name: str) -> tuple[User, Project]:
    email = user_email.strip().lower()
    actor = db.query(User).filter(
        func.lower(User.email) == email,
        User.is_active.is_(True),
    ).one_or_none()
    if not actor or not actor.workspace_id:
        raise ValueError(f"Active workspace user not found: {email}")
    projects = db.query(Project).filter(
        Project.workspace_id == actor.workspace_id,
        Project.is_active.is_(True),
        func.lower(Project.name) == project_name.strip().lower(),
    ).all()
    if len(projects) != 1:
        raise ValueError(
            f"Expected exactly one active project named {project_name!r} "
            f"in workspace {actor.workspace_id}; found {len(projects)}"
        )
    project = projects[0]
    if not is_workspace_admin(db, actor) and not check_project_access(
        db, actor.id, project.id, "admin",
    ):
        raise ValueError(
            f"User {email} is not an owner/admin of project {project.name!r}"
        )
    return actor, project


def _is_email_opted_out(user: MessagingUser) -> bool:
    return bool(
        user.global_opt_out
        or user.is_subscribed is False
        or "email" in (user.opted_out_channels or [])
    )


def _relevant_since(db, project_id: int, now: datetime) -> datetime:
    """Oldest timestamp still capable of affecting an active policy."""
    starts = [now - timedelta(days=7)]
    policy = db.query(ProjectPolicy).filter(ProjectPolicy.project_id == project_id).first()
    if not policy:
        return min(starts)
    caps = policy.contact_caps or {}
    if caps.get("monthly"):
        starts.append(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    if caps.get("weekly"):
        starts.append((now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ))
    if caps.get("daily"):
        starts.append(now.replace(hour=0, minute=0, second=0, microsecond=0))
    cooldowns = [int(value) for value in (policy.channel_cooldowns or {}).values() if value]
    if cooldowns:
        starts.append(now - timedelta(seconds=max(cooldowns)))
    priority_window = int((policy.priority_rules or {}).get("conflict_window_seconds") or 0)
    if priority_window:
        starts.append(now - timedelta(seconds=priority_window))
    return min(starts)


def _consent_plan(
    db,
    *,
    project_id: int,
    actor: User,
    attestation_ref: str,
    attestation_note: str,
    now: datetime,
) -> tuple[dict[str, Any], list[ContactPermissionEvidence], list[MessagingUser]]:
    users = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.status == "active",
        MessagingUser.is_sandbox.is_(False),
    ).all()
    endpoints = db.query(ContactEndpoint).filter(
        ContactEndpoint.project_id == project_id,
        ContactEndpoint.endpoint_type == "email",
        ContactEndpoint.status == "active",
    ).order_by(ContactEndpoint.user_id, ContactEndpoint.id).all()
    endpoints_by_user: dict[int, list[ContactEndpoint]] = defaultdict(list)
    for endpoint in endpoints:
        endpoints_by_user[endpoint.user_id].append(endpoint)

    evidence_rows = db.query(ContactPermissionEvidence).filter(
        ContactPermissionEvidence.project_id == project_id,
        ContactPermissionEvidence.channel == "email",
        ContactPermissionEvidence.permission_type == "marketing",
    ).order_by(
        ContactPermissionEvidence.user_id,
        ContactPermissionEvidence.captured_at.desc(),
        ContactPermissionEvidence.id.desc(),
    ).all()
    evidence_by_user: dict[int, list[ContactPermissionEvidence]] = defaultdict(list)
    for evidence in evidence_rows:
        evidence_by_user[evidence.user_id].append(evidence)

    ref_digest = hashlib.sha256(attestation_ref.encode("utf-8")).hexdigest()[:20]
    planned: list[ContactPermissionEvidence] = []
    cache_users: list[MessagingUser] = []
    counts = defaultdict(int)
    for user in users:
        user_endpoints = endpoints_by_user.get(user.id, [])
        if _is_email_opted_out(user):
            counts["excluded_opted_out"] += 1
            continue
        if not user.is_subscribed:
            counts["excluded_not_subscribed"] += 1
            continue
        if not user_endpoints:
            counts["excluded_missing_endpoint"] += 1
            continue

        endpoint_plans: list[ContactPermissionEvidence] = []
        blocked = False
        for endpoint in user_endpoints:
            applicable = [
                row for row in evidence_by_user.get(user.id, [])
                if row.endpoint_id in {None, endpoint.id}
            ]
            latest = applicable[0] if applicable else None
            if latest and latest.status in {"denied", "withdrawn"}:
                blocked = True
                counts["excluded_existing_denial_or_withdrawal"] += 1
                break
            exact_active = next((
                row for row in applicable
                if row.endpoint_id == endpoint.id
                and row.status == "granted"
                and (row.expires_at is None or row.expires_at > now)
            ), None)
            if exact_active:
                counts["already_evidenced_endpoints"] += 1
                continue
            evidence_ref = f"admin-attestation:{project_id}:{ref_digest}:{endpoint.id}"
            existing_ref = next((
                row for row in evidence_by_user.get(user.id, [])
                if row.evidence_ref == evidence_ref
            ), None)
            if existing_ref:
                if existing_ref.status == "granted":
                    counts["already_evidenced_endpoints"] += 1
                    continue
                blocked = True
                counts["excluded_existing_denial_or_withdrawal"] += 1
                break
            endpoint_plans.append(ContactPermissionEvidence(
                project_id=project_id,
                user_id=user.id,
                endpoint_id=endpoint.id,
                channel="email",
                permission_type="marketing",
                status="granted",
                source="admin_subscriber_attestation",
                policy_version="legacy-subscriber-attestation-v1",
                evidence_ref=evidence_ref,
                captured_at=now,
                evidence_metadata={
                    "legal_basis": "consent",
                    "attestation_ref": attestation_ref,
                    "attestation_note": attestation_note,
                    "attested_by_user_id": actor.id,
                    "attested_by_email": actor.email,
                    "recorded_at": now.isoformat(),
                    "original_consent_at_known": False,
                    "relationship_observed_at": endpoint.first_seen_at.isoformat(),
                    "migration": "campaign-readiness-v1",
                },
            ))
        if blocked:
            continue
        planned.extend(endpoint_plans)
        cache_users.append(user)
        counts["eligible_subscribers"] += 1
        counts["new_evidence_endpoints"] += len(endpoint_plans)

    return dict(counts), planned, cache_users


def _ledger_plan(db, *, project_id: int, since: datetime) -> tuple[dict[str, Any], list, list]:
    event_time = func.coalesce(SendLog.sent_at, SendLog.delivered_at, SendLog.queued_at)
    logs = db.query(SendLog).filter(
        SendLog.project_id == project_id,
        SendLog.user_id.isnot(None),
        SendLog.status.in_(sorted(SUBMITTED_STATUSES)),
        event_time >= since,
    ).order_by(event_time, SendLog.id).all()
    ledgers = db.query(ContactLedger).filter(
        ContactLedger.project_id == project_id,
        ContactLedger.sent_at >= since - timedelta(seconds=5),
    ).order_by(ContactLedger.sent_at, ContactLedger.id).all()
    exact = {
        (row.user_id, row.channel, row.source, row.source_id): row
        for row in ledgers if row.source_id
    }
    by_contact: dict[tuple[int, str], list[ContactLedger]] = defaultdict(list)
    for row in ledgers:
        by_contact[(row.user_id, row.channel)].append(row)

    used_ledger_ids: set[int] = set()
    create_rows: list[ContactLedger] = []
    canonicalize: list[tuple[ContactLedger, SendLog]] = []
    counts = defaultdict(int)
    counts["source_send_logs"] = len(logs)
    counts["relevant_since"] = since.isoformat()
    for log in logs:
        sent_at = log.sent_at or log.delivered_at or log.queued_at
        source = str(log.source_type or "send_log")
        if len(source) > 30:
            counts["excluded_source_too_long"] += 1
            continue
        exact_row = exact.get((log.user_id, log.channel, source, str(log.id)))
        if exact_row:
            used_ledger_ids.add(exact_row.id)
            counts["already_canonical"] += 1
            continue
        matches = [
            row for row in by_contact.get((log.user_id, log.channel), [])
            if row.id not in used_ledger_ids
            and abs((row.sent_at - sent_at).total_seconds()) <= 5
        ]
        if len(matches) == 1:
            row = matches[0]
            used_ledger_ids.add(row.id)
            canonicalize.append((row, log))
            counts["canonicalize_existing"] += 1
            continue
        if len(matches) > 1:
            counts["ambiguous_existing"] += 1
            continue
        create_rows.append(ContactLedger(
            project_id=project_id,
            user_id=log.user_id,
            channel=log.channel,
            source=source,
            source_id=str(log.id),
            sent_at=sent_at,
        ))
        counts["new_ledger_rows"] += 1
    return dict(counts), create_rows, canonicalize


def _readiness(db, *, project_id: int, now: datetime) -> dict[str, Any]:
    users = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.status == "active",
        MessagingUser.is_sandbox.is_(False),
    ).all()
    endpoints = db.query(ContactEndpoint).filter(
        ContactEndpoint.project_id == project_id,
        ContactEndpoint.endpoint_type == "email",
        ContactEndpoint.status == "active",
    ).all()
    granted = db.query(ContactPermissionEvidence).filter(
        ContactPermissionEvidence.project_id == project_id,
        ContactPermissionEvidence.channel == "email",
        ContactPermissionEvidence.permission_type == "marketing",
        ContactPermissionEvidence.status == "granted",
        (ContactPermissionEvidence.expires_at.is_(None) | (ContactPermissionEvidence.expires_at > now)),
    ).all()
    granted_endpoint_ids = {row.endpoint_id for row in granted if row.endpoint_id is not None}
    states = db.query(ContactVerificationState).filter(
        ContactVerificationState.project_id == project_id,
        ContactVerificationState.verification_type == "email",
        ContactVerificationState.canonical_status == "valid",
        ContactVerificationState.last_attempt_status == "succeeded",
        ContactVerificationState.expires_at > now,
    ).all()
    valid = {(row.user_id, row.endpoint_id, row.identifier_hash) for row in states}
    users_by_id = {row.id: row for row in users}
    eligible_users = {
        endpoint.user_id for endpoint in endpoints
        if endpoint.user_id in users_by_id
        and not _is_email_opted_out(users_by_id[endpoint.user_id])
        and endpoint.id in granted_endpoint_ids
        and (endpoint.user_id, endpoint.id, endpoint.value_hash) in valid
    }
    profiles = db.query(ChannelDeliveryProfile).filter(
        ChannelDeliveryProfile.project_id == project_id,
        ChannelDeliveryProfile.channel == "email",
        ChannelDeliveryProfile.status == "active",
    ).all()
    return {
        "active_contacts": len(users),
        "active_email_endpoints": len(endpoints),
        "granted_email_endpoints": len(granted_endpoint_ids),
        "fresh_valid_email_endpoints": len(valid),
        "campaign_eligible_contacts": len(eligible_users),
        "active_email_delivery_profiles": len(profiles),
        "healthy_email_delivery_profiles": sum(
            1 for row in profiles if row.health_status in {"healthy", "available"}
        ),
    }


def prepare(
    db,
    *,
    actor: User,
    project: Project,
    attestation_ref: str,
    attestation_note: str,
    apply: bool,
    activate: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.utcnow()
    if not attestation_ref.strip() or not attestation_note.strip():
        raise ValueError("Consent attestation reference and note must not be empty")
    since = _relevant_since(db, project.id, now)
    consent, evidence_rows, cache_users = _consent_plan(
        db,
        project_id=project.id,
        actor=actor,
        attestation_ref=attestation_ref,
        attestation_note=attestation_note,
        now=now,
    )
    ledger, ledger_rows, canonicalize = _ledger_plan(db, project_id=project.id, since=since)
    before = _readiness(db, project_id=project.id, now=now)

    rollout = EngineRolloutService(db)
    current = {item["feature_key"]: item for item in rollout.list_features(project.id)}
    updates = [
        {
            "feature_key": key,
            "mode": mode,
            "expected_version": int(current[key]["version"]),
        }
        for key, mode in ROLLOUT_TARGETS.items()
        if current[key]["project_mode"] != mode
    ]
    dependency_errors = rollout.validate(project.id, updates)
    result = {
        "mode": "apply" if apply else "preview",
        "project": {"id": project.id, "name": project.name, "workspace_id": project.workspace_id},
        "actor_user_id": actor.id,
        "consent": consent,
        "ledger": ledger,
        "readiness_before": before,
        "rollout": {
            "requested": ROLLOUT_TARGETS if activate else {},
            "updates": updates if activate else [],
            "dependency_errors": dependency_errors if activate else [],
        },
    }
    if not apply:
        db.rollback()
        return result

    for evidence in evidence_rows:
        db.add(evidence)
    for user in cache_users:
        channels = dict(user.consent_channels or {})
        channels["email"] = {
            "granted": True,
            "source": "admin_subscriber_attestation",
            "timestamp": now.isoformat(),
            "original_consent_at_known": False,
        }
        user.consent_channels = channels
        user.consent_marketing = True
        # MessagingUser.consent_version is a compact legacy cache (varchar 20);
        # the full policy identifier remains on the immutable evidence row.
        user.consent_version = "legacy-subscriber-v1"
    for row, log in canonicalize:
        row.source = str(log.source_type or "send_log")
        row.source_id = str(log.id)
        row.sent_at = log.sent_at or log.delivered_at or log.queued_at
    for row in ledger_rows:
        db.add(row)
    db.commit()

    if activate:
        if dependency_errors:
            raise RuntimeError(f"Rollout dependencies are not satisfied: {dependency_errors}")
        rollout.update(project.id, updates, actor.id)

    result["readiness_after"] = _readiness(db, project_id=project.id, now=now)
    result["effective_rollouts"] = {
        item["feature_key"]: item["effective_mode"]
        for item in rollout.list_features(project.id)
        if item["feature_key"] in ROLLOUT_TARGETS
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--attestation-ref", required=True)
    parser.add_argument("--attestation-note", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    if args.activate and not args.apply:
        parser.error("--activate requires --apply")

    db = SessionLocal()
    try:
        actor, project = _resolve_scope(db, args.user_email, args.project_name)
        result = prepare(
            db,
            actor=actor,
            project=project,
            attestation_ref=args.attestation_ref.strip(),
            attestation_note=args.attestation_note.strip(),
            apply=args.apply,
            activate=args.activate,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
