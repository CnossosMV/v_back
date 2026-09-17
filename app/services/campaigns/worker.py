"""Persistent campaign scheduler and recipient dispatcher."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import SendLog
from app.models.campaigns import (
    Campaign,
    CampaignRecipient,
    CampaignRun,
    CampaignWave,
    ChannelCapacityReservation,
    ChannelDeliveryProfile,
)
from app.models.messaging import MessagingUser
from app.services.campaigns.alerts import upsert_operational_alert
from app.services.campaigns.channel_registry import CampaignChannelRegistry
from app.services.campaigns.capacity import _bucket, _daily_capacity
from app.services.campaigns.service import CampaignService
from app.services.channels.send_service import SendService

logger = logging.getLogger(__name__)

_SEND_SUCCESS = {"sent", "delivered", "read", "opened"}
_SEND_PENDING = {"queued", "delayed", "deferred", "candidate", "campaign_selected"}
_SEND_FAILURE = {
    "failed", "exhausted", "blocked", "skipped", "superseded", "expired", "canceled",
    "submission_unknown",
}
_SEND_SUBMITTING = "submitting"
_CAPACITY_MARKER = "capacity_consumed"
_CAMPAIGN_WORKER_LOCK_ID = 738003


class CampaignWorker:
    def __init__(self, poll_interval: int = 5, batch_size: int = 25, recurrence_interval: int = 60):
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.recurrence_interval = recurrence_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_recurrence_scan: datetime | None = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(db_session_factory or SessionLocal),
            name="campaign-worker",
        )
        logger.info("Campaign worker started (poll=%ss, batch=%s)", self.poll_interval, self.batch_size)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Campaign worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = await self._process_cycle(db)
                    if processed:
                        logger.info("Campaign worker processed %s recipient(s)", processed)
                finally:
                    db.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Campaign worker cycle failed")
            await asyncio.sleep(self.poll_interval)

    async def _process_cycle(self, db: Session) -> int:
        locked = False
        if db.get_bind().dialect.name == "postgresql":
            locked = bool(db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _CAMPAIGN_WORKER_LOCK_ID},
            ).scalar())
            if not locked:
                db.rollback()
                return 0
        try:
            return await self._process_locked_cycle(db)
        finally:
            if locked:
                db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _CAMPAIGN_WORKER_LOCK_ID},
                )
                db.rollback()

    async def _process_locked_cycle(self, db: Session) -> int:
        now = datetime.utcnow()
        self._recover_stale_processing(db, now)
        if (
            self._last_recurrence_scan is None
            or (now - self._last_recurrence_scan).total_seconds() >= self.recurrence_interval
        ):
            CampaignService(db).schedule_due_recurrences(now)
            self._last_recurrence_scan = now

        self._reconcile_send_logs(db)
        self._reclaim_campaign_deferrals(db, now)
        self._finalize_quiescent_runs(db)
        claimed: list[int] = []
        for _ in range(self.batch_size):
            recipient_id = self._claim_one(db, now)
            if recipient_id is None:
                break
            claimed.append(recipient_id)
        for recipient_id in claimed:
            await self._dispatch(db, recipient_id)
        return len(claimed)

    def _claim_one(self, db: Session, now: datetime) -> int | None:
        recipient = db.query(CampaignRecipient).join(
            CampaignRun,
            CampaignRun.id == CampaignRecipient.run_id,
        ).join(
            Campaign,
            Campaign.id == CampaignRun.campaign_id,
        ).join(
            CampaignWave,
            CampaignWave.id == CampaignRecipient.wave_id,
        ).filter(
            CampaignRecipient.status == "pending",
            CampaignRecipient.scheduled_at <= now,
            CampaignRun.status.in_(["scheduled", "running"]),
            CampaignRun.cancel_requested == False,  # noqa: E712
            Campaign.status == "active",
            CampaignWave.status.in_(["planned", "running"]),
        ).order_by(
            CampaignRecipient.scheduled_at,
            CampaignRecipient.id,
        ).with_for_update(skip_locked=True, of=CampaignRecipient).first()
        if not recipient:
            db.rollback()
            return None
        recipient.status = "processing"
        recipient.attempt_count = int(recipient.attempt_count or 0) + 1
        recipient.updated_at = now
        run = recipient.run
        wave = recipient.wave
        if run.status == "scheduled":
            run.status = "running"
            run.started_at = run.started_at or now
        if wave and wave.status == "planned":
            wave.status = "running"
            wave.started_at = wave.started_at or now
        recipient_id = recipient.id
        db.commit()
        return recipient_id

    def _recover_stale_processing(self, db: Session, now: datetime) -> int:
        """Return abandoned claims to the durable queue after a worker restart."""
        stale_before = now - timedelta(minutes=10)
        # A durable pre-I/O intent without a terminal outcome is ambiguous. It
        # must never return to pending because doing so could submit twice.
        unknown_rows = db.query(CampaignRecipient, SendLog).join(
            SendLog,
            SendLog.id == CampaignRecipient.send_log_id,
        ).filter(
            CampaignRecipient.status == "processing",
            CampaignRecipient.updated_at < stale_before,
            SendLog.project_id == CampaignRecipient.project_id,
            SendLog.source_type == "campaign",
            SendLog.source_id == CampaignRecipient.id,
            SendLog.status == _SEND_SUBMITTING,
        ).all()
        touched: set[tuple[int, int | None]] = set()
        for recipient, send_log in unknown_rows:
            send_log.status = "submission_unknown"
            send_log.error_message = "Worker stopped while provider submission was in progress"
            recipient.status = "failed"
            recipient.last_error_code = "submission_unknown"
            recipient.last_error = send_log.error_message
            recipient.completed_at = now
            self._upsert_submission_unknown_alert(db, recipient, send_log.error_message)
            touched.add((recipient.run_id, recipient.wave_id))
        if unknown_rows:
            db.flush()

        canceled_runs = select(CampaignRun.id).where(or_(
            CampaignRun.cancel_requested == True,  # noqa: E712
            CampaignRun.status.in_(["canceled", "cancel_requested"]),
        ))
        canceled_targets = db.query(
            CampaignRecipient.run_id,
            CampaignRecipient.wave_id,
        ).filter(
            CampaignRecipient.status == "processing",
            CampaignRecipient.updated_at < stale_before,
            CampaignRecipient.run_id.in_(canceled_runs),
        ).distinct().all()
        canceled = db.query(CampaignRecipient).filter(
            CampaignRecipient.status == "processing",
            CampaignRecipient.updated_at < stale_before,
            CampaignRecipient.run_id.in_(canceled_runs),
        ).update({
            CampaignRecipient.status: "canceled",
            CampaignRecipient.suppression_reason: "run_canceled",
            CampaignRecipient.last_error_code: "run_canceled",
            CampaignRecipient.last_error: "Recovered claim belongs to a canceled campaign run",
            CampaignRecipient.completed_at: now,
        }, synchronize_session=False)
        pending = db.query(CampaignRecipient).filter(
            CampaignRecipient.status == "processing",
            CampaignRecipient.updated_at < stale_before,
            ~CampaignRecipient.run_id.in_(canceled_runs),
        ).update({
            CampaignRecipient.status: "pending",
            CampaignRecipient.scheduled_at: now,
            CampaignRecipient.last_error_code: case(
                (CampaignRecipient.last_error_code == _CAPACITY_MARKER, _CAPACITY_MARKER),
                else_="stale_worker_claim",
            ),
            CampaignRecipient.last_error: "Recovered after an interrupted campaign worker claim",
        }, synchronize_session=False)
        changed = len(unknown_rows) + canceled + pending
        if changed:
            db.commit()
        else:
            db.rollback()
        touched.update(canceled_targets)
        for run_id, wave_id in touched:
            self._refresh_run(db, run_id, wave_id)
        return changed

    async def _dispatch(self, db: Session, recipient_id: int) -> None:
        recipient = db.query(CampaignRecipient).filter(
            CampaignRecipient.id == recipient_id,
        ).first()
        if not recipient or recipient.status != "processing":
            return
        try:
            run = recipient.run
            if run.cancel_requested or run.status in {"canceled", "cancel_requested"}:
                self._finish_recipient(db, recipient, "canceled", "run_canceled")
                return
            if not run.campaign or run.campaign.status != "active":
                self._requeue_for_campaign_pause(db, recipient)
                return

            existing = db.query(SendLog).filter(
                SendLog.project_id == recipient.project_id,
                SendLog.source_type == "campaign",
                SendLog.source_id == recipient.id,
                SendLog.status.in_(list(
                    _SEND_SUCCESS | _SEND_PENDING | _SEND_FAILURE | {"submission_unknown", _SEND_SUBMITTING}
                )),
            ).order_by(SendLog.id.desc()).first()
            if existing:
                recipient.send_log_id = existing.id
                if existing.expires_at and existing.expires_at <= datetime.utcnow():
                    existing.status = "expired"
                    existing.failed_at = datetime.utcnow()
                    existing.error_message = "Campaign attention candidate expired before submission"
                    self._finish_recipient(
                        db, recipient, "skipped", existing.error_message,
                    )
                    return
                if existing.status in {"submission_unknown", _SEND_SUBMITTING}:
                    existing.status = "submission_unknown"
                    existing.error_message = (
                        existing.error_message or "Provider submission outcome is unknown"
                    )
                    self._terminalize_submission_unknown(
                        db, recipient, existing.error_message,
                    )
                    return
                if existing.status in _SEND_SUCCESS:
                    db.commit()
                    recipient = db.query(CampaignRecipient).filter(
                        CampaignRecipient.id == recipient_id,
                    ).one()
                    self._complete_provider_success(
                        db,
                        recipient,
                        "delivered" if existing.status in {"delivered", "read", "opened"} else "sent",
                    )
                    return
                if existing.status in _SEND_FAILURE:
                    terminal = "canceled" if existing.status == "canceled" else "skipped"
                    self._finish_recipient(
                        db, recipient, terminal,
                        existing.error_message or f"campaign_candidate_{existing.status}",
                    )
                    return
                if existing.status == "candidate":
                    # The shared Selection worker owns this row until it marks
                    # it campaign_selected. CampaignWorker must never bypass
                    # that arbitration and submit it directly.
                    self._requeue_existing_deferral(
                        db, recipient, existing, "Awaiting shared attention selection",
                    )
                    return
                if existing.status in {"delayed", "deferred"}:
                    # A campaign that waited for quiet hours, capacity or a
                    # delivery profile no longer owns attention.  Re-enter
                    # the shared contest at the actual retry time.
                    self._requeue_existing_deferral(
                        db, recipient, existing,
                        "Campaign deferral must win attention again",
                    )
                    return
                # A paced/quiet-hours/paused campaign dispatch already owns a
                # durable SendLog and its pace reservation.  Keep that exact
                # row and, when it becomes due, finalize it in place after all
                # campaign gates have been revalidated.  Minting a new log on
                # every wake-up would reserve another pace slot and could make
                # the recipient chase the cursor forever.
                if existing.scheduled_at and existing.scheduled_at > datetime.utcnow():
                    self._requeue_existing_deferral(db, recipient, existing)
                    return

            revalidated = self._revalidate(db, recipient)
            if revalidated is None:
                return
            user, endpoint, eligibility = revalidated

            profile = db.query(ChannelDeliveryProfile).filter(
                ChannelDeliveryProfile.id == recipient.delivery_profile_id,
                ChannelDeliveryProfile.project_id == recipient.project_id,
                ChannelDeliveryProfile.channel == recipient.channel,
                ChannelDeliveryProfile.sender_identity_id == recipient.sender_identity_id,
            ).first()
            if profile and profile.status == "paused":
                self._requeue_for_delivery_pause(db, recipient, "delivery_profile_paused")
                return
            if (
                profile
                and profile.sender_identity
                and profile.sender_identity.status == "paused"
            ):
                self._requeue_for_delivery_pause(db, recipient, "sender_identity_paused")
                return
            if (
                not profile
                or profile.status != "active"
                or profile.health_status not in {"healthy", "degraded"}
                or not profile.sender_identity
                or profile.sender_identity.status != "active"
                or profile.sender_identity.project_id != recipient.project_id
                or profile.sender_identity.channel != recipient.channel
                or profile.sender_identity.provider != recipient.provider
                or profile.provider != recipient.provider
            ):
                self._retry_or_fail(db, recipient, "delivery_profile_unavailable")
                return
            adapter = CampaignChannelRegistry.get(recipient.channel)
            if not adapter:
                self._finish_recipient(db, recipient, "failed", "campaign_channel_not_registered")
                return
            if not recipient.action:
                self._finish_recipient(db, recipient, "failed", "campaign_action_missing")
                return
            dispatch = adapter.build_dispatch(
                db,
                profile,
                recipient.action,
                recipient.variant,
                user,
                endpoint.normalized_value or endpoint.value,
            )
            if dispatch.errors:
                self._finish_recipient(db, recipient, "skipped", ";".join(dispatch.errors))
                return

            if not self._lock_dispatch_gate(db, recipient):
                return
            if existing and existing.status == "campaign_selected":
                # Lock order is campaign/run -> attention slot -> SendLog.
                # This matches cancellation and avoids a run/attention
                # deadlock while still placing the re-rank immediately before
                # the provider hand-off.
                if not self._confirm_attention_selection(db, recipient, existing):
                    return
            throttle_until = self._profile_throttle_until(
                db, recipient, profile, datetime.utcnow(), run.campaign.policy_config or {},
            )
            if throttle_until is not None:
                self._requeue_for_profile_capacity(db, recipient, throttle_until)
                return

            content_metadata = dict(dispatch.content.metadata or {})
            content_metadata.update({
                "campaign_run_id": run.id,
                "campaign_recipient_id": recipient.id,
                "contact_endpoint_id": endpoint.id,
                "message_id": self._message_id(recipient),
            })
            dispatch.content.metadata = content_metadata
            policy = run.campaign.policy_config or {}
            decision = await SendService(db).send(
                project_id=recipient.project_id,
                user_id=user.id,
                recipient=dispatch.recipient,
                content=dispatch.content,
                channel=recipient.channel,
                fallback_order=[recipient.channel],
                on_channel_unavailable="fail",
                source_type="campaign",
                source_id=recipient.id,
                instance_config=dispatch.instance_config,
                policy_profile="campaign",
                policy_context={
                    "permission_authorized": True,
                    "permission_policy": policy.get("permission_mode", "explicit_consent"),
                    "permission_evidence_id": eligibility.permission_evidence_id,
                    "verification_policy": policy.get("verification_mode", "require_valid"),
                    "endpoint_id": endpoint.id,
                    "endpoint_hash": endpoint.value_hash,
                    "locale": user.locale,
                    "deadline_at": run.deadline_at,
                },
                template_id=dispatch.template_id,
                existing_send_log_id=existing.id if existing else None,
            )
            recipient.send_log_id = decision.send_log_id
            if decision.status in _SEND_PENDING:
                deferred_log = db.query(SendLog).filter(
                    SendLog.id == decision.send_log_id,
                    SendLog.project_id == recipient.project_id,
                    SendLog.source_type == "campaign",
                    SendLog.source_id == recipient.id,
                ).first()
                if not deferred_log:
                    self._retry_or_fail(db, recipient, "campaign_deferred_send_log_missing")
                else:
                    self._requeue_existing_deferral(db, recipient, deferred_log, decision.error)
            elif decision.success:
                # Persist provider success before capacity accounting.  If the
                # process dies in between, the next claim finds this SendLog
                # and reconciles capacity without sending a duplicate.
                db.commit()
                recipient = db.query(CampaignRecipient).filter(
                    CampaignRecipient.id == recipient_id,
                ).one()
                self._complete_provider_success(db, recipient, "sent")
            elif decision.status == "canceled":
                self._finish_recipient(
                    db, recipient, "canceled", decision.error or "run_canceled",
                )
            elif decision.status == "submission_unknown":
                self._terminalize_submission_unknown(
                    db, recipient, decision.error or "Provider submission outcome is unknown",
                )
            elif decision.status in {"skipped", "exhausted"}:
                self._finish_recipient(
                    db, recipient, "skipped", decision.error or decision.status,
                )
            else:
                self._retry_or_fail(db, recipient, decision.error or decision.status or "send_failed")
        except Exception as exc:
            db.rollback()
            recipient = db.query(CampaignRecipient).filter(CampaignRecipient.id == recipient_id).first()
            if recipient:
                uncertain = db.query(SendLog).filter(
                    SendLog.project_id == recipient.project_id,
                    SendLog.source_type == "campaign",
                    SendLog.source_id == recipient.id,
                    SendLog.status.in_([_SEND_SUBMITTING, "submission_unknown"]),
                ).order_by(SendLog.id.desc()).first()
                if uncertain:
                    recipient.send_log_id = uncertain.id
                    uncertain.status = "submission_unknown"
                    uncertain.error_message = uncertain.error_message or str(exc)[:4000]
                    self._terminalize_submission_unknown(
                        db, recipient, uncertain.error_message,
                    )
                else:
                    self._retry_or_fail(db, recipient, str(exc))
            logger.exception("Campaign recipient %s dispatch failed", recipient_id)

    def _lock_dispatch_gate(self, db: Session, recipient: CampaignRecipient) -> bool:
        """Serialize the final cancel/pause decision with an external send."""
        run = db.query(CampaignRun).filter(
            CampaignRun.id == recipient.run_id,
            CampaignRun.project_id == recipient.project_id,
        ).with_for_update().first()
        if not run or run.cancel_requested or run.status in {"canceled", "cancel_requested"}:
            self._finish_recipient(db, recipient, "canceled", "run_canceled")
            return False
        campaign = db.query(Campaign).filter(
            Campaign.id == run.campaign_id,
            Campaign.project_id == recipient.project_id,
        ).with_for_update().first()
        if not campaign or campaign.status != "active":
            self._requeue_for_campaign_pause(db, recipient)
            return False
        return True

    def _confirm_attention_selection(
        self,
        db: Session,
        recipient: CampaignRecipient,
        send_log: SendLog,
    ) -> bool:
        """Final campaign attention CAS before the provider hand-off.

        ``campaign_selected`` is deliberately not sufficient by itself.  The
        row must still rank first in the complete current contest while the
        same slot lock used by contender insertion is held.  SendService's
        durable ``submitting`` commit is the decision deadline and releases
        the transaction lock.
        """
        from app.services.channels.selection import (
            acquire_attention_xact_lock,
            arbitrate_due_attention,
            candidate_mode,
            due_attention_contest,
            eligible_candidates,
            selection_mode,
        )

        if (
            candidate_mode(db, recipient.project_id) != "enforce"
            or selection_mode(db, recipient.project_id) != "enforce"
        ):
            send_log.status = "candidate"
            self._requeue_existing_deferral(
                db,
                recipient,
                send_log,
                "Attention engines are not enforced; campaign fails closed",
            )
            return False

        now = datetime.utcnow()
        acquire_attention_xact_lock(
            db,
            send_log.project_id,
            send_log.user_id,
            send_log.recipient,
            send_log.attention_scope,
        )
        current = db.query(SendLog).filter(
            SendLog.id == send_log.id,
            SendLog.project_id == recipient.project_id,
        ).execution_options(populate_existing=True).with_for_update().first()
        if not current:
            self._finish_recipient(db, recipient, "skipped", "campaign_attention_log_missing")
            return False
        if current.status != "campaign_selected":
            if current.status in _SEND_FAILURE:
                terminal = "canceled" if current.status == "canceled" else "skipped"
                self._finish_recipient(
                    db, recipient, terminal,
                    current.error_message or f"campaign_attention_{current.status}",
                )
            else:
                self._requeue_existing_deferral(
                    db, recipient, current,
                    "Campaign attention reservation changed before dispatch",
                )
            return False

        contest = eligible_candidates(
            due_attention_contest(db, current, now, lock=True),
            now,
        )
        decision = arbitrate_due_attention(
            db, contest, now, lock_future=True,
        )
        winner = decision["dispatch_winner"]
        if not winner or winner.id != current.id:
            current.status = "candidate"
            current.scheduled_at = decision.get("hold_until") or (
                now + timedelta(seconds=self.poll_interval)
            )
            if current.expires_at:
                current.scheduled_at = min(current.scheduled_at, current.expires_at)
            trace = list(current.decision_trace or [])
            trace.append({
                "step": "campaign_selection_revalidation_lost",
                "lost_to": getattr(decision.get("attention_owner"), "id", None),
                "candidates": len(contest),
                "consequence_at_gate": decision.get("consequence_at_gate"),
                "recheck_at": current.scheduled_at.isoformat(),
                "ts": now.isoformat(),
            })
            current.decision_trace = trace
            self._requeue_existing_deferral(
                db, recipient, current,
                "Campaign lost attention during final revalidation",
            )
            return False

        trace = list(current.decision_trace or [])
        trace.append({
            "step": "campaign_selection_revalidated",
            "candidates": len(contest),
            "consequence_at_gate": decision.get("consequence_at_gate"),
            "ts": now.isoformat(),
        })
        current.decision_trace = trace
        db.flush()
        return True

    def _profile_throttle_until(
        self,
        db: Session,
        recipient: CampaignRecipient,
        profile: ChannelDeliveryProfile,
        now: datetime,
        policy_config: dict,
    ) -> datetime | None:
        """Enforce rolling profile limits at dispatch, not only at planning."""
        base = db.query(CampaignRecipient.sent_at).filter(
            CampaignRecipient.project_id == recipient.project_id,
            CampaignRecipient.delivery_profile_id == profile.id,
            CampaignRecipient.sent_at.isnot(None),
        )
        blocked_until: list[datetime] = []
        for limit, window in (
            (profile.max_per_minute, timedelta(minutes=1)),
            (profile.max_per_hour, timedelta(hours=1)),
        ):
            if not limit:
                continue
            recent_count, oldest = base.with_entities(
                func.count(CampaignRecipient.id),
                func.min(CampaignRecipient.sent_at),
            ).filter(
                CampaignRecipient.sent_at > now - window,
            ).one()
            if int(recent_count or 0) >= int(limit) and oldest is not None:
                blocked_until.append(oldest + window + timedelta(seconds=1))

        bucket_start, bucket_end, local_day = _bucket(profile, now)
        daily_limit = _daily_capacity(profile, policy_config, local_day)
        if daily_limit > 0:
            sent_today = base.filter(
                CampaignRecipient.sent_at >= bucket_start,
                CampaignRecipient.sent_at < bucket_end,
            ).count()
            if sent_today >= daily_limit:
                blocked_until.append(bucket_end + timedelta(seconds=1))
        return max(blocked_until) if blocked_until else None

    def _requeue_for_profile_capacity(
        self,
        db: Session,
        recipient: CampaignRecipient,
        retry_at: datetime,
    ) -> None:
        if recipient.run.deadline_at and retry_at > recipient.run.deadline_at:
            self._finish_recipient(
                db, recipient, "failed", "delivery_profile_capacity_deadline_missed",
            )
            upsert_operational_alert(
                db,
                project_id=recipient.project_id,
                dedupe_key=f"campaign-deadline:{recipient.run_id}:{recipient.delivery_profile_id}",
                alert_type="campaign_capacity_deadline_missed",
                title="Campaign delivery capacity missed its deadline",
                message="Provider capacity became unavailable before the campaign deadline",
                severity="error",
                context={
                    "campaign_id": recipient.run.campaign_id,
                    "run_id": recipient.run_id,
                    "delivery_profile_id": recipient.delivery_profile_id,
                    "recipient_id": recipient.id,
                },
            )
            db.commit()
            return
        recipient.status = "pending"
        recipient.attempt_count = max(0, int(recipient.attempt_count or 0) - 1)
        recipient.scheduled_at = max(retry_at, datetime.utcnow() + timedelta(seconds=self.poll_interval))
        recipient.last_error = "Delivery profile capacity is temporarily exhausted"
        if recipient.last_error_code != _CAPACITY_MARKER:
            recipient.last_error_code = "delivery_profile_rate_limited"
        self._return_attention_to_selection(
            db, recipient, recipient.scheduled_at, "delivery_profile_rate_limited",
        )
        db.commit()
        self._refresh_run(db, recipient.run_id, recipient.wave_id)

    def _revalidate(self, db: Session, recipient: CampaignRecipient):
        if not recipient.user_id or not recipient.endpoint_id or not recipient.endpoint_hash:
            self._finish_recipient(db, recipient, "skipped", "snapshot_identifier_missing")
            return None
        candidate = db.query(MessagingUser.id).filter(
            MessagingUser.project_id == recipient.project_id,
            MessagingUser.id == recipient.user_id,
        )
        result = CampaignService(db).eligibility.evaluate(
            project_id=recipient.project_id,
            candidate_query=candidate,
            selection_config=recipient.run.campaign.selection_config,
            policy_config=recipient.run.campaign.policy_config,
            channel=recipient.channel,
        )
        if not result or not result[0].eligible:
            reason = result[0].reason if result else "contact_missing"
            self._finish_recipient(db, recipient, "skipped", reason)
            return None
        item = result[0]
        if (
            not item.endpoint
            or item.endpoint.id != recipient.endpoint_id
            or item.endpoint.value_hash != recipient.endpoint_hash
        ):
            self._finish_recipient(db, recipient, "skipped", "identifier_changed")
            return None
        return item.user, item.endpoint, item

    def _consume_capacity(self, db: Session, recipient: CampaignRecipient) -> None:
        if recipient.last_error_code == _CAPACITY_MARKER:
            return
        reservation = db.query(ChannelCapacityReservation).filter(
            ChannelCapacityReservation.run_id == recipient.run_id,
            ChannelCapacityReservation.wave_id == recipient.wave_id,
            ChannelCapacityReservation.delivery_profile_id == recipient.delivery_profile_id,
            ChannelCapacityReservation.status.in_(["reserved", "active", "consumed"]),
        ).with_for_update().first()
        if not reservation:
            raise RuntimeError("Campaign capacity reservation missing")
        if int(reservation.units_consumed or 0) >= int(reservation.units_reserved or 0):
            raise RuntimeError("Campaign capacity reservation exhausted")
        reservation.units_consumed = int(reservation.units_consumed or 0) + 1
        reservation.status = (
            "consumed"
            if reservation.units_consumed == reservation.units_reserved
            else "active"
        )
        recipient.last_error_code = _CAPACITY_MARKER
        db.commit()
        db.refresh(recipient)

    def _complete_provider_success(
        self,
        db: Session,
        recipient: CampaignRecipient,
        status: str,
    ) -> None:
        """Finalize an already submitted message without ever re-sending it."""
        recipient_id = recipient.id
        try:
            self._consume_capacity(db, recipient)
        except Exception as exc:
            db.rollback()
            recipient = db.query(CampaignRecipient).filter(
                CampaignRecipient.id == recipient_id,
            ).one()
            recipient.last_error_code = "capacity_reconciliation_failed"
            recipient.last_error = str(exc)[:4000]
            upsert_operational_alert(
                db,
                project_id=recipient.project_id,
                dedupe_key=f"campaign-capacity:{recipient.run_id}:{recipient.delivery_profile_id}",
                alert_type="campaign_capacity_reconciliation_failed",
                title="Campaign capacity accounting requires reconciliation",
                message=str(exc),
                severity="error",
                context={
                    "campaign_id": recipient.run.campaign_id,
                    "run_id": recipient.run_id,
                    "delivery_profile_id": recipient.delivery_profile_id,
                    "recipient_id": recipient.id,
                    "send_log_id": recipient.send_log_id,
                },
            )
            db.commit()
        try:
            if recipient.user_id:
                from app.services.scoring.policy_service import PolicyService

                send_log = db.query(SendLog).filter(
                    SendLog.id == recipient.send_log_id,
                    SendLog.project_id == recipient.project_id,
                ).first()
                if not send_log:
                    raise RuntimeError("Campaign ledger reconciliation has no SendLog")
                PolicyService(db).record_contact_once(
                    project_id=recipient.project_id,
                    user_id=recipient.user_id,
                    channel=recipient.channel,
                    source="campaign",
                    source_id=send_log.id,
                    sent_at=send_log.sent_at or datetime.utcnow(),
                )
        except Exception as exc:
            db.rollback()
            recipient = db.query(CampaignRecipient).filter(
                CampaignRecipient.id == recipient_id,
            ).one()
            upsert_operational_alert(
                db,
                project_id=recipient.project_id,
                dedupe_key=f"campaign-ledger:{recipient.id}",
                alert_type="campaign_ledger_reconciliation_failed",
                title="Campaign contact ledger requires reconciliation",
                message=str(exc),
                severity="error",
                context={
                    "campaign_id": recipient.run.campaign_id,
                    "run_id": recipient.run_id,
                    "recipient_id": recipient.id,
                    "send_log_id": recipient.send_log_id,
                },
            )
            db.commit()
        self._finish_recipient(db, recipient, status)

    def _retry_or_fail(self, db: Session, recipient: CampaignRecipient, error: str) -> None:
        recipient.last_error = str(error)[:4000]
        if recipient.last_error_code != _CAPACITY_MARKER:
            recipient.last_error_code = str(error)[:100]
        if int(recipient.attempt_count or 0) < 3 and not recipient.run.cancel_requested:
            recipient.status = "pending"
            recipient.scheduled_at = datetime.utcnow() + timedelta(minutes=2 ** int(recipient.attempt_count or 1))
            self._return_attention_to_selection(
                db, recipient, recipient.scheduled_at, "campaign_retry",
            )
            db.commit()
            self._refresh_run(db, recipient.run_id, recipient.wave_id)
            return
        self._finish_recipient(db, recipient, "failed", str(error))
        upsert_operational_alert(
            db,
            project_id=recipient.project_id,
            dedupe_key=f"campaign-delivery:{recipient.run_id}:{recipient.delivery_profile_id}",
            alert_type="campaign_delivery_failed",
            title="Campaign delivery failures require attention",
            message=str(error),
            severity="error",
            context={
                "campaign_id": recipient.run.campaign_id,
                "run_id": recipient.run_id,
                "delivery_profile_id": recipient.delivery_profile_id,
                "recipient_id": recipient.id,
            },
        )
        db.commit()

    def _requeue_for_campaign_pause(self, db: Session, recipient: CampaignRecipient) -> None:
        """Pause is a dispatch gate, not a failure or an attempt-consuming retry."""
        recipient.status = "pending"
        recipient.attempt_count = max(0, int(recipient.attempt_count or 0) - 1)
        recipient.scheduled_at = max(
            recipient.scheduled_at or datetime.utcnow(),
            datetime.utcnow() + timedelta(seconds=self.poll_interval),
        )
        recipient.last_error = "Campaign paused before dispatch"
        if recipient.last_error_code != _CAPACITY_MARKER:
            recipient.last_error_code = "campaign_paused"
        self._return_attention_to_selection(
            db, recipient, recipient.scheduled_at, "campaign_paused",
        )
        db.commit()
        self._refresh_run(db, recipient.run_id, recipient.wave_id)

    def _requeue_for_delivery_pause(
        self,
        db: Session,
        recipient: CampaignRecipient,
        reason: str,
    ) -> None:
        """Park profile/sender pauses without spending a delivery attempt."""
        recipient.status = "pending"
        recipient.attempt_count = max(0, int(recipient.attempt_count or 0) - 1)
        recipient.scheduled_at = max(
            recipient.scheduled_at or datetime.utcnow(),
            datetime.utcnow() + timedelta(seconds=self.poll_interval),
        )
        recipient.last_error = "Campaign delivery is paused by its sender configuration"
        if recipient.last_error_code != _CAPACITY_MARKER:
            recipient.last_error_code = reason
        self._return_attention_to_selection(
            db, recipient, recipient.scheduled_at, reason,
        )
        db.commit()
        self._refresh_run(db, recipient.run_id, recipient.wave_id)

    def _requeue_existing_deferral(
        self,
        db: Session,
        recipient: CampaignRecipient,
        send_log: SendLog,
        reason: str | None = None,
    ) -> None:
        """Keep a deferred SendLog and its pace reservation campaign-owned."""
        now = datetime.utcnow()
        resume_at = send_log.scheduled_at or now + timedelta(minutes=5)
        recipient.status = "pending"
        recipient.attempt_count = max(0, int(recipient.attempt_count or 0) - 1)
        recipient.scheduled_at = max(resume_at, now + timedelta(seconds=self.poll_interval))
        recipient.send_log_id = send_log.id
        recipient.last_error = reason or "Campaign delivery deferred; queued for strict revalidation"
        if recipient.last_error_code != _CAPACITY_MARKER:
            recipient.last_error_code = "campaign_deferred"
        self._return_attention_to_selection(
            db, recipient, recipient.scheduled_at, "campaign_deferred",
        )
        db.commit()
        self._refresh_run(db, recipient.run_id, recipient.wave_id)

    def _return_attention_to_selection(
        self,
        db: Session,
        recipient: CampaignRecipient,
        retry_at: datetime,
        reason: str,
    ) -> None:
        """Release a campaign reservation whenever dispatch is postponed."""
        if not recipient.send_log_id:
            return
        send_log = db.query(SendLog).filter(
            SendLog.id == recipient.send_log_id,
            SendLog.project_id == recipient.project_id,
            SendLog.source_type == "campaign",
            SendLog.source_id == recipient.id,
            SendLog.status.in_(["campaign_selected", "delayed", "deferred"]),
        ).first()
        if not send_log:
            return
        from app.services.channels.selection import acquire_attention_xact_lock
        acquire_attention_xact_lock(
            db,
            send_log.project_id,
            send_log.user_id,
            send_log.recipient,
            send_log.attention_scope,
        )
        send_log = db.query(SendLog).filter(
            SendLog.id == recipient.send_log_id,
            SendLog.project_id == recipient.project_id,
            SendLog.source_type == "campaign",
            SendLog.source_id == recipient.id,
            SendLog.status.in_(["campaign_selected", "delayed", "deferred"]),
        ).execution_options(populate_existing=True).with_for_update().first()
        if not send_log:
            return
        send_log.status = "candidate"
        send_log.scheduled_at = retry_at
        trace = list(send_log.decision_trace or [])
        trace.append({
            "step": "campaign_attention_released",
            "reason": reason,
            "retry_at": retry_at.isoformat(),
            "ts": datetime.utcnow().isoformat(),
        })
        send_log.decision_trace = trace
        db.flush()

    def _reclaim_campaign_deferrals(self, db: Session, now: datetime) -> int:
        """Migrate legacy deferred recipients back to campaign ownership.

        The SendLog itself is retained so the pace reservation and audit
        identity survive the hand-off from the generic scheduled worker.
        """
        rows = db.query(CampaignRecipient, SendLog).join(
            SendLog,
            SendLog.id == CampaignRecipient.send_log_id,
        ).join(
            CampaignRun,
            CampaignRun.id == CampaignRecipient.run_id,
        ).filter(
            CampaignRecipient.status == "deferred",
            CampaignRun.status.in_(["scheduled", "running"]),
            CampaignRun.cancel_requested == False,  # noqa: E712
            SendLog.project_id == CampaignRecipient.project_id,
            SendLog.source_type == "campaign",
            SendLog.source_id == CampaignRecipient.id,
            SendLog.status.in_(list(_SEND_PENDING)),
        ).order_by(CampaignRecipient.id).limit(self.batch_size * 4).all()
        touched: set[tuple[int, int | None]] = set()
        for recipient, send_log in rows:
            resume_at = send_log.scheduled_at or now + timedelta(minutes=5)
            recipient.status = "pending"
            recipient.scheduled_at = max(resume_at, now + timedelta(seconds=self.poll_interval))
            recipient.send_log_id = send_log.id
            if recipient.last_error_code != _CAPACITY_MARKER:
                recipient.last_error_code = "campaign_deferral_reclaimed"
            touched.add((recipient.run_id, recipient.wave_id))
        if rows:
            db.commit()
        for run_id, wave_id in touched:
            self._refresh_run(db, run_id, wave_id)
        return len(rows)

    def _finish_recipient(
        self,
        db: Session,
        recipient: CampaignRecipient,
        status: str,
        reason: str | None = None,
    ) -> None:
        now = datetime.utcnow()
        if status in {"canceled", "skipped", "failed"}:
            # Last-line defense for cancellation racing a deferred/submitting
            # SendLog linkage, and for a selected reservation whose campaign
            # becomes ineligible before provider I/O. Provider-terminal rows
            # are never rewritten.
            send_status = (
                "canceled" if status == "canceled"
                else "failed" if status == "failed"
                else "skipped"
            )
            pending_log = db.query(SendLog).filter(
                SendLog.project_id == recipient.project_id,
                SendLog.source_type == "campaign",
                SendLog.source_id == recipient.id,
                SendLog.status.in_(list(_SEND_PENDING | {_SEND_SUBMITTING})),
            ).order_by(SendLog.id.desc()).first()
            if pending_log:
                from app.services.channels.selection import acquire_attention_xact_lock
                acquire_attention_xact_lock(
                    db,
                    pending_log.project_id,
                    pending_log.user_id,
                    pending_log.recipient,
                    pending_log.attention_scope,
                )
            db.query(SendLog).filter(
                SendLog.project_id == recipient.project_id,
                SendLog.source_type == "campaign",
                SendLog.source_id == recipient.id,
                SendLog.status.in_(list(_SEND_PENDING | {_SEND_SUBMITTING})),
            ).update({
                SendLog.status: send_status,
                SendLog.error_message: reason or f"Campaign recipient {status}",
                SendLog.failed_at: now,
            }, synchronize_session=False)
        recipient.status = status
        recipient.suppression_reason = reason if status in {"skipped", "canceled"} else recipient.suppression_reason
        if status == "failed":
            recipient.last_error = reason
            recipient.last_error_code = (reason or "failed")[:100]
        elif recipient.last_error_code == _CAPACITY_MARKER:
            recipient.last_error_code = None
        if status in {"sent", "delivered"}:
            recipient.sent_at = recipient.sent_at or now
        recipient.completed_at = now
        run_id, wave_id = recipient.run_id, recipient.wave_id
        db.commit()
        self._refresh_run(db, run_id, wave_id)

    def _reconcile_send_logs(self, db: Session) -> int:
        rows = db.query(CampaignRecipient, SendLog).join(
            SendLog,
            SendLog.id == CampaignRecipient.send_log_id,
        ).filter(
            or_(
                and_(
                    CampaignRecipient.status == "deferred",
                    SendLog.status.in_(list(_SEND_SUCCESS | _SEND_FAILURE)),
                ),
                and_(
                    CampaignRecipient.status == "sent",
                    SendLog.status.in_(["delivered", "read", "opened", "failed", "exhausted", "blocked"]),
                ),
            ),
        ).limit(self.batch_size * 4).all()
        touched: set[tuple[int, int | None]] = set()
        for recipient, log in rows:
            if log.status in {"delivered", "read", "opened"}:
                recipient.status = "delivered"
                recipient.sent_at = recipient.sent_at or log.sent_at or datetime.utcnow()
                recipient.completed_at = log.delivered_at or datetime.utcnow()
            elif log.status == "sent":
                recipient.status = "sent"
                recipient.sent_at = log.sent_at or datetime.utcnow()
                recipient.completed_at = recipient.completed_at or datetime.utcnow()
            else:
                recipient.status = "failed"
                recipient.last_error = log.error_message
                recipient.last_error_code = log.status
                recipient.completed_at = datetime.utcnow()
                if log.status == "submission_unknown":
                    self._upsert_submission_unknown_alert(
                        db,
                        recipient,
                        log.error_message or "Provider submission outcome is unknown",
                    )
            touched.add((recipient.run_id, recipient.wave_id))
        if rows:
            db.commit()
        for run_id, wave_id in touched:
            self._refresh_run(db, run_id, wave_id)
        return len(rows)

    @staticmethod
    def _message_id(recipient: CampaignRecipient) -> str:
        digest = hashlib.sha256(
            f"{recipient.project_id}:{recipient.id}:{recipient.idempotency_key}".encode()
        ).hexdigest()
        return f"<campaign-{digest}@versya.io>"

    def _terminalize_submission_unknown(
        self,
        db: Session,
        recipient: CampaignRecipient,
        detail: str,
    ) -> None:
        self._upsert_submission_unknown_alert(db, recipient, detail)
        if recipient.user_id:
            from app.services.scoring.policy_service import PolicyService
            send_log = db.query(SendLog).filter(
                SendLog.id == recipient.send_log_id,
                SendLog.project_id == recipient.project_id,
            ).first()
            if not send_log:
                raise RuntimeError("Unknown campaign submission has no SendLog")
            PolicyService(db).record_contact_once(
                project_id=recipient.project_id,
                user_id=recipient.user_id,
                channel=recipient.channel,
                source="campaign",
                source_id=send_log.id,
                sent_at=send_log.sent_at or datetime.utcnow(),
            )
        self._finish_recipient(db, recipient, "failed", "submission_unknown")

    @staticmethod
    def _upsert_submission_unknown_alert(
        db: Session,
        recipient: CampaignRecipient,
        detail: str,
    ) -> None:
        upsert_operational_alert(
            db,
            project_id=recipient.project_id,
            dedupe_key=f"campaign-submission-unknown:{recipient.id}",
            alert_type="campaign_submission_unknown",
            title="Campaign provider submission outcome is unknown",
            message=str(detail)[:4000],
            severity="error",
            context={
                "campaign_id": recipient.run.campaign_id,
                "run_id": recipient.run_id,
                "delivery_profile_id": recipient.delivery_profile_id,
                "recipient_id": recipient.id,
                "send_log_id": recipient.send_log_id,
            },
        )

    def _finalize_quiescent_runs(self, db: Session, limit: int = 100) -> int:
        """Repair run counters after a crash between recipient and run commits."""
        active_recipient = select(CampaignRecipient.id).where(
            CampaignRecipient.run_id == CampaignRun.id,
            CampaignRecipient.status.in_(["pending", "processing", "deferred", "held"]),
        )
        rows = db.query(CampaignRun.id).filter(
            CampaignRun.status.in_(["scheduled", "running", "cancel_requested"]),
            ~active_recipient.exists(),
        ).order_by(CampaignRun.id).limit(limit).all()
        for (run_id,) in rows:
            self._refresh_run(db, run_id, None)
        return len(rows)

    def _refresh_run(self, db: Session, run_id: int, wave_id: int | None) -> None:
        if wave_id:
            wave = db.query(CampaignWave).filter(CampaignWave.id == wave_id).first()
            if wave:
                counts = dict(db.query(
                    CampaignRecipient.status,
                    func.count(CampaignRecipient.id),
                ).filter(CampaignRecipient.wave_id == wave_id).group_by(CampaignRecipient.status).all())
                wave.sent_count = counts.get("sent", 0) + counts.get("delivered", 0)
                wave.failed_count = counts.get("failed", 0)
                wave.skipped_count = counts.get("skipped", 0)
                active = sum(counts.get(status, 0) for status in ("pending", "processing", "deferred", "held"))
                if active == 0:
                    wave.status = "completed"
                    wave.finished_at = wave.finished_at or datetime.utcnow()

        run = db.query(CampaignRun).filter(CampaignRun.id == run_id).first()
        if not run:
            db.commit()
            return
        counts = dict(db.query(
            CampaignRecipient.status,
            func.count(CampaignRecipient.id),
        ).filter(CampaignRecipient.run_id == run_id).group_by(CampaignRecipient.status).all())
        run.sent_count = counts.get("sent", 0) + counts.get("delivered", 0)
        run.delivered_count = counts.get("delivered", 0)
        run.failed_count = counts.get("failed", 0)
        run.skipped_count = counts.get("skipped", 0)
        run.canceled_count = counts.get("canceled", 0)
        active = sum(counts.get(status, 0) for status in ("pending", "processing", "deferred", "held"))
        if active == 0:
            run.status = "canceled" if run.cancel_requested else "completed"
            run.finished_at = run.finished_at or datetime.utcnow()
            reservations = db.query(ChannelCapacityReservation).filter(
                ChannelCapacityReservation.run_id == run.id,
                ChannelCapacityReservation.status.in_(["reserved", "active"]),
            ).all()
            for reservation in reservations:
                reservation.units_reserved = int(reservation.units_consumed or 0)
                reservation.status = "consumed" if reservation.units_consumed else "released"
                if not reservation.units_consumed:
                    reservation.released_at = datetime.utcnow()
            from app.services.decision_gate_service import DecisionGateService
            DecisionGateService(db).reconcile_run(run)
        db.commit()


campaign_worker = CampaignWorker()
