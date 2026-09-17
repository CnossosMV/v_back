"""Auditable, one-run exceptions for soft campaign pacing rules."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import ProjectPolicy
from app.models.campaigns import CampaignPolicyOverride, CampaignRun


SOFT_OVERRIDE_RULES = frozenset({
    "channel_cooldown",
    "contact_caps",
    "quiet_hours",
})
IMMUTABLE_GUARDS = (
    "consent",
    "opt_out",
    "deliverability",
    "provider_capacity",
    "priority",
    "suppression",
)


class CampaignOverrideError(ValueError):
    pass


def normalize_override_request(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    rules = sorted({str(rule).strip() for rule in (payload.get("rules") or [])})
    invalid = sorted(set(rules) - SOFT_OVERRIDE_RULES)
    if not rules:
        raise CampaignOverrideError("At least one soft pacing rule must be selected")
    if invalid:
        raise CampaignOverrideError(
            "Campaign policy override cannot bypass: " + ", ".join(invalid)
        )
    reason = str(payload.get("reason") or "").strip()
    if len(reason) < 10:
        raise CampaignOverrideError("Campaign policy override reason is required")
    if payload.get("risk_acknowledged") is not True:
        raise CampaignOverrideError("Campaign policy override risk must be acknowledged")
    expires_in_hours = int(payload.get("expires_in_hours") or 24)
    if not 1 <= expires_in_hours <= 72:
        raise CampaignOverrideError("Campaign policy override expiry must be between 1 and 72 hours")
    return {
        "rules": rules,
        "reason": reason,
        "risk_acknowledged": True,
        "expires_in_hours": expires_in_hours,
    }


def create_override(
    db: Session,
    run: CampaignRun,
    payload: dict[str, Any] | None,
    requested_by_user_id: int | None,
) -> CampaignPolicyOverride | None:
    normalized = normalize_override_request(payload)
    if normalized is None:
        return None
    if run.trigger_type != "manual":
        raise CampaignOverrideError("Only manual campaign runs may request pacing overrides")
    if requested_by_user_id is None:
        raise CampaignOverrideError("Automated runs cannot request pacing overrides")

    now = datetime.utcnow()
    active_from = max(now, run.starts_at or now)
    policy = db.query(ProjectPolicy).filter(
        ProjectPolicy.project_id == run.project_id,
    ).first()
    row = CampaignPolicyOverride(
        project_id=run.project_id,
        run_id=run.id,
        status="approved",
        override_keys=normalized["rules"],
        reason=normalized["reason"],
        risk_acknowledged=True,
        dual_approval_required=False,
        planned_count_snapshot=int(run.planned_count or 0),
        policy_snapshot={
            "contact_caps": policy.contact_caps if policy else None,
            "channel_cooldowns": policy.channel_cooldowns if policy else None,
            "quiet_hours": policy.quiet_hours if policy else None,
            "priority_rules": policy.priority_rules if policy else None,
            "immutable_guards": list(IMMUTABLE_GUARDS),
            "approval_mode": "requesting_admin",
        },
        requested_by_user_id=requested_by_user_id,
        approved_by_user_id=requested_by_user_id,
        approved_at=now,
        expires_at=active_from + timedelta(hours=normalized["expires_in_hours"]),
    )
    db.add(row)
    db.flush()
    return row


def active_override_rules(
    override: CampaignPolicyOverride | None,
    *,
    now: datetime | None = None,
) -> set[str]:
    now = now or datetime.utcnow()
    if not override:
        return set()
    if (
        override.status != "approved"
        or not override.risk_acknowledged
        or override.revoked_at is not None
        or override.expires_at <= now
    ):
        return set()
    return set(override.override_keys or []) & set(SOFT_OVERRIDE_RULES)


def approve_override(
    db: Session,
    override: CampaignPolicyOverride,
    approved_by_user_id: int,
) -> CampaignPolicyOverride:
    if override.status == "approved":
        return override
    if override.status != "pending_approval":
        raise CampaignOverrideError("Campaign policy override is not awaiting approval")
    if override.run.status != "awaiting_override_approval":
        raise CampaignOverrideError("Campaign run is no longer awaiting override approval")
    if override.expires_at <= datetime.utcnow():
        override.status = "expired"
        db.commit()
        raise CampaignOverrideError("Campaign policy override has expired")
    override.status = "approved"
    override.approved_by_user_id = approved_by_user_id
    override.approved_at = datetime.utcnow()
    if override.run.status == "awaiting_override_approval":
        override.run.status = "scheduled"
    db.commit()
    db.refresh(override)
    return override


def revoke_override(
    db: Session,
    override: CampaignPolicyOverride,
    revoked_by_user_id: int,
) -> CampaignPolicyOverride:
    if override.status == "revoked":
        return override
    if override.status not in {"approved", "pending_approval"}:
        raise CampaignOverrideError("Campaign policy override cannot be revoked in its current state")
    override.status = "revoked"
    override.revoked_by_user_id = revoked_by_user_id
    override.revoked_at = datetime.utcnow()
    db.commit()
    db.refresh(override)
    return override
