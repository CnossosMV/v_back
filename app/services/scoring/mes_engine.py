"""
Message Effectiveness Scoring (MES) Engine.

Computes reach_score + reengagement_score for each SendLog entry.
Reach measures confidence the message was seen (channel-specific).
Re-engagement measures whether the message brought the user back (event attribution).

v3 (ALGORITHM_VERSION=3):
- Only events with server-verified frontend provenance are eligible.
- Inbound replies (SendLog.replied_at) are the top of the reach ladder.
- Negative delivery feedback (hard bounce / complaint / blocked) zeroes reach;
  an opt-out inside the attribution window floors the combined score.
- Re-engagement is normalized to a true 0-100 scale.
- Events hard-linked through attribution.send_log_id get full credit, including
  anonymous browser events. Events linked to a different send are excluded;
  unlinked identified events share credit by time-decay proximity.
- The binary pre-session ×0.3 discount is replaced by a graded uplift factor
  against the user's trailing 7-day activity baseline (plus a milder ×0.5
  pre-session discount).
- Scores carry a `settled` flag: final once the attribution window closes.
"""
import math
import re
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func as sqlfunc, text

from app.models import (
    SendLog, MessageEffectivenessScore, MESConfig, DeliveryFeedback,
)
from app.services.messaging.event_sources import FRONTEND as EVENT_SOURCE_FRONTEND

logger = logging.getLogger(__name__)

ALGORITHM_VERSION = 3

# Raw re-engagement caps: weighted events (45) + diversity (15) + sustained (15).
# Used to normalize the raw score to a true 0-100 scale.
REENG_RAW_MAX = 75.0

# Trailing window used to estimate the user's baseline activity rate.
BASELINE_DAYS = 7

# Delivery feedback types that mean the message never reached (or actively
# hurt) the recipient — reach is zeroed.
NEGATIVE_FEEDBACK_TYPES = ("hard_bounce", "complaint", "unreachable", "blocked")

# Opt-out events inside the attribution window floor the combined score.
OPT_OUT_EVENT_NAMES = (
    "contact.unsubscribed",
    "contact.global_opted_out",
    "contact.channel_opted_out",
    "contact.complaint",
)

# Combined score ceiling applied when the message triggered an opt-out.
OPT_OUT_SCORE_FLOOR = 5.0

# SendLog statuses that will never progress further.
TERMINAL_FAIL_STATUSES = ("failed", "exhausted", "blocked", "skipped", "superseded")

# Default event weights for re-engagement.
# Keys are prefix-matched against event_name. "editor_" matches "editor_export_clicked".
# Events not matching any key get DEFAULT_UNMATCHED_WEIGHT.
DEFAULT_EVENT_WEIGHTS: Dict[str, float] = {
    "editor_": 15.0,      # User interacted with the product editor (high value)
    "page_view": 10.0,    # User visited a page
    "auth_login": 8.0,    # User logged in (came back)
    "gtm_": 12.0,         # GTM conversion events
    "trial_": 10.0,       # Trial-related actions
}

DEFAULT_UNMATCHED_WEIGHT: float = 5.0  # Any frontend event not matching a pattern

# Exclusion patterns — events matching these are NOT counted for re-engagement.
# Projects may still configure semantic exclusions explicitly.
DEFAULT_EXCLUDE_PATTERNS: List[str] = []

DEFAULT_GRADE_THRESHOLDS = {"high": 70, "medium": 40, "low": 0}


class MESEngine:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_score(self, send_log_id: int) -> Optional[MessageEffectivenessScore]:
        """Full pipeline: load SendLog -> compute reach -> compute re-engagement -> combine -> upsert."""
        send_log = self.db.query(SendLog).filter(SendLog.id == send_log_id).first()
        if not send_log:
            return None

        # External touches (operator-logged out-of-band interactions) carry no
        # tracking — scoring them would read as never-opened messages.
        if send_log.source_type == "external_touch":
            return None

        config = self._get_config(send_log.project_id)

        # Compute sub-scores
        reach, reach_signals = self.compute_reach_score(send_log)
        reengagement, reeng_signals = self.compute_reengagement_score(send_log, config)

        # Combined
        rw = config.reach_weight if config else 0.5
        ew = config.reengagement_weight if config else 0.5
        combined = rw * reach + ew * reengagement
        combined = min(100.0, max(0.0, combined))

        # Negative outcome floor: the message triggered an opt-out/complaint
        if reeng_signals.get("opt_out_in_window") or reach_signals.get("negative_feedback"):
            combined = min(combined, OPT_OUT_SCORE_FLOOR)

        # Grade
        thresholds = (config.grade_thresholds if config and config.grade_thresholds
                      else DEFAULT_GRADE_THRESHOLDS)
        if send_log.status in TERMINAL_FAIL_STATUSES:
            grade = "failed"
        else:
            grade = self._determine_grade(combined, thresholds)

        attribution_window = config.attribution_window_hours if config else 24

        now = datetime.utcnow()

        # Settle lifecycle: score is final once the attribution window closed
        # (or the send terminally failed — nothing more can change).
        if send_log.status in TERMINAL_FAIL_STATUSES:
            settled = True
        elif send_log.sent_at:
            settled = now >= send_log.sent_at + timedelta(hours=attribution_window)
        else:
            settled = False

        # Resolve funnel_step_id if source is funnel (validate FK exists)
        funnel_id = None
        funnel_step_id = None
        if send_log.source_type == "funnel" and send_log.source_id:
            from app.models import Funnel
            funnel_exists = self.db.query(Funnel.id).filter(
                Funnel.id == send_log.source_id,
            ).first()
            if funnel_exists:
                funnel_id = send_log.source_id
                funnel_step_id = self._resolve_funnel_step(send_log)

        # Upsert
        existing = self.db.query(MessageEffectivenessScore).filter(
            MessageEffectivenessScore.send_log_id == send_log_id,
        ).first()

        if existing:
            existing.reach_score = round(reach, 2)
            existing.reach_signals = reach_signals
            existing.reengagement_score = round(reengagement, 2)
            existing.reengagement_signals = reeng_signals
            existing.combined_score = round(combined, 2)
            existing.score_grade = grade
            existing.algorithm_version = ALGORITHM_VERSION
            existing.attribution_window_h = attribution_window
            existing.computed_at = now
            existing.stale = False
            existing.settled = settled
            existing.funnel_id = funnel_id
            existing.funnel_step_id = funnel_step_id
            return existing

        mes = MessageEffectivenessScore(
            project_id=send_log.project_id,
            send_log_id=send_log.id,
            user_id=send_log.user_id,
            channel=send_log.channel or send_log.resolved_channel or "unknown",
            template_id=send_log.template_id,
            source_type=send_log.source_type or "unknown",
            source_id=send_log.source_id,
            funnel_id=funnel_id,
            funnel_step_id=funnel_step_id,
            reach_score=round(reach, 2),
            reach_signals=reach_signals,
            reengagement_score=round(reengagement, 2),
            reengagement_signals=reeng_signals,
            combined_score=round(combined, 2),
            score_grade=grade,
            algorithm_version=ALGORITHM_VERSION,
            attribution_window_h=attribution_window,
            computed_at=now,
            stale=False,
            settled=settled,
        )
        self.db.add(mes)
        return mes

    def batch_compute(self, project_id: int, limit: int = 500) -> int:
        """Process up to `limit` stale scores for a project. Returns count processed."""
        # 1. Create MES rows for SendLogs that don't have one yet
        self._create_missing_records(project_id, limit)

        # 2. Scores produced by older algorithms must be recomputed even when
        #    already settled.
        self._mark_outdated_versions(project_id, limit)

        # 3. Settle pass: computed-but-unsettled rows whose window has since
        #    closed get one final recompute (marked stale here, picked up below).
        self._mark_unsettled_due(project_id, limit)

        # 4. Process stale records, draining old algorithm versions first.
        stale_records = (
            self.db.query(MessageEffectivenessScore)
            .filter(
                MessageEffectivenessScore.project_id == project_id,
                MessageEffectivenessScore.stale == True,
            )
            .order_by(
                (MessageEffectivenessScore.algorithm_version != ALGORITHM_VERSION).desc(),
                MessageEffectivenessScore.created_at.desc(),
            )
            .limit(limit)
            .all()
        )

        count = 0
        for mes in stale_records:
            try:
                self.compute_score(mes.send_log_id)
                count += 1
            except Exception as e:
                logger.error(f"Error computing MES for send_log {mes.send_log_id}: {e}")

        if count > 0:
            self.db.commit()

        return count

    def mark_stale(self, send_log_id: int) -> None:
        """Mark an existing MES record as stale (needs recomputation)."""
        self.db.query(MessageEffectivenessScore).filter(
            MessageEffectivenessScore.send_log_id == send_log_id,
        ).update({"stale": True}, synchronize_session=False)

    def mark_stale_for_event(self, event: Any) -> None:
        """Invalidate scores affected by an identified or directly attributed event."""
        if event.user_id:
            self.mark_stale_for_user(event.project_id, event.user_id)

        attribution = event.attribution if isinstance(event.attribution, dict) else {}
        attributed_send_log_id = attribution.get("send_log_id")
        if attributed_send_log_id is None:
            return

        try:
            attributed_send_log_id = int(attributed_send_log_id)
        except (TypeError, ValueError):
            return

        self.db.query(MessageEffectivenessScore).filter(
            MessageEffectivenessScore.project_id == event.project_id,
            MessageEffectivenessScore.send_log_id == attributed_send_log_id,
        ).update({"stale": True}, synchronize_session=False)

    def mark_stale_for_user(self, project_id: int, user_id: int) -> None:
        """Mark recent MES records for a user as stale (new events may affect re-engagement).

        The lookback tracks the project's attribution window (+2h of slack for
        clock skew / late event delivery) so a widened window keeps updating.
        """
        if not user_id:
            return
        config = self._get_config(project_id)
        window_h = config.attribution_window_hours if config else 24
        cutoff = datetime.utcnow() - timedelta(hours=window_h + 2)
        self.db.query(MessageEffectivenessScore).filter(
            MessageEffectivenessScore.project_id == project_id,
            MessageEffectivenessScore.user_id == user_id,
            MessageEffectivenessScore.created_at >= cutoff,
            MessageEffectivenessScore.settled == False,
        ).update({"stale": True}, synchronize_session=False)

    # ------------------------------------------------------------------
    # Reach Score
    # ------------------------------------------------------------------

    def compute_reach_score(self, send_log: SendLog) -> Tuple[float, Dict[str, Any]]:
        """
        Channel-specific reach confidence (0-100).

        Score reflects the BEST delivery signal available:
        - Inbound reply = 95+ (user responded — definitive on any channel)
        - WhatsApp read = 90 (blue ticks, definitive)
        - Email click = 95 (user interacted)
        - Email human open = 75, proxy open = 55
        - SMS delivered = 35 (no read receipt)
        Hard bounce / complaint / blocked feedback zeroes the score.
        """
        status = send_log.status or "queued"
        channel = (send_log.channel or send_log.resolved_channel or "").lower()
        click_count = send_log.click_count or 0
        open_count = send_log.open_count or 0
        reply_count = send_log.reply_count or 0
        has_click = click_count > 0
        has_reply = bool(send_log.replied_at) or reply_count > 0
        is_proxy_open = False

        # Detect proxy opens for email
        if channel == "email" and send_log.opened_at and not has_click:
            is_proxy_open = self._check_proxy_open(send_log)

        signals = {
            "status": status,
            "channel": channel,
            "has_click": has_click,
            "has_reply": has_reply,
            "reply_count": reply_count,
            "open_count": open_count,
            "is_proxy_open": is_proxy_open,
        }

        # Negative delivery feedback (bounce/complaint/blocked) → reach 0
        negative = self._get_negative_feedback(send_log.id)
        if negative:
            signals["negative_feedback"] = negative
            signals["computed_score"] = 0.0
            return (0.0, signals)

        # Base score by delivery status
        base_scores = {
            "failed": 0,
            "exhausted": 0,
            "blocked": 0,
            "skipped": 0,
            "superseded": 0,
            "queued": 5,
            "delayed": 5,
            "deferred": 5,
            "sent": 15,
            "delivered": 40,
            "read": 70,
        }
        score = base_scores.get(status, 5)

        # Channel-specific overrides
        if channel == "whatsapp":
            if status == "delivered":
                score = 50
            elif status == "read":
                score = 90
        elif channel == "email":
            if status == "delivered":
                score = 30
            elif status == "read":
                if has_click:
                    score = 95
                elif is_proxy_open:
                    score = 55
                else:
                    score = 75
        elif channel == "sms":
            if status == "delivered":
                score = 35
        else:
            # webhook, inapp, push, web, etc.
            if status == "delivered":
                score = 45
            elif status == "read":
                score = 80

        # Email bonus: click overrides even if status isn't "read"
        if channel == "email" and has_click:
            score = max(score, 95)

        # Multiple non-proxy opens bonus
        if channel == "email" and open_count > 1 and not is_proxy_open:
            score = min(100, score + 5)

        # Reply overrides everything (user demonstrably received AND engaged).
        # Not applied to terminally-failed sends (score stays 0).
        if has_reply and status not in TERMINAL_FAIL_STATUSES:
            score = max(score, 95)
            if reply_count > 1:
                score = min(100, score + 3)

        score = min(100.0, max(0.0, float(score)))
        signals["computed_score"] = score
        return (score, signals)

    # ------------------------------------------------------------------
    # Re-engagement Score
    # ------------------------------------------------------------------

    def compute_reengagement_score(
        self, send_log: SendLog, config: Optional[MESConfig]
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Attribution-based re-engagement scoring (0-100).

        Measures whether the message brought the user back, using weighted
        frontend events with time decay. Events hard-linked to this send
        (via tracked-link UTM token) get full credit; unlinked events share
        credit across overlapping sends by time-decay proximity; the result
        is adjusted against the user's trailing activity baseline.
        """
        signals: Dict[str, Any] = {
            "events_found": 0,
            "unique_event_names": 0,
            "first_event_delay_minutes": None,
            "pre_session_detected": False,
            "pre_session_event_count": 0,
            "attribution_conflict_count": 0,
            "hard_linked_events": 0,
            "excluded_other_send_events": 0,
            "pre_session_discount": 1.0,
            "baseline_events_7d": 0,
            "expected_baseline_events": 0.0,
            "uplift_factor": 1.0,
            "sustained_engagement": False,
            "weighted_event_score": 0,
            "speed_multiplier": 0,
            "diversity_score": 0,
            "sustained_score": 0,
            "raw_score": 0,
            "normalized_score": 0,
            "opt_out_in_window": False,
            "eligible_event_sources": [EVENT_SOURCE_FRONTEND],
        }

        # A timestamp is mandatory. Identified events use the recipient user_id;
        # anonymous events are eligible only when directly attributed to this send.
        if not send_log.sent_at:
            signals["skip_reason"] = "no_sent_at"
            return (0.0, signals)

        # Config values
        attribution_window_h = config.attribution_window_hours if config else 24
        pre_session_min = config.pre_session_window_min if config else 15
        event_weights = (config.reengagement_event_weights if config and config.reengagement_event_weights
                         else DEFAULT_EVENT_WEIGHTS)
        exclude_patterns = (config.exclude_event_patterns if config and config.exclude_event_patterns
                            else DEFAULT_EXCLUDE_PATTERNS)
        speed_decay_rate = config.speed_decay_rate if config else 0.08
        event_decay_rate = config.event_decay_rate if config else 0.1

        sent_at = send_log.sent_at
        window_end = sent_at + timedelta(hours=attribution_window_h)
        now = datetime.utcnow()

        # 0. Opt-out inside the window is a hard negative outcome (the combined
        #    score is floored in compute_score).
        if send_log.user_id:
            signals["opt_out_in_window"] = self._check_opt_out_in_window(
                send_log.project_id, send_log.user_id, sent_at, min(window_end, now),
            )

        # 1. Pre-session detection (user already active just before the send)
        pre_session_start = sent_at - timedelta(minutes=pre_session_min)
        pre_engagement = 0
        if send_log.user_id:
            pre_engagement = self._count_engagement_events(
                send_log.project_id, send_log.user_id,
                pre_session_start, sent_at,
                event_weights, exclude_patterns,
            )
        if pre_engagement > 0:
            signals["pre_session_detected"] = True
            signals["pre_session_event_count"] = pre_engagement
            # Milder than v1's 0.3: the baseline uplift factor below already
            # discounts habitual activity.
            signals["pre_session_discount"] = 0.5

        # 2. Post-message engagement events (with attribution payload)
        post_events = self._get_engagement_events(
            send_log.project_id, send_log.user_id, send_log.id,
            sent_at, min(window_end, now),
            event_weights, exclude_patterns,
        )

        if not post_events:
            window_closed = now >= window_end
            signals["window_status"] = "closed" if window_closed else "open"
            signals["skip_reason"] = "no_post_events"
            return (0.0, signals)

        # 2b. Partition by hard attribution link (utm_content tracking token
        #     resolved at ingestion into attribution.send_log_id).
        own_events: List[Tuple[str, datetime, bool]] = []  # (name, at, hard_linked)
        excluded_other = 0
        for evt_name, evt_created_at, evt_attribution in post_events:
            linked_id = None
            if isinstance(evt_attribution, dict):
                linked_id = evt_attribution.get("send_log_id")
            if linked_id is not None:
                try:
                    linked_id = int(linked_id)
                except (TypeError, ValueError):
                    linked_id = None
            if linked_id == send_log.id:
                own_events.append((evt_name, evt_created_at, True))
            elif linked_id is not None:
                excluded_other += 1  # deterministically belongs to another send
            else:
                own_events.append((evt_name, evt_created_at, False))

        signals["excluded_other_send_events"] = excluded_other
        signals["hard_linked_events"] = sum(1 for _, _, hard in own_events if hard)

        if not own_events:
            signals["window_status"] = "measured"
            signals["skip_reason"] = "all_events_linked_to_other_sends"
            return (0.0, signals)

        signals["events_found"] = len(own_events)
        signals["window_status"] = "measured"

        # 2c. Overlapping sends for credit sharing of unlinked events
        other_sends = []
        if send_log.user_id:
            other_sends = self._get_overlapping_sends(
                send_log.project_id, send_log.user_id, send_log.id,
                sent_at, attribution_window_h,
            )
        signals["attribution_conflict_count"] = len(other_sends)

        # 3. Weighted event signal: per-event time decay × credit share
        weighted_sum = 0.0
        event_names = set()
        hour_buckets = set()
        first_event_at = None

        for evt_name, evt_created_at, hard_linked in own_events:
            event_names.add(evt_name)
            hours_since = max(0, (evt_created_at - sent_at).total_seconds() / 3600)
            hour_buckets.add(int(hours_since))

            if first_event_at is None or evt_created_at < first_event_at:
                first_event_at = evt_created_at

            weight = self._get_event_weight(evt_name, event_weights)
            per_event_decay = math.exp(-event_decay_rate * hours_since)

            if hard_linked or not other_sends:
                share = 1.0
            else:
                share = self._credit_share(
                    sent_at, other_sends, evt_created_at, event_decay_rate,
                )
            weighted_sum += weight * per_event_decay * share

        weighted_event_score = min(45.0, weighted_sum)
        signals["weighted_event_score"] = round(weighted_event_score, 2)

        # 4. Return speed multiplier
        first_delay_hours = (first_event_at - sent_at).total_seconds() / 3600
        signals["first_event_delay_minutes"] = round(first_delay_hours * 60, 1)
        speed_multiplier = math.exp(-speed_decay_rate * first_delay_hours)
        signals["speed_multiplier"] = round(speed_multiplier, 4)

        # 5. Diversity bonus
        unique_count = len(event_names)
        signals["unique_event_names"] = unique_count
        diversity_score = min(15.0, 5.0 * unique_count)
        signals["diversity_score"] = round(diversity_score, 2)

        # 6. Sustained engagement bonus
        sustained = len(hour_buckets) > 2
        signals["sustained_engagement"] = sustained
        sustained_score = 15.0 if sustained else 0.0
        signals["sustained_score"] = sustained_score

        # 7. Raw score, normalized to a true 0-100 scale
        raw = (weighted_event_score + diversity_score + sustained_score) * speed_multiplier
        signals["raw_score"] = round(raw, 2)
        normalized = min(100.0, raw / REENG_RAW_MAX * 100.0)
        signals["normalized_score"] = round(normalized, 2)

        # 8. Baseline uplift adjustment: activity the user would likely have
        #    shown anyway (trailing 7d rate scaled to the window) earns no credit.
        baseline_count = 0
        if send_log.user_id:
            baseline_count = self._count_engagement_events(
                send_log.project_id, send_log.user_id,
                sent_at - timedelta(days=BASELINE_DAYS), sent_at,
                event_weights, exclude_patterns,
            )
        signals["baseline_events_7d"] = baseline_count
        expected = baseline_count * (attribution_window_h / (BASELINE_DAYS * 24.0))
        signals["expected_baseline_events"] = round(expected, 2)
        n_events = len(own_events)
        uplift_factor = n_events / (n_events + expected) if expected > 0 else 1.0
        signals["uplift_factor"] = round(uplift_factor, 4)

        # 9. Apply adjustments
        final = normalized * uplift_factor * signals["pre_session_discount"]
        final = min(100.0, max(0.0, final))

        return (final, signals)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_config(self, project_id: int) -> Optional[MESConfig]:
        """Load MESConfig for project, return None if not configured (use defaults)."""
        return self.db.query(MESConfig).filter(
            MESConfig.project_id == project_id,
        ).first()

    def _get_negative_feedback(self, send_log_id: int) -> Optional[str]:
        """Return the worst negative feedback type recorded for this send, if any."""
        row = (
            self.db.query(DeliveryFeedback.feedback_type)
            .filter(
                DeliveryFeedback.send_log_id == send_log_id,
                DeliveryFeedback.feedback_type.in_(NEGATIVE_FEEDBACK_TYPES),
            )
            .first()
        )
        return row[0] if row else None

    def _check_opt_out_in_window(
        self, project_id: int, user_id: int, start: datetime, end: datetime,
    ) -> bool:
        """True if the user opted out / complained inside the attribution window."""
        from app.models.messaging import MessagingEvent

        row = (
            self.db.query(MessagingEvent.id)
            .filter(
                MessagingEvent.project_id == project_id,
                MessagingEvent.user_id == user_id,
                MessagingEvent.event_name.in_(OPT_OUT_EVENT_NAMES),
                MessagingEvent.created_at >= start,
                MessagingEvent.created_at <= end,
            )
            .first()
        )
        return row is not None

    def _check_proxy_open(self, send_log: SendLog) -> bool:
        """Check if email open was via proxy (Gmail, Apple Mail, Yahoo)."""
        from app.models.messaging import MessagingEvent

        proxy_event = (
            self.db.query(MessagingEvent.id)
            .filter(
                MessagingEvent.project_id == send_log.project_id,
                MessagingEvent.event_name == "channel.email.opened",
                MessagingEvent.properties["send_log_id"].astext == str(send_log.id),
                MessagingEvent.properties["is_proxy"].astext == "true",
            )
            .first()
        )
        return proxy_event is not None

    def _count_engagement_events(
        self,
        project_id: int,
        user_id: int,
        start: datetime,
        end: datetime,
        event_weights: Dict[str, float],
        exclude_patterns: List[str],
    ) -> int:
        """Count engagement events (all except excluded patterns) in a time window."""
        from app.models.messaging import MessagingEvent

        rows = (
            self.db.query(MessagingEvent.event_name)
            .filter(
                MessagingEvent.project_id == project_id,
                MessagingEvent.user_id == user_id,
                MessagingEvent.source == EVENT_SOURCE_FRONTEND,
                MessagingEvent.created_at >= start,
                MessagingEvent.created_at < end,
            )
            .limit(2000)
            .all()
        )
        compiled = [re.compile(p) for p in exclude_patterns]
        return sum(1 for (name,) in rows if not any(pat.search(name) for pat in compiled))

    def _get_engagement_events(
        self,
        project_id: int,
        user_id: Optional[int],
        send_log_id: int,
        start: datetime,
        end: datetime,
        event_weights: Dict[str, float],
        exclude_patterns: List[str],
    ) -> List[Tuple[str, datetime, Optional[Dict[str, Any]]]]:
        """Get engagement events (all except excluded patterns) in a time window.

        Returns (event_name, created_at, attribution) tuples — attribution is
        used to detect hard links to a specific send (send_log_id).
        """
        from app.models.messaging import MessagingEvent

        direct_attribution = (
            MessagingEvent.attribution["send_log_id"].astext == str(send_log_id)
        )
        recipient_or_direct = direct_attribution
        if user_id:
            recipient_or_direct = or_(MessagingEvent.user_id == user_id, direct_attribution)

        rows = (
            self.db.query(
                MessagingEvent.event_name,
                MessagingEvent.created_at,
                MessagingEvent.attribution,
            )
            .filter(
                MessagingEvent.project_id == project_id,
                MessagingEvent.source == EVENT_SOURCE_FRONTEND,
                recipient_or_direct,
                MessagingEvent.created_at >= start,
                MessagingEvent.created_at <= end,
            )
            .order_by(MessagingEvent.created_at.asc())
            .limit(500)
            .all()
        )

        # Exclude system events via patterns
        compiled = [re.compile(p) for p in exclude_patterns]
        return [(name, created_at, attribution) for name, created_at, attribution in rows
                if not any(pat.search(name) for pat in compiled)]

    def _get_event_weight(self, event_name: str, weights: Dict[str, float]) -> float:
        """Get weight for an event name. Exact match first, then prefix match."""
        if event_name in weights:
            return weights[event_name]
        # Prefix match (e.g., "editor_" matches "editor_export_clicked")
        for key, weight in weights.items():
            if event_name.startswith(key):
                return weight
        return DEFAULT_UNMATCHED_WEIGHT

    def _get_overlapping_sends(
        self,
        project_id: int,
        user_id: int,
        send_log_id: int,
        sent_at: datetime,
        window_h: int,
    ) -> List[datetime]:
        """sent_at of other non-failed sends whose windows can cover this send's events."""
        window_start = sent_at - timedelta(hours=window_h)
        window_end = sent_at + timedelta(hours=window_h)
        rows = (
            self.db.query(SendLog.sent_at)
            .filter(
                SendLog.project_id == project_id,
                SendLog.user_id == user_id,
                SendLog.id != send_log_id,
                SendLog.sent_at.isnot(None),
                SendLog.sent_at >= window_start,
                SendLog.sent_at <= window_end,
                SendLog.status.notin_(list(TERMINAL_FAIL_STATUSES)),
            )
            .limit(50)
            .all()
        )
        return [r[0] for r in rows]

    @staticmethod
    def _credit_share(
        own_sent_at: datetime,
        other_sends: List[datetime],
        event_at: datetime,
        decay_rate: float,
    ) -> float:
        """Fraction of credit this send gets for an unlinked event.

        Time-decay proximity: each send that had already happened when the
        event fired competes; closer sends win more credit.
        """
        own_hours = max(0.0, (event_at - own_sent_at).total_seconds() / 3600)
        own_decay = math.exp(-decay_rate * own_hours)
        denom = own_decay
        for other_sent in other_sends:
            if other_sent <= event_at:
                hours = (event_at - other_sent).total_seconds() / 3600
                denom += math.exp(-decay_rate * hours)
        return own_decay / denom if denom > 0 else 1.0

    def _count_attribution_conflicts(
        self,
        project_id: int,
        user_id: int,
        send_log_id: int,
        sent_at: datetime,
    ) -> int:
        """Count other SendLogs for the same user within +-2 hours (attribution conflict).

        Kept for backwards compatibility (v1 signals); v2 uses
        _get_overlapping_sends + per-event credit sharing instead.
        """
        window_start = sent_at - timedelta(hours=2)
        window_end = sent_at + timedelta(hours=2)
        count = (
            self.db.query(sqlfunc.count(SendLog.id))
            .filter(
                SendLog.project_id == project_id,
                SendLog.user_id == user_id,
                SendLog.id != send_log_id,
                SendLog.sent_at >= window_start,
                SendLog.sent_at <= window_end,
                SendLog.status.notin_(["failed", "skipped", "exhausted", "blocked"]),
            )
            .scalar()
        ) or 0
        return count

    def _resolve_funnel_step(self, send_log: SendLog) -> Optional[int]:
        """Resolve funnel_step_id from enrollment logs for a funnel-originated SendLog."""
        from app.models import FunnelEnrollmentLog

        if not send_log.deferred_source_enrollment_id:
            return None

        # Look for the enrollment log that references this send
        log_entry = (
            self.db.query(FunnelEnrollmentLog.step_id)
            .filter(
                FunnelEnrollmentLog.enrollment_id == send_log.deferred_source_enrollment_id,
                FunnelEnrollmentLog.action.in_(["action_executed", "send_message_sent"]),
            )
            .order_by(FunnelEnrollmentLog.created_at.desc())
            .first()
        )
        return log_entry[0] if log_entry else None

    def _mark_outdated_versions(self, project_id: int, limit: int) -> int:
        """Mark old algorithm rows stale, including scores already settled."""
        outdated_ids = (
            self.db.query(MessageEffectivenessScore.id)
            .filter(
                MessageEffectivenessScore.project_id == project_id,
                MessageEffectivenessScore.algorithm_version != ALGORITHM_VERSION,
                MessageEffectivenessScore.stale == False,
            )
            .order_by(MessageEffectivenessScore.created_at.desc())
            .limit(limit)
            .all()
        )
        if not outdated_ids:
            return 0

        ids = [row[0] for row in outdated_ids]
        self.db.query(MessageEffectivenessScore).filter(
            MessageEffectivenessScore.id.in_(ids),
        ).update({"stale": True}, synchronize_session=False)
        return len(ids)

    def _mark_unsettled_due(self, project_id: int, limit: int) -> int:
        """Mark computed-but-unsettled rows whose window has closed for one
        final recompute (which sets settled=True). Safety net for any missed
        stale marks — guarantees every score eventually finalizes."""
        due_ids = (
            self.db.query(MessageEffectivenessScore.id)
            .join(SendLog, SendLog.id == MessageEffectivenessScore.send_log_id)
            .filter(
                MessageEffectivenessScore.project_id == project_id,
                MessageEffectivenessScore.settled == False,
                MessageEffectivenessScore.stale == False,
                MessageEffectivenessScore.computed_at.isnot(None),
                or_(
                    SendLog.status.in_(list(TERMINAL_FAIL_STATUSES)),
                    and_(
                        SendLog.sent_at.isnot(None),
                        SendLog.sent_at
                        <= sqlfunc.timezone("utc", sqlfunc.now())
                        - sqlfunc.make_interval(
                            0, 0, 0, 0,
                            MessageEffectivenessScore.attribution_window_h,
                        ),
                    ),
                ),
            )
            .limit(limit)
            .all()
        )
        if not due_ids:
            return 0
        ids = [r[0] for r in due_ids]
        self.db.query(MessageEffectivenessScore).filter(
            MessageEffectivenessScore.id.in_(ids),
        ).update({"stale": True}, synchronize_session=False)
        return len(ids)

    def _create_missing_records(self, project_id: int, limit: int) -> int:
        """Create MES rows for SendLogs that don't have one yet.

        Only picks up messages from the last 7 days. Older messages get
        scored on-the-fly when viewed via the batch endpoint.
        """
        from sqlalchemy.orm import aliased

        mes_alias = aliased(MessageEffectivenessScore)
        cutoff = datetime.utcnow() - timedelta(days=7)

        missing = (
            self.db.query(SendLog)
            .outerjoin(mes_alias, mes_alias.send_log_id == SendLog.id)
            .filter(
                SendLog.project_id == project_id,
                SendLog.status.notin_(["queued"]),
                SendLog.source_type != "external_touch",
                SendLog.queued_at >= cutoff,
                mes_alias.id.is_(None),
            )
            .order_by(SendLog.queued_at.desc())
            .limit(limit)
            .all()
        )

        count = 0
        for sl in missing:
            try:
                mes = MessageEffectivenessScore(
                    project_id=sl.project_id,
                    send_log_id=sl.id,
                    user_id=sl.user_id,
                    channel=sl.channel or sl.resolved_channel or "unknown",
                    template_id=sl.template_id,
                    source_type=sl.source_type or "unknown",
                    source_id=sl.source_id,
                    stale=True,
                )
                self.db.add(mes)
                count += 1
            except Exception as e:
                logger.error(f"Error creating MES record for send_log {sl.id}: {e}")

        if count > 0:
            self.db.flush()

        return count

    @staticmethod
    def _determine_grade(score: float, thresholds: Dict[str, Any]) -> str:
        high = thresholds.get("high", 70)
        medium = thresholds.get("medium", 40)
        if score >= high:
            return "high"
        elif score >= medium:
            return "medium"
        return "low"
