"""
Deferred Send Helper — creates deferred SendLog entries when PolicyService
blocks a send with a deferrable violation (quiet hours, channel cooldown).

Instead of dropping the message, stores a send intent that the
ScheduledSendWorker will pick up and re-render at send time.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.models import SendLog, ProjectPolicy
from app.schemas.policies import PolicyDecision
from app.services.channels.selection import acquire_attention_xact_lock
from app.services.channels.source_contract import resolve_dispatch_source

logger = logging.getLogger(__name__)


class DeferredSendHelper:
    def __init__(self, db: Session):
        self.db = db

    def should_defer(self, project_id: int, decision: PolicyDecision) -> bool:
        """
        Check if the project's suppression_config allows deferring this violation type.

        Returns False if:
        - decision is not deferrable
        - decision has no defer_until
        - project has no suppression_config or deferral is not enabled for this violation
        """
        if not decision.deferrable or not decision.defer_until:
            return False

        policy = self.db.query(ProjectPolicy).filter(
            ProjectPolicy.project_id == project_id,
        ).first()
        if not policy or not policy.suppression_config:
            return False

        sc = policy.suppression_config
        violated = decision.policy_violated

        if violated == "quiet_hours":
            return bool(sc.get("defer_on_quiet_hours", False))
        elif violated == "channel_cooldown":
            return bool(sc.get("defer_on_cooldown", False))
        elif violated and (
            violated.endswith("_cap") or violated.endswith("_total_cap")
        ):
            return bool(sc.get("defer_on_caps", False))

        return False

    def create_deferred_send(
        self,
        project_id: int,
        user_id: Optional[int],
        channel: str,
        recipient: str,
        decision: PolicyDecision,
        source_type: str,
        source_id: Optional[int] = None,
        template_id: Optional[int] = None,
        render_context: Optional[Dict[str, Any]] = None,
        content_payload: Optional[Dict[str, Any]] = None,
        expires_at: Optional[datetime] = None,
        enrollment_id: Optional[int] = None,
        priority: int = 0,
    ) -> SendLog:
        """
        Create a SendLog with status='deferred' and scheduled_at=decision.defer_until.

        If no explicit expires_at, uses the project's default_expires_after_hours
        from suppression_config.
        """
        if not expires_at:
            expires_at = self._compute_default_expiry(project_id, decision.defer_until)

        resolution = resolve_dispatch_source(
            self.db, project_id, source_type, source_id,
        )
        intent_fields = resolution.intent_fields
        acquire_attention_xact_lock(
            self.db, project_id, user_id, recipient,
            intent_fields.get("attention_scope"),
        )

        log = SendLog(
            project_id=project_id,
            user_id=user_id,
            channel=channel,
            recipient=recipient,
            content_type="deferred",
            content_summary=f"Deferred: {decision.reason}",
            content_payload=content_payload,
            template_id=template_id,
            source_type=source_type,
            source_id=source_id,
            preferred_channel=channel,
            status="deferred",
            **intent_fields,
            scheduled_at=decision.defer_until,
            render_context=render_context,
            expires_at=expires_at,
            deferred_source_enrollment_id=enrollment_id,
            priority=priority,
            decision_trace=[{
                "policy_violated": decision.policy_violated,
                "reason": decision.reason,
                "defer_until": decision.defer_until.isoformat() if decision.defer_until else None,
            }],
        )
        self.db.add(log)
        self.db.flush()

        logger.info(
            "Created deferred send log %d for project %d user %s channel %s, "
            "scheduled_at=%s expires_at=%s priority=%d",
            log.id, project_id, user_id, channel,
            decision.defer_until, expires_at, priority,
        )
        return log

    def create_suppression_deferred(
        self,
        project_id: int,
        user_id: Optional[int],
        channel: str,
        recipient: str,
        defer_until: datetime,
        reason: str,
        source_type: str,
        source_id: Optional[int] = None,
        template_id: Optional[int] = None,
        render_context: Optional[Dict[str, Any]] = None,
        content_payload: Optional[Dict[str, Any]] = None,
        expires_at: Optional[datetime] = None,
        enrollment_id: Optional[int] = None,
        priority: int = 0,
    ) -> SendLog:
        """
        Create a deferred SendLog for suppression-window violations.

        Unlike create_deferred_send, this does not require a PolicyDecision —
        it is used when the funnel engine's suppression check blocks a send.
        """
        if not expires_at:
            expires_at = self._compute_default_expiry(project_id, defer_until)

        resolution = resolve_dispatch_source(
            self.db, project_id, source_type, source_id,
        )
        intent_fields = resolution.intent_fields
        acquire_attention_xact_lock(
            self.db, project_id, user_id, recipient,
            intent_fields.get("attention_scope"),
        )

        log = SendLog(
            project_id=project_id,
            user_id=user_id,
            channel=channel,
            recipient=recipient,
            content_type="deferred",
            content_summary=f"Deferred: {reason}",
            content_payload=content_payload,
            template_id=template_id,
            source_type=source_type,
            source_id=source_id,
            preferred_channel=channel,
            status="deferred",
            **intent_fields,
            scheduled_at=defer_until,
            render_context=render_context,
            expires_at=expires_at,
            deferred_source_enrollment_id=enrollment_id,
            priority=priority,
            decision_trace=[{
                "policy_violated": "suppression_window",
                "reason": reason,
                "defer_until": defer_until.isoformat() if defer_until else None,
            }],
        )
        self.db.add(log)
        self.db.flush()

        logger.info(
            "Created suppression-deferred send log %d for project %d user %s "
            "channel %s, scheduled_at=%s expires_at=%s priority=%d",
            log.id, project_id, user_id, channel,
            defer_until, expires_at, priority,
        )
        return log

    def _compute_default_expiry(
        self, project_id: int, defer_until: Optional[datetime]
    ) -> Optional[datetime]:
        """Compute default expires_at from project suppression_config."""
        policy = self.db.query(ProjectPolicy).filter(
            ProjectPolicy.project_id == project_id,
        ).first()
        if not policy or not policy.suppression_config:
            return None

        hours = policy.suppression_config.get("default_expires_after_hours")
        if not hours:
            return None

        base = defer_until or datetime.utcnow()
        return base + timedelta(hours=int(hours))
