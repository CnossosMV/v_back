"""
Scheduled Send Worker — background worker for processing delayed/scheduled sends.

Polls every 30 seconds for send_logs with status in ('delayed', 'deferred') and
scheduled_at <= NOW(), then re-executes them through the SendService.

Deferred sends (created by DeferredSendHelper when policy blocks a message)
are re-rendered from render_context at send time, checked for expiration,
and policy-rechecked before delivery.
"""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 738002  # unique ID for scheduled send worker


class ScheduledSendWorker:
    """Background worker that processes scheduled/delayed/deferred sends."""

    def __init__(self, poll_interval: int = 30, batch_size: int = 50, clock=None):
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._running = False
        self._task: Optional[asyncio.Task] = None
        from app.services.clock import SystemClock
        self.clock = clock or SystemClock()

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Scheduled send worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info(
            "Scheduled send worker started (poll=%ds, batch=%d)",
            self.poll_interval, self.batch_size,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduled send worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = await self._process_cycle(db)
                    if processed > 0:
                        logger.info("Scheduled send worker processed %d sends", processed)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Error in scheduled send worker: %s", e)

            await asyncio.sleep(self.poll_interval)

    async def _process_cycle(self, db) -> int:
        """Run one processing cycle with advisory lock."""
        acquired = db.execute(
            text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")
        ).scalar()
        if not acquired:
            return 0

        attention_lock_ids: list[int] = []
        try:
            from app.models import SendLog
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService

            now = self.clock.utcnow()

            # Fetch due work without row locks first.  Row-locking before the
            # per-contact advisory lock would invert CampaignWorker's lock
            # order and permit a deadlock.  The complete contest is reloaded
            # under its slot lock below.
            # Automation pause: parked automation sends for a paused contact
            # stay frozen (no writes, no requeue loop) — excluded from the
            # dequeue until the contact is resumed, then they're past-due and
            # flow on the next cycle. Manual-lane and retry rows keep flowing.
            from sqlalchemy import or_
            from app.models.messaging import MessagingUser
            from app.services.messaging.contact_pause_service import AUTOMATION_SOURCE_TYPES

            logs = db.query(SendLog).outerjoin(
                MessagingUser, SendLog.user_id == MessagingUser.id,
            ).filter(
                SendLog.status.in_(["delayed", "deferred", "candidate"]),
                SendLog.scheduled_at <= now,
                # Campaign deferrals remain CampaignWorker-owned, but campaign
                # candidates must enter this shared attention contest.
                or_(SendLog.source_type != "campaign", SendLog.status == "candidate"),
                or_(
                    SendLog.source_type.notin_(AUTOMATION_SOURCE_TYPES),
                    MessagingUser.id.is_(None),
                    MessagingUser.automations_paused == False,  # noqa: E712
                ),
            ).order_by(
                SendLog.priority.desc(),
                SendLog.scheduled_at.asc(),
            ).limit(self.batch_size).all()

            if not logs:
                return 0

            # Lock every seeded attention slot in stable bigint order.  These
            # session locks remain held through winner dispatch and all loser
            # writes, so a concurrent contender is deterministically ordered
            # after this cycle instead of appearing between selection and I/O.
            from app.services.channels.selection import (
                acquire_attention_session_lock,
                attention_lock_id,
                due_attention_contest,
                eligible_candidates,
                release_attention_session_lock,
            )
            slot_anchors = {}
            for seed in logs:
                lock_id = attention_lock_id(
                    seed.project_id,
                    seed.user_id,
                    seed.recipient,
                    seed.attention_scope,
                )
                slot_anchors.setdefault(lock_id, []).append(seed)
            for lock_id in sorted(slot_anchors):
                seed = slot_anchors[lock_id][0]
                acquired_lock_id = acquire_attention_session_lock(
                    db,
                    seed.project_id,
                    seed.user_id,
                    seed.recipient,
                    seed.attention_scope,
                )
                if acquired_lock_id is not None:
                    attention_lock_ids.append(acquired_lock_id)

            # The seed LIMIT is only a work bound, never an arbitration bound.
            # Expand every seeded contact/scope to its full due contest while
            # holding row locks, otherwise two candidates split by the LIMIT
            # could each be mistaken for the winner.
            seen_ids = {log.id for log in logs}
            for seeds in slot_anchors.values():
                for seed in seeds:
                    contest = due_attention_contest(db, seed, now, lock=True)
                    for contender in contest:
                        if contender.id not in seen_ids:
                            logs.append(contender)
                            seen_ids.add(contender.id)

            # --- Phase 2: Selection pass ---
            # Group this batch of due sends by contact and pick one winner per
            # slot. 'shadow' logs the contest read-only (proves the arbiter
            # against real same-contact collisions, changes nothing). 'enforce'
            # holds the losers — only the winner dispatches this cycle; losers
            # are re-deferred a cycle and re-contested (relevance preserved).
            _selection_skip: set = set()
            _selection_winners: set = set()
            _selection_hold_until: dict = {}
            from app.services.channels.selection import (
                arbitrate_due_attention, future_plan_mode, group_by_contact,
                selection_mode,
            )
            _selection_is_binding = any(
                selection_mode(db, getattr(row, "project_id", None)) == "enforce"
                for row in logs
            )
            try:
                for _key, _group in group_by_contact(
                    eligible_candidates(logs, now)
                ).items():
                    _sel_mode = selection_mode(db, getattr(_group[0], "project_id", None))
                    if _sel_mode == "off":
                        continue
                    _future_mode = future_plan_mode(
                        db, getattr(_group[0], "project_id", None)
                    )
                    if len(_group) < 2 and _future_mode == "off":
                        continue
                    _decision = arbitrate_due_attention(
                        db, _group, now, lock_future=True,
                    )
                    _r = _decision["due"]
                    _w = _decision["dispatch_winner"]
                    _owner = _decision["attention_owner"]
                    if _sel_mode == "enforce" and getattr(_w, "id", None) is not None:
                        _selection_winners.add(_w.id)
                    _loser_ids = [
                        row.id for row in _group
                        if getattr(_w, "id", None) != row.id
                    ]
                    logger.info(
                        "[selection][%s][future=%s] contact=%s due=%d dispatch=send_log:%s "
                        "owner=send_log:%s consequence=%s losers=%s",
                        _sel_mode, _decision["mode"], _key, len(_group),
                        getattr(_w, "id", None), getattr(_owner, "id", None),
                        _decision["consequence_at_gate"], _loser_ids,
                    )
                    # Bandit informs selection (shadow): log the channel
                    # recommendation for the winning slot. Read-only — the
                    # interventional act-on-it path is a later flag-gated step.
                    _wslot = getattr(_w, "slot_id", None) if _w is not None else None
                    from app.services.channels.consolidation import bandit_act_mode
                    _bandit_mode = bandit_act_mode(db, getattr(_w, "project_id", None))
                    if _wslot and _bandit_mode != "off":
                        try:
                            from app.services.bandit.reward import RewardService
                            _rec = RewardService(db).recommendation(
                                getattr(_w, "project_id", None), "channel", _wslot,
                            )
                            _g = _rec["gate"]
                            if _rec["arms"]:
                                logger.info(
                                    "[bandit][%s] slot=%s gate=%s challenger=%s eff=%s conf=%s",
                                    _bandit_mode, _wslot, _g["earned_auto"],
                                    _g["challenger_arm"], _g["effect_size"], _g["confidence"],
                                )
                            # Interventional: re-target the winner's channel to
                            # the earned-auto arm (execution-only).
                            from app.services.channels.registry import ChannelRegistry
                            _arm = _g["challenger_arm"]
                            if (_bandit_mode == "enforce" and _g["earned_auto"]
                                    and _arm and _arm != getattr(_w, "preferred_channel", None)
                                    and ChannelRegistry.get_adapter(_arm) is not None):
                                _w.preferred_channel = _arm
                                _w.fallback_order = [_arm]
                                logger.info(
                                    "[bandit][enforce] slot=%s re-targeted channel -> %s",
                                    _wslot, _arm,
                                )
                        except Exception as _be:
                            logger.debug("[bandit] reco failed: %s", _be)
                    if _sel_mode == "enforce":
                        _selection_skip.update(i for i in _loser_ids if i is not None)
                        if _decision.get("hold_until") is not None:
                            for _loser_id in _loser_ids:
                                _selection_hold_until[_loser_id] = _decision["hold_until"]
                        # Annotate each held loser's trace with what it lost
                        # to, so the Decision view can explain the contest.
                        # Losers aren't dispatched this cycle, so their trace
                        # is safe to write (winner's would be overwritten).
                        for _loser in [row for row in _group if row.id in _loser_ids]:
                            _tr = list(getattr(_loser, "decision_trace", None) or [])
                            _tr.append({
                                "step": (
                                    "future_reservation_hold"
                                    if _decision.get("hold_until") is not None
                                    else "selection_lost"
                                ),
                                "lost_to": getattr(_owner, "id", None),
                                "winner_tier": getattr(_owner, "intent_tier", None),
                                "candidates": len(_group),
                                "consequence_at_gate": _decision["consequence_at_gate"],
                                "recheck_at": (
                                    _decision["hold_until"].isoformat()
                                    if _decision.get("hold_until") is not None
                                    else None
                                ),
                                "ts": now.isoformat(),
                            })
                            _loser.decision_trace = _tr
            except Exception as _e:
                logger.warning("[selection] pass failed: %s", _e)
                if _selection_is_binding:
                    # An authoritative gate must never fail open.  Abort this
                    # cycle before provider I/O; the next poll recomputes the
                    # whole contest from durable intents.
                    raise RuntimeError(
                        "Authoritative attention selection failed closed"
                    ) from _e

            # Keep one slot contiguous and place its enforce-mode winner last.
            # Loser/expiry mutations are therefore durable before the winner
            # dispatches, and the slot lock can be released immediately after
            # that group instead of blocking every contact for the full batch.
            grouped_logs = {}
            for log in logs:
                lock_id = attention_lock_id(
                    log.project_id, log.user_id, log.recipient, log.attention_scope,
                )
                grouped_logs.setdefault(lock_id, []).append(log)
            logs = []
            for lock_id in grouped_logs:
                group_logs = grouped_logs[lock_id]
                logs.extend([
                    log for log in group_logs if log.id not in _selection_winners
                ])
                logs.extend([
                    log for log in group_logs if log.id in _selection_winners
                ])
            slot_last_index = {
                attention_lock_id(
                    log.project_id, log.user_id, log.recipient, log.attention_scope,
                ): index
                for index, log in enumerate(logs)
            }

            # --- Pace governor: per-domain per-cycle dispatch cap ---
            # Second smoothing layer on top of the enqueue-time reserve: even
            # when many rows for one sending domain come due together, dispatch
            # at most (max_per_minute * poll_interval/60) of them per cycle and
            # re-defer the overflow, so the provider never gets a burst.
            from app.services.channels.send_pace_service import SendPaceService, pace_mode
            _domain_counts: dict = {}
            _domain_quota: dict = {}
            _pace_svc = SendPaceService(db, clock=self.clock)

            def _cycle_quota(domain: str, project_id) -> int:
                if domain not in _domain_quota:
                    try:
                        pol = _pace_svc.resolve_policy(domain, project_id)
                        _domain_quota[domain] = max(1, int(round(pol.max_per_minute * self.poll_interval / 60.0)))
                    except Exception:
                        _domain_quota[domain] = self.batch_size
                return _domain_quota[domain]

            processed = 0
            for log_index, log in enumerate(logs):
                log_slot_lock_id = attention_lock_id(
                    log.project_id, log.user_id, log.recipient, log.attention_scope,
                )
                try:
                    # --- Selection: loser held this cycle (enforce mode) ---
                    # Re-defer ~1 cycle and re-contest next pass. Don't change
                    # status (relevance preserved); skip expiry-passed rows so a
                    # perishable loser still expires rather than looping forever.
                    if log.id in _selection_skip and not (log.expires_at and log.expires_at <= now):
                        if log.source_type == "campaign" and log.status == "campaign_selected":
                            # A newer/higher contender revokes the campaign's
                            # reservation. It must win Selection again before
                            # CampaignWorker may approach provider I/O.
                            log.status = "candidate"
                            trace = list(log.decision_trace or [])
                            trace.append({
                                "step": "campaign_selection_revoked",
                                "reason": "higher_or_more_perishable_contender",
                                "ts": now.isoformat(),
                            })
                            log.decision_trace = trace
                        next_check = _selection_hold_until.get(
                            log.id, now + timedelta(seconds=self.poll_interval)
                        )
                        if log.expires_at:
                            next_check = min(next_check, log.expires_at)
                        log.scheduled_at = next_check
                        db.commit()
                        processed += 1
                        continue

                    # --- Expiration check ---
                    if log.expires_at and log.expires_at <= now:
                        log.status = "expired"
                        log.failed_at = now
                        log.error_message = "Deferred send expired before delivery"
                        if log.deferred_source_enrollment_id:
                            self._handle_funnel_resolution(db, log, "expired")
                        db.commit()
                        processed += 1
                        logger.info("Deferred send %d expired", log.id)
                        continue

                    if log.source_type == "campaign" and log.status == "candidate":
                        # Selection decides attention; CampaignWorker retains
                        # provider submission, permission and endpoint duties.
                        if selection_mode(db, log.project_id) != "enforce":
                            log.scheduled_at = now + timedelta(seconds=self.poll_interval)
                            trace = list(log.decision_trace or [])
                            trace.append({
                                "step": "campaign_selection_held",
                                "reason": "selection_not_enforced",
                                "ts": now.isoformat(),
                            })
                            log.decision_trace = trace
                            db.commit()
                            processed += 1
                            continue
                        log.status = "campaign_selected"
                        trace = list(log.decision_trace or [])
                        trace.append({
                            "step": "campaign_selected",
                            "ts": now.isoformat(),
                        })
                        log.decision_trace = trace
                        db.commit()
                        processed += 1
                        continue

                    if log.source_type == "campaign" and log.status == "campaign_selected":
                        # This row is a revocable attention reservation owned
                        # by CampaignWorker, never generic provider work.
                        db.commit()
                        processed += 1
                        continue

                    # --- Pace: per-domain per-cycle cap (re-defer overflow) ---
                    if pace_mode(db, log.project_id) == "enforce" and log.sending_domain:
                        _dom = log.sending_domain
                        _q = _cycle_quota(_dom, log.project_id)
                        if _domain_counts.get(_dom, 0) >= _q:
                            log.scheduled_at = now + timedelta(seconds=self.poll_interval)
                            db.commit()
                            processed += 1
                            continue
                        _domain_counts[_dom] = _domain_counts.get(_dom, 0) + 1

                    # --- Baseline revalidation for funnel-deferred sends ---
                    if log.deferred_source_enrollment_id:
                        if not self._baseline_revalidation(db, log):
                            log.status = "skipped"
                            log.failed_at = now
                            log.error_message = "Baseline revalidation failed"
                            self._handle_funnel_resolution(db, log, "revalidation_failed")
                            db.commit()
                            processed += 1
                            continue

                    # --- User-defined revalidation conditions ---
                    if log.render_context and log.render_context.get("revalidation_condition"):
                        if not self._evaluate_revalidation(db, log):
                            log.status = "skipped"
                            log.failed_at = now
                            log.error_message = "Revalidation condition not met at dispatch time"
                            self._handle_funnel_resolution(db, log, "revalidation_failed")
                            db.commit()
                            processed += 1
                            continue

                    # --- Clear recoverable opt-outs before sending ---
                    if log.status == "deferred" and log.user_id and log.preferred_channel:
                        try:
                            from app.models.messaging import MessagingUser as _MU
                            _user = db.query(_MU).filter(_MU.id == log.user_id).first()
                            if _user:
                                _opted = list(_user.opted_out_channels or [])
                                if log.preferred_channel in _opted:
                                    from app.models import DeliveryFeedback as _DF
                                    _recoverable = db.query(_DF).filter(
                                        _DF.user_id == log.user_id,
                                        _DF.channel == log.preferred_channel,
                                        _DF.provider_code.in_(["131026", "470"]),
                                    ).first()
                                    if not _recoverable:
                                        _recoverable = db.query(_DF).filter(
                                            _DF.user_id == log.user_id,
                                            _DF.channel == log.preferred_channel,
                                            _DF.feedback_type.in_(["temporarily_unreachable", "policy_violation"]),
                                        ).first()
                                    if _recoverable:
                                        _opted.remove(log.preferred_channel)
                                        _user.opted_out_channels = _opted
                                        db.flush()
                                        logger.info(
                                            "Cleared recoverable opt-out for user %d channel %s before deferred send %d",
                                            log.user_id, log.preferred_channel, log.id,
                                        )
                        except Exception as e:
                            logger.warning("Error clearing opt-out for deferred send %d: %s", log.id, e)

                    # --- Policy re-check for deferred sends ---
                    if log.status == "deferred" and log.user_id:
                        try:
                            from app.services.scoring.policy_service import PolicyService
                            recheck = PolicyService(db).check_can_contact(
                                project_id=log.project_id,
                                user_id=log.user_id,
                                channel=log.channel,
                                source=log.source_type,
                                source_id=log.source_id,
                            )
                            if not recheck.allowed:
                                if recheck.deferrable and recheck.defer_until:
                                    # Re-defer to new time
                                    log.scheduled_at = recheck.defer_until
                                    db.commit()
                                    logger.info(
                                        "Re-deferred send %d to %s",
                                        log.id, recheck.defer_until,
                                    )
                                else:
                                    # Non-deferrable block → drop
                                    log.status = "skipped"
                                    log.failed_at = now
                                    log.error_message = f"Policy blocked at send time: {recheck.reason}"
                                    if log.deferred_source_enrollment_id:
                                        self._handle_funnel_resolution(db, log, "skipped")
                                    db.commit()
                                    logger.info(
                                        "Deferred send %d dropped (non-deferrable block): %s",
                                        log.id, recheck.reason,
                                    )
                                processed += 1
                                continue
                        except Exception as e:
                            logger.warning("Policy re-check error for send %d (proceeding): %s", log.id, e)

                    # --- Policy re-check for retry rows (shadow by default) ---
                    # Retries are status='delayed', so they skip the deferred
                    # recheck above. A multi-attempt retry can fire after the
                    # contact's policy state changed (new cap/cooldown/opt-out).
                    # Ship count-only first: enforce only when
                    # SEND_RETRY_POLICY_ENFORCE is truthy, else log would-suppress.
                    if log.status == "delayed" and log.source_type == "retry" and log.user_id:
                        try:
                            from app.services.scoring.policy_service import PolicyService
                            recheck = PolicyService(db).check_can_contact(
                                project_id=log.project_id,
                                user_id=log.user_id,
                                channel=log.channel or log.preferred_channel,
                                source=log.source_type,
                                source_id=log.source_id,
                            )
                            if not recheck.allowed:
                                from app.services.engine_rollout_service import effective_mode
                                mode = effective_mode(db, log.project_id, "retry_policy")
                                if mode != "enforce":
                                    logger.info(
                                        "[shadow] WA retry %d WOULD be suppressed by "
                                        "policy: %s (deferrable=%s)",
                                        log.id, recheck.reason, recheck.deferrable,
                                    )
                                elif recheck.deferrable and recheck.defer_until:
                                    log.scheduled_at = recheck.defer_until
                                    db.commit()
                                    logger.info(
                                        "Re-deferred WA retry %d to %s",
                                        log.id, recheck.defer_until,
                                    )
                                    processed += 1
                                    continue
                                else:
                                    log.status = "skipped"
                                    log.failed_at = now
                                    log.error_message = (
                                        f"Policy blocked retry at send time: {recheck.reason}"
                                    )
                                    if log.deferred_source_enrollment_id:
                                        self._handle_funnel_resolution(db, log, "skipped")
                                    db.commit()
                                    logger.info(
                                        "WA retry %d dropped (policy block): %s",
                                        log.id, recheck.reason,
                                    )
                                    processed += 1
                                    continue
                        except Exception as e:
                            logger.warning(
                                "Retry policy re-check error for send %d (proceeding): %s",
                                log.id, e,
                            )

                    # --- Re-render content for deferred sends ---
                    if log.status == "deferred" and log.render_context:
                        content = self._re_render_content(db, log)
                        if not content:
                            log.status = "failed"
                            log.failed_at = now
                            log.error_message = "Failed to re-render deferred content"
                            if log.deferred_source_enrollment_id:
                                self._handle_funnel_resolution(db, log, "failed")
                            db.commit()
                            processed += 1
                            continue
                    elif log.content_payload:
                        content = OutboundContent.from_dict(log.content_payload)
                    else:
                        content = OutboundContent(
                            content_type=log.content_type,
                            text=log.content_summary,
                        )

                    svc = SendService(db)
                    # For deferred sends with no stored fallback_order, restrict to
                    # the intended channel only — avoids cross-channel fallback (e.g.
                    # trying email for a phone-number WhatsApp recipient).
                    effective_fallback = log.fallback_order
                    if effective_fallback is None and log.preferred_channel:
                        effective_fallback = [log.preferred_channel]

                    # Resolve instance from render_context or original log.
                    # instance_id can be at top level (new logs) or nested in
                    # render_context.config (old logs before the fix).
                    instance_config = None
                    if log.render_context:
                        iid = log.render_context.get("instance_id")
                        if not iid:
                            cfg = log.render_context.get("config")
                            if isinstance(cfg, dict):
                                iid = cfg.get("instance_id")
                        if iid:
                            instance_config = {"instance_id": iid}
                    if not instance_config and log.instance_id:
                        instance_config = {"instance_id": log.instance_id}

                    # Propagate retry chain context so the new SendLog created
                    # by SendService stays linked to the same ancestor. Without
                    # this, the retry scheduler can't tell that the dispatched
                    # log is already a retry and would treat its eventual
                    # failure as a fresh first attempt.
                    propagated_retry_of_id = (
                        log.retry_of_id
                        if (log.source_type == "retry" or log.retry_of_id)
                        else None
                    )

                    decision = await svc.send(
                        project_id=log.project_id,
                        user_id=log.user_id,
                        recipient=log.recipient,
                        content=content,
                        channel=log.preferred_channel,
                        fallback_order=effective_fallback,
                        source_type=log.source_type,
                        source_id=log.source_id,
                        template_id=log.template_id,
                        instance_config=instance_config,
                        render_context=log.render_context,
                        deferred_source_enrollment_id=log.deferred_source_enrollment_id,
                        retry_of_id=propagated_retry_of_id,
                        # Finalize THIS row in place — no second SendLog row.
                        existing_send_log_id=log.id,
                    )

                    # Branch on the real outcome, not decision.success: a
                    # quiet-hours re-defer returns success=True/status="delayed"
                    # with nothing delivered. Treating that as "sent" was the
                    # phantom-sent bug (ledger recorded + enrollment resumed for
                    # a message that never left).
                    if decision.status == "sent":
                        # send() already updated this row to sent (provider id +
                        # real content). Record attention + advance enrollment.
                        # When send() is the ledger writer (0D), skip the legacy
                        # write here to avoid double-counting.
                        from app.services.channels.consolidation import ledger_in_send_enabled
                        if log.render_context and log.user_id and not ledger_in_send_enabled(db, log.project_id):
                            self._record_contact_ledger(db, log)
                        if log.deferred_source_enrollment_id:
                            self._resume_funnel_enrollment(db, log)
                    elif decision.status == "delayed":
                        # Re-deferred in place (e.g. quiet hours active at
                        # dispatch). Nothing delivered — no ledger, no resume.
                        logger.info(
                            "Dispatch of send %d re-deferred to %s",
                            log.id, log.scheduled_at,
                        )
                    else:
                        # failed / exhausted / blocked / skipped — row already terminal
                        # from send(). Advance any stuck funnel enrollment.
                        if log.deferred_source_enrollment_id:
                            self._handle_funnel_resolution(db, log, decision.status or "failed")

                    db.commit()
                    processed += 1
                except Exception as e:
                    logger.error("Failed to process scheduled send %d: %s", log.id, e)
                    log.status = "failed"
                    log.failed_at = self.clock.utcnow()
                    log.error_message = str(e)
                    db.commit()
                    processed += 1
                finally:
                    if slot_last_index.get(log_slot_lock_id) == log_index:
                        release_attention_session_lock(db, log_slot_lock_id)
                        if log_slot_lock_id in attention_lock_ids:
                            attention_lock_ids.remove(log_slot_lock_id)

            return processed
        finally:
            from app.services.channels.selection import release_attention_session_lock
            for lock_id in reversed(attention_lock_ids):
                release_attention_session_lock(db, lock_id)
            db.execute(
                text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})")
            )

    def _re_render_content(self, db, log):
        """Re-render template content from render_context with fresh user data."""
        from app.services.channels.base import OutboundContent

        ctx = log.render_context
        if not ctx:
            return None

        template_id = ctx.get("template_id") or log.template_id
        variables = ctx.get("variables", {})

        # Fetch fresh user data to update variables
        if log.user_id:
            try:
                from app.models.messaging import MessagingUser
                user = db.query(MessagingUser).filter(
                    MessagingUser.id == log.user_id,
                ).first()
                if user:
                    if user.name:
                        variables["name"] = user.name
                        variables["first_name"] = user.name.split()[0]
                    if user.email:
                        variables["email"] = user.email
                    if user.phone:
                        variables["phone"] = user.phone
                    if user.external_id:
                        variables["external_id"] = user.external_id
                    for k, v in (user.properties or {}).items():
                        variables[k] = v
            except Exception as e:
                logger.warning("Error fetching fresh user data for deferred send %d: %s", log.id, e)

        # Inject project variables
        try:
            from app.services.project_variable_service import ProjectVariableService
            contact_ctx = {
                "name": variables.get("name", ""),
                "first_name": variables.get("first_name", ""),
                "email": variables.get("email", ""),
                "phone": variables.get("phone", ""),
                "external_id": variables.get("external_id", ""),
            }
            project_variables = ProjectVariableService(db).render_project_variables(
                log.project_id, contact_ctx
            )
            variables["project"] = project_variables
            variables["projects"] = project_variables
        except Exception as e:
            logger.warning("Error injecting project variables for deferred send %d: %s", log.id, e)

        if template_id:
            try:
                from app.models.messaging import MessagingTemplate
                template = db.query(MessagingTemplate).filter(
                    MessagingTemplate.id == template_id,
                ).first()
                if template:
                    from app.services.messaging.template_renderer import template_renderer
                    rendered_body, rendered_subject, _, _ = template_renderer.render_template(
                        template.body, variables, template.subject,
                    )
                    channel = log.channel or "email"
                    if channel == "email":
                        return OutboundContent(
                            content_type="rich",
                            html=rendered_body,
                            subject=rendered_subject,
                        )
                    else:
                        return OutboundContent(
                            content_type="text",
                            text=rendered_body,
                        )
            except Exception as e:
                logger.error("Error re-rendering template for deferred send %d: %s", log.id, e)
                return None

        # Fallback: for action types that don't use templates (e.g., free-form WhatsApp)
        action_type = ctx.get("action_type", "")
        config = ctx.get("config", {})

        # Recover step_config from FunnelStep when render_context.config is empty
        # (bug: old _defer_urgent_send stored config.get("config", {}) on flat step_config → {})
        if not config and log.deferred_source_enrollment_id and ctx.get("step_id"):
            try:
                from app.models import FunnelStep
                step = db.query(FunnelStep).filter(
                    FunnelStep.id == ctx["step_id"],
                ).first()
                if step and step.step_config:
                    config = step.step_config
                    logger.info("Recovered step_config from FunnelStep %d for send %d", step.id, log.id)
            except Exception as e:
                logger.warning("Error recovering step_config for send %d: %s", log.id, e)

        import re

        def _resolve_nested(data, path):
            keys = path.split('.')
            val = data
            for k in keys:
                if isinstance(val, dict):
                    val = val.get(k)
                else:
                    return None
                if val is None:
                    return None
            return val

        def _replace(match):
            key = match.group(1).strip()
            val = _resolve_nested(variables, key)
            if val is not None:
                return str(val)
            return match.group(0)

        if "whatsapp" in action_type or "send_message" in action_type:
            message_type = config.get("message_type", "text")
            wa_template_name = config.get("template_name")

            # WhatsApp template send — produces OutboundContent that the adapter
            # routes to send_template_message() (works outside 24h window).
            if message_type == "template" and wa_template_name:
                # Resolve wa_variable_mapping into template_components
                wa_components = config.get("template_components")
                wa_mapping = config.get("wa_variable_mapping")
                if wa_mapping and not wa_components:
                    try:
                        resolved = {}
                        for slot, path in wa_mapping.items():
                            val = _resolve_nested(variables, path)
                            if val is not None:
                                resolved[slot] = str(val)
                            else:
                                resolved[slot] = path
                        # Build header + body components from resolved mapping
                        body_params = []
                        header_params = []
                        for slot, val in resolved.items():
                            param_name = slot.split(".", 1)[-1] if "." in slot else slot
                            if slot.startswith("header"):
                                header_params.append({"type": "text", "parameter_name": param_name, "text": val})
                            else:
                                body_params.append({"type": "text", "parameter_name": param_name, "text": val})
                        wa_components = []
                        if header_params:
                            wa_components.append({"type": "header", "parameters": header_params})
                        if body_params:
                            wa_components.append({"type": "body", "parameters": body_params})
                    except Exception as e:
                        logger.warning("Error resolving wa_variable_mapping for send %d: %s", log.id, e)

                return OutboundContent(
                    content_type="template",
                    template_name=wa_template_name,
                    template_language=config.get("template_language", "en_US"),
                    template_components=wa_components,
                )

            # Free-form WhatsApp text
            if config.get("message"):
                message_text = config["message"]
                message_text = re.sub(r'\{\{([\w.]+)\}\}', _replace, message_text)
                return OutboundContent(content_type="text", text=message_text)

        return None

    def _handle_funnel_resolution(self, db, log, reason: str) -> None:
        """On expiry/drop/fail, advance the stuck funnel enrollment past the deferred step."""
        try:
            from app.models import FunnelEnrollment, FunnelStep
            enrollment = db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == log.deferred_source_enrollment_id,
            ).first()
            if not enrollment or enrollment.status != "active":
                return

            current_step = db.query(FunnelStep).filter(
                FunnelStep.id == enrollment.current_step_id,
            ).first()
            if not current_step:
                return

            from app.services.funnel_engine import FunnelEngine
            engine = FunnelEngine(db)
            engine._log(enrollment, current_step, "deferred_send_resolved", {
                "send_log_id": log.id,
                "resolution": reason,
            })
            engine._advance_to_next(enrollment, current_step)
            db.flush()
        except Exception as e:
            logger.error("Error resolving funnel enrollment %s for send %d: %s",
                         log.deferred_source_enrollment_id, log.id, e)

    def _resume_funnel_enrollment(self, db, log) -> None:
        """After successful deferred send, record contact and advance enrollment."""
        try:
            from app.models import FunnelEnrollment, FunnelStep
            enrollment = db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == log.deferred_source_enrollment_id,
            ).first()
            if not enrollment or enrollment.status != "active":
                return

            # Verify enrollment is still on the deferred step — if it already
            # advanced (e.g. due to a prior bug), just log and skip to avoid
            # duplicate sends or step-skipping.
            deferred_step_id = (log.render_context or {}).get("step_id")
            if deferred_step_id and str(enrollment.current_step_id) != str(deferred_step_id):
                logger.warning(
                    "Deferred send %d: enrollment %s already advanced past step %s (now on %s), skipping resume",
                    log.id, enrollment.id, deferred_step_id, enrollment.current_step_id,
                )
                return

            current_step = db.query(FunnelStep).filter(
                FunnelStep.id == enrollment.current_step_id,
            ).first()
            if not current_step:
                return

            from app.services.funnel_engine import FunnelEngine
            engine = FunnelEngine(db)
            engine._log(enrollment, current_step, "deferred_send_resolved", {
                "send_log_id": log.id,
                "resolution": "sent",
            })
            engine._advance_to_next(enrollment, current_step)
            db.flush()
        except Exception as e:
            logger.error("Error resuming funnel enrollment %s for send %d: %s",
                         log.deferred_source_enrollment_id, log.id, e)

    def _baseline_revalidation(self, db, log) -> bool:
        """Check baseline conditions before dispatching a funnel-deferred send.

        Returns True if all baseline checks pass; False to skip the send.
        Checks: contact exists, not opted out, enrollment active, funnel active.
        """
        try:
            from app.models import FunnelEnrollment
            from app.models.messaging import MessagingUser

            # 1. Contact exists
            user = db.query(MessagingUser).filter(
                MessagingUser.id == log.user_id,
            ).first()
            if not user:
                logger.info("Baseline revalidation failed for send %d: contact not found", log.id)
                return False

            # 2. Contact not opted out / unsubscribed
            if getattr(user, 'global_opt_out', False):
                logger.info("Baseline revalidation failed for send %d: global opt-out", log.id)
                return False
            if getattr(user, 'is_subscribed', True) is False:
                logger.info("Baseline revalidation failed for send %d: unsubscribed", log.id)
                return False
            opted_out_channels = getattr(user, 'opted_out_channels', None) or []
            if log.channel in opted_out_channels:
                logger.info("Baseline revalidation failed for send %d: channel opt-out", log.id)
                return False

            # 3. Enrollment still active
            enrollment = db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == log.deferred_source_enrollment_id,
                FunnelEnrollment.status == "active",
            ).first()
            if not enrollment:
                logger.info("Baseline revalidation failed for send %d: enrollment not active", log.id)
                return False

            # 4. Funnel still active
            if enrollment.funnel and enrollment.funnel.status != "active":
                logger.info("Baseline revalidation failed for send %d: funnel not active", log.id)
                return False

            return True
        except Exception as e:
            logger.warning("Baseline revalidation error for send %d (proceeding): %s", log.id, e)
            return True  # On error, allow the send to proceed

    def _evaluate_revalidation(self, db, log) -> bool:
        """Evaluate user-defined revalidation conditions on the deferred send.

        Returns True if conditions still hold; False to skip.
        """
        try:
            from app.services.event_actions.conditions import ConditionEvaluator
            from app.models.messaging import MessagingUser

            conditions = log.render_context.get("revalidation_condition", [])
            if not conditions:
                return True

            user = db.query(MessagingUser).filter(
                MessagingUser.id == log.user_id,
            ).first()
            if not user:
                return False

            user_data = {}
            if hasattr(user, 'properties') and user.properties:
                user_data = dict(user.properties)
            for attr in ('email', 'phone', 'name', 'first_name', 'last_name'):
                val = getattr(user, attr, None)
                if val:
                    user_data[attr] = val

            event_data = {"properties": {}, "event_name": "", "source": "revalidation"}

            # Optionally inject scores
            extra_context = {}
            try:
                from app.services.scoring.scoring_engine import ScoringEngine
                scores = ScoringEngine(db).get_user_scores(log.project_id, log.user_id)
                if scores:
                    extra_context["scores"] = {s.score_definition.name: s.score_value for s in scores if s.score_definition}
            except Exception:
                pass

            evaluator = ConditionEvaluator()
            return evaluator.evaluate_all(conditions, event_data, user_data, "all", extra_context)
        except Exception as e:
            logger.warning("Revalidation evaluation error for send %d (proceeding): %s", log.id, e)
            return True  # On error, allow the send to proceed

    def _record_contact_ledger(self, db, log) -> None:
        """Record the deferred send in the contact ledger."""
        try:
            from app.services.scoring.policy_service import PolicyService
            PolicyService(db).record_contact(
                project_id=log.project_id,
                user_id=log.user_id,
                channel=log.channel,
                source=log.source_type,
                source_id=log.source_id,
            )
        except Exception as e:
            logger.warning("Error recording contact ledger for deferred send %d: %s", log.id, e)


# Singleton instance
scheduled_send_worker = ScheduledSendWorker()
