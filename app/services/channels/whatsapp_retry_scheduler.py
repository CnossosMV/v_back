"""
WhatsApp Retry Scheduler — clones a failed SendLog into a delayed retry.

When a Meta Cloud API webhook reports a failure with a transient error
code (e.g. 131026 — recipient temporarily unreachable), this scheduler
creates a new SendLog with status='delayed' and scheduled_at in the
future. The existing ScheduledSendWorker (30 s poll) picks it up and
re-runs it through the full SendService pipeline.

Design notes:
- Retry chain is tracked via SendLog.retry_of_id. Chain depth is
  capped by whatsapp_retry_policy.MAX_RETRY_ATTEMPTS.
- The retry clone copies content_payload, template_id, instance_id, and
  recipient verbatim — Meta-side state changes (24-h window, etc.) are
  re-resolved by the adapter at dispatch time.
- source_type is set to "retry" so DeliveryFeedbackService excludes the
  retry's eventual failure from the soft-bounce threshold count
  (see delivery_feedback_service.py — retry_log_ids subquery).
- Dedupe: if a delayed retry already exists for the failing log, the
  scheduler is a no-op. Meta retries webhooks aggressively, so this
  matters.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import SendLog
from app.services.channels.whatsapp_retry_policy import (
    MAX_RETRY_ATTEMPTS,
    is_retryable,
    next_backoff,
    retry_expires_at,
)

logger = logging.getLogger(__name__)


class WhatsAppRetryScheduler:
    """Schedules retries for failed WhatsApp Meta Cloud API sends."""

    def __init__(self, db: Session):
        self.db = db

    # ── public ────────────────────────────────────────────────────────────

    def maybe_schedule_retry(
        self,
        failed_log: SendLog,
        error_code: Optional[str],
        error_message: Optional[str] = None,
    ) -> Optional[SendLog]:
        """Schedule a retry if appropriate; return the new SendLog or None.

        Returns None when:
        - channel is not WhatsApp
        - error_code is not in the retryable set
        - retry chain depth is already at the cap
        - a delayed retry for this log already exists (dedupe)
        """
        if not failed_log:
            return None

        if (failed_log.channel or "").lower() != "whatsapp":
            return None

        if not is_retryable(error_code):
            return None

        # Determine attempt index by walking the retry chain back to the root.
        # Original send is attempt 0; first retry is attempt 1.
        chain_depth = self._chain_depth(failed_log)
        attempt_index = chain_depth + 1  # the retry we're about to schedule

        if attempt_index > MAX_RETRY_ATTEMPTS:
            logger.info(
                "WhatsApp retry budget exhausted for send_log=%d (depth=%d, max=%d, code=%s)",
                failed_log.id, chain_depth, MAX_RETRY_ATTEMPTS, error_code,
            )
            return None

        # Dedupe — only one outstanding retry per failed log.
        existing = self.db.query(SendLog).filter(
            SendLog.retry_of_id == failed_log.id,
            SendLog.status.in_(("delayed", "queued", "sent", "delivered", "read")),
        ).first()
        if existing:
            logger.debug(
                "WhatsApp retry already scheduled for send_log=%d (retry=%d, status=%s)",
                failed_log.id, existing.id, existing.status,
            )
            return existing

        delay = next_backoff(attempt_index)
        if delay is None:
            return None
        scheduled_at = datetime.utcnow() + delay

        # Walk back to the root log for context that retries lose
        # (render_context, enrollment linkage, template id).
        root = self._find_root(failed_log)

        # Prefer the failing log's own fields; fall back to the root's.
        content_payload = failed_log.content_payload or root.content_payload
        content_type = failed_log.content_type or root.content_type or "text"
        content_summary = failed_log.content_summary or root.content_summary
        template_id = failed_log.template_id or root.template_id
        instance_id = failed_log.instance_id or root.instance_id
        render_context = failed_log.render_context or root.render_context
        enrollment_id = (
            failed_log.deferred_source_enrollment_id
            or root.deferred_source_enrollment_id
        )

        # Restrict fallback to the same channel — never cross over to email/SMS
        # because of a transient WhatsApp problem.
        preferred_channel = failed_log.channel or failed_log.preferred_channel or "whatsapp"
        fallback_order = [preferred_channel]

        # Bound the retry: a stale retry must expire rather than fire days
        # later, and it inherits the dispatch priority of the send it retries
        # so it keeps its place in the worker's priority-ordered queue.
        expires_at = retry_expires_at(
            scheduled_at,
            failed_log.expires_at or root.expires_at,
        )
        priority = failed_log.priority if failed_log.priority is not None else root.priority

        # Annotate the original log's trace so the UI can show the retry plan.
        try:
            trace = list(failed_log.decision_trace or [])
            trace.append({
                "step": "auto_retry_scheduled",
                "attempt": attempt_index,
                "max_attempts": MAX_RETRY_ATTEMPTS,
                "error_code": error_code,
                "error_message": error_message,
                "scheduled_at": scheduled_at.isoformat(),
                "ts": datetime.utcnow().isoformat(),
            })
            failed_log.decision_trace = trace
        except Exception:
            # decision_trace is best-effort; don't block the retry on it.
            logger.debug("Could not append auto_retry_scheduled trace", exc_info=True)

        from app.services.channels.selection import acquire_attention_xact_lock
        from app.services.channels.source_contract import resolve_dispatch_source

        resolution = resolve_dispatch_source(
            self.db, failed_log.project_id, "retry", failed_log.id,
        )
        acquire_attention_xact_lock(
            self.db,
            failed_log.project_id,
            failed_log.user_id,
            failed_log.recipient,
            resolution.intent_fields.get("attention_scope"),
        )
        retry_log = SendLog(
            project_id=failed_log.project_id,
            user_id=failed_log.user_id,
            channel=preferred_channel,
            recipient=failed_log.recipient,
            content_type=content_type,
            content_summary=content_summary,
            content_payload=content_payload,
            template_id=template_id,
            source_type="retry",
            source_id=failed_log.id,
            preferred_channel=preferred_channel,
            fallback_order=fallback_order,
            fallback_attempt=0,
            status="delayed",
            scheduled_at=scheduled_at,
            expires_at=expires_at,
            priority=priority,
            instance_id=instance_id,
            render_context=render_context,
            deferred_source_enrollment_id=enrollment_id,
            retry_of_id=failed_log.id,
            **resolution.intent_fields,
            decision_trace=[{
                "step": "auto_retry_created",
                "attempt": attempt_index,
                "max_attempts": MAX_RETRY_ATTEMPTS,
                "from_log_id": failed_log.id,
                "trigger_error_code": error_code,
                "trigger_error_message": error_message,
                "scheduled_at": scheduled_at.isoformat(),
                "ts": datetime.utcnow().isoformat(),
            }],
        )

        self.db.add(retry_log)
        self.db.flush()

        logger.info(
            "Scheduled WhatsApp retry: original=%d retry=%d attempt=%d/%d "
            "delay=%s scheduled_at=%s code=%s",
            failed_log.id, retry_log.id, attempt_index, MAX_RETRY_ATTEMPTS,
            delay, scheduled_at.isoformat(), error_code,
        )
        return retry_log

    # ── internals ─────────────────────────────────────────────────────────

    def _chain_depth(self, log: SendLog) -> int:
        """How many automatic retries already precede this log.

        Walks `retry_of_id` back to a log with no parent. Cycles are guarded.
        """
        depth = 0
        current = log
        visited: set[int] = {log.id}
        while current and current.retry_of_id:
            parent = self.db.query(SendLog).filter(
                SendLog.id == current.retry_of_id,
            ).first()
            if not parent or parent.id in visited:
                break
            visited.add(parent.id)
            depth += 1
            current = parent
        return depth

    def _find_root(self, log: SendLog) -> SendLog:
        """Walk to the topmost ancestor in the retry chain."""
        current = log
        visited: set[int] = {log.id}
        while current.retry_of_id:
            parent = self.db.query(SendLog).filter(
                SendLog.id == current.retry_of_id,
            ).first()
            if not parent or parent.id in visited:
                break
            visited.add(parent.id)
            current = parent
        return current
