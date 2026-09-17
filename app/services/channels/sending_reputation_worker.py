"""
Sending Reputation Worker.

Periodically computes, per sending domain, the 24h bounce and complaint rates
from DeliveryFeedback (attributed to a domain at record time) against the sends
in the same window, and adjusts each SendingDomain's `status`:

    active     — healthy; full pace.
    throttled  — bounce rate over the throttle threshold; SendPaceService reduces
                 the per-minute rate by `throttle_factor`.
    paused     — bounce/complaint rate over the pause threshold; new sends HOLD.

Transitions use a recovery margin (hysteresis) so a domain hovering near a
threshold does not flap. A minimum sample of sends is required before any
downgrade so a couple of bounces on a low-volume domain can't pause it.
"""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 739005  # unique ID for the sending-reputation worker
MIN_SAMPLE = 20            # min sends in the window before a downgrade
RECOVERY_MARGIN = 0.8      # must fall below threshold*margin to recover a level


class SendingReputationWorker:
    def __init__(self, poll_interval: int = 300, window_hours: int = 24):
        self.poll_interval = poll_interval
        self.window_hours = window_hours
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            return
        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Sending reputation worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Sending reputation worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    self._process_cycle(db)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Error in sending reputation worker: %s", e)
            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db) -> None:
        acquired = db.execute(
            text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")
        ).scalar()
        if not acquired:
            return
        try:
            from app.models import SendLog, DeliveryFeedback, SendingDomain

            cutoff = datetime.utcnow() - timedelta(hours=self.window_hours)

            # Sends per domain in window (the denominator).
            sent_rows = (
                db.query(SendLog.sending_domain, func.count(SendLog.id))
                .filter(
                    SendLog.sending_domain.isnot(None),
                    SendLog.status.in_(["sent", "delivered", "read"]),
                    SendLog.sent_at >= cutoff,
                )
                .group_by(SendLog.sending_domain)
                .all()
            )
            sent_by_domain = {d: c for d, c in sent_rows}

            # Bounces / complaints per domain in window.
            fb_rows = (
                db.query(
                    DeliveryFeedback.sending_domain,
                    DeliveryFeedback.feedback_type,
                    func.count(DeliveryFeedback.id),
                )
                .filter(
                    DeliveryFeedback.sending_domain.isnot(None),
                    DeliveryFeedback.created_at >= cutoff,
                )
                .group_by(DeliveryFeedback.sending_domain, DeliveryFeedback.feedback_type)
                .all()
            )
            bounce_by_domain: dict = {}
            complaint_by_domain: dict = {}
            for dom, ftype, cnt in fb_rows:
                if ftype in ("hard_bounce", "soft_bounce", "unreachable", "blocked"):
                    bounce_by_domain[dom] = bounce_by_domain.get(dom, 0) + cnt
                elif ftype == "complaint":
                    complaint_by_domain[dom] = complaint_by_domain.get(dom, 0) + cnt

            # Evaluate every domain that has any activity or an existing row.
            domains = set(sent_by_domain) | set(bounce_by_domain) | set(complaint_by_domain)
            rows = db.query(SendingDomain).filter(SendingDomain.domain.in_(domains)).all() if domains else []
            row_by_domain = {r.domain: r for r in rows}

            now = datetime.utcnow()
            changed = 0
            for domain in domains:
                row = row_by_domain.get(domain)
                if row is None:
                    continue  # only manage domains that already have a policy row
                sent = sent_by_domain.get(domain, 0)
                bounces = bounce_by_domain.get(domain, 0)
                complaints = complaint_by_domain.get(domain, 0)
                denom = max(sent, bounces + complaints)  # avoid divide-by-zero; be conservative
                if denom == 0:
                    continue
                bounce_rate = 100.0 * bounces / denom
                complaint_rate = 100.0 * complaints / denom

                row.bounce_rate_24h = round(bounce_rate, 3)
                row.complaint_rate_24h = round(complaint_rate, 3)
                row.reputation_updated_at = now

                new_status = self._decide_status(row, bounce_rate, complaint_rate, sent)
                if new_status != row.status:
                    logger.warning(
                        "[reputation] domain=%s %s -> %s (sent=%d bounce=%.2f%% complaint=%.2f%%)",
                        domain, row.status, new_status, sent, bounce_rate, complaint_rate,
                    )
                    old = row.status
                    row.status = new_status
                    self._emit_status_event(db, row, old, new_status, bounce_rate, complaint_rate)
                    changed += 1
            db.commit()
            if changed:
                logger.info("[reputation] updated %d domain statuses", changed)
        except Exception as e:
            logger.error("Sending reputation cycle failed: %s", e)
            db.rollback()
        finally:
            db.execute(text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})"))

    def _decide_status(self, row, bounce_rate: float, complaint_rate: float, sent: int) -> str:
        pause_b = row.auto_pause_bounce_pct or 5.0
        pause_c = row.auto_pause_complaint_pct or 0.3
        throttle_b = row.auto_throttle_bounce_pct or 2.0
        current = row.status or "active"

        # Escalation is immediate; de-escalation requires enough sample and a
        # recovery margin (hysteresis) so a domain near a threshold won't flap.
        over_pause = bounce_rate >= pause_b or complaint_rate >= pause_c
        over_throttle = bounce_rate >= throttle_b

        if over_pause:
            return "paused"
        if over_throttle:
            # Don't relax a paused domain straight to active on one good window.
            if current == "paused" and sent < MIN_SAMPLE:
                return "paused"
            return "throttled"

        # Healthy window. Require sample + margin before recovering a level.
        if current in ("paused", "throttled"):
            if sent < MIN_SAMPLE:
                return current
            if bounce_rate < throttle_b * RECOVERY_MARGIN and complaint_rate < pause_c * RECOVERY_MARGIN:
                return "active"
            return current
        return "active"

    def _emit_status_event(self, db, row, old_status: str, new_status: str,
                           bounce_rate: float, complaint_rate: float) -> None:
        """Emit a project-scoped MessagingEvent on a reputation transition."""
        if not row.project_id:
            return
        try:
            from app.models.messaging import MessagingEvent
            db.add(MessagingEvent(
                project_id=row.project_id,
                user_id=None,
                event_name="sending_domain.status_changed",
                properties={
                    "domain": row.domain,
                    "old_status": old_status,
                    "new_status": new_status,
                    "bounce_rate_24h": round(bounce_rate, 3),
                    "complaint_rate_24h": round(complaint_rate, 3),
                },
                source="system",
                processed=False,
            ))
        except Exception:
            logger.debug("Failed to emit reputation event", exc_info=True)


# Module singleton
sending_reputation_worker = SendingReputationWorker()
