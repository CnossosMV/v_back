"""Persist authenticated channel-permission claims without weakening opt-out."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.campaigns import ContactEndpoint, ContactPermissionEvidence
from app.models.messaging import MessagingUser


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class PermissionEvidenceService:
    def __init__(self, db: Session):
        self.db = db

    def record_email_claim(
        self,
        *,
        project_id: int,
        user: MessagingUser,
        claim: Any,
        api_key_id: int,
        ingress: str,
    ) -> ContactPermissionEvidence:
        """Append evidence and project the newest claim onto the contact cache.

        The authenticated API key and ingress are recorded as provenance. This
        never clears an unsubscribe/global opt-out; those remain harder gates.
        """
        if user.project_id != project_id:
            raise ValueError("Permission subject does not belong to project")

        captured_at = _utc_naive(claim.captured_at)
        expires_at = _utc_naive(claim.expires_at) if claim.expires_at else None
        existing = self.db.query(ContactPermissionEvidence).filter(
            ContactPermissionEvidence.project_id == project_id,
            ContactPermissionEvidence.user_id == user.id,
            ContactPermissionEvidence.channel == "email",
            ContactPermissionEvidence.permission_type == claim.permission_type,
            ContactPermissionEvidence.evidence_ref == claim.evidence_ref,
        ).first()
        if existing:
            return existing

        latest = self.db.query(ContactPermissionEvidence).filter(
            ContactPermissionEvidence.project_id == project_id,
            ContactPermissionEvidence.user_id == user.id,
            ContactPermissionEvidence.channel == "email",
            ContactPermissionEvidence.permission_type == claim.permission_type,
        ).order_by(
            ContactPermissionEvidence.captured_at.desc(),
            ContactPermissionEvidence.id.desc(),
        ).first()

        endpoint_id = self.db.query(ContactEndpoint.id).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == user.id,
            ContactEndpoint.endpoint_type == "email",
            ContactEndpoint.is_primary.is_(True),
        ).scalar()
        if endpoint_id is None:
            raise ValueError("Email permission requires an active primary email endpoint")
        metadata = dict(claim.metadata or {})
        metadata.update({
            "authenticated_api_key_id": api_key_id,
            "ingress": ingress,
        })
        evidence = ContactPermissionEvidence(
            project_id=project_id,
            user_id=user.id,
            endpoint_id=endpoint_id,
            channel="email",
            permission_type=claim.permission_type,
            status=claim.status,
            source=claim.source,
            policy_version=claim.policy_version,
            evidence_ref=claim.evidence_ref,
            captured_at=captured_at,
            expires_at=expires_at,
            evidence_metadata=metadata,
        )
        self.db.add(evidence)
        self.db.flush()

        if latest is None or captured_at >= latest.captured_at:
            granted = claim.status == "granted" and (
                expires_at is None or expires_at > datetime.utcnow()
            )
            channels = dict(user.consent_channels or {})
            channels["email"] = {
                "granted": granted,
                "status": claim.status,
                "source": claim.source,
                "captured_at": captured_at.isoformat(),
                "policy_version": claim.policy_version,
                "evidence_ref": claim.evidence_ref,
                "expires_at": expires_at.isoformat() if expires_at else None,
            }
            user.consent_channels = channels
            user.consent_marketing = granted
            user.consent_given_at = captured_at if granted else user.consent_given_at
            user.consent_version = claim.policy_version

        return evidence
