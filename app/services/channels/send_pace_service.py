"""
Outbound send-pace governor.

Receivers (Gmail, Outlook, …) judge sender reputation per **sending domain** and
penalize sudden volume spikes with temporary deferrals (SMTP 4.x). This governor
spreads outbound sends into a smooth per-domain rate so no tenant can burst-
penalize a shared or dedicated sending domain.

Design:
- **Leaky bucket per sending domain.** Each domain has a `next_slot_at` cursor.
  A send asks `reserve_slot`; the cursor advances by `60s / max_per_minute`.
  If the reserved slot is ~now → SEND_NOW; if it's in the future → DELAY_UNTIL
  (the caller parks a `status='delayed'` SendLog with that `scheduled_at`, which
  the existing scheduled_send_worker drains).
- **Atomic across processes.** The reserve runs in its own short-lived session
  under `pg_advisory_xact_lock(hashtext('sendpace:'||domain))`, committed
  immediately so the lock is never held across provider I/O. Postgres is the only
  cross-process primitive available (Redis here is pub/sub-only).
- **Policy precedence:** SendingDomain row → ProjectSendConfig → global env
  default. Unknown domains auto-create a SendingDomain row (warm-up on) so new
  tenants need zero setup.
- **Warm-up ramp** (opt-out per domain) caps the daily volume for young domains.
- **Reputation** status (active/throttled/paused) is written by
  sending_reputation_worker and consulted here (throttled → reduced rate;
  paused → HOLD).

Rollout mirrors the existing guardian/candidate/selection modes:
`SEND_PACE_MODE = off | shadow | enforce` (default off).
"""
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional, List

from sqlalchemy import text

from app.database import SessionLocal
from app.models import SendingDomain, SendingRateState, ProjectSendConfig, CustomerSMTPConfig

logger = logging.getLogger(__name__)

# Default warm-up ladder: index = whole days since warmup_started_at, value =
# max sends allowed that day. Past the end of the list → no warm-up cap.
DEFAULT_WARMUP_SCHEDULE: List[int] = [50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000]

# Grace window: a reserved slot within this many seconds of "now" is treated as
# sendable inline rather than delayed (avoids delaying steady-state traffic).
GRACE_SECONDS = 1.0


def pace_mode(db=None, project_id=None) -> str:
    """'off' | 'shadow' | 'enforce' from SEND_PACE_MODE. Default 'off'.

    off     — governor not consulted (current behaviour).
    shadow  — compute the decision and record it in the trace, but deliver as
              today (nothing delayed). Use to validate on real traffic.
    enforce — actually delay/hold over-budget sends.
    """
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "pace")


def _global_default_per_minute() -> int:
    try:
        return max(1, int(os.getenv("SEND_PACE_DEFAULT_PER_MINUTE", "60")))
    except ValueError:
        return 60


def _global_default_per_day() -> Optional[int]:
    raw = os.getenv("SEND_PACE_DEFAULT_PER_DAY", "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


@dataclass
class PacePolicy:
    """Resolved effective policy for a domain (for observability / decisions)."""
    domain: str
    max_per_minute: int
    max_per_day: Optional[int]        # effective cap incl. warm-up; None = unlimited
    status: str                       # active | throttled | paused
    warmup_active: bool = False
    warmup_day: Optional[int] = None


@dataclass
class ReserveResult:
    decision: str                     # 'send_now' | 'delay' | 'hold'
    scheduled_at: Optional[datetime] = None
    reason: str = ""
    policy: Optional[PacePolicy] = None
    trace: dict = field(default_factory=dict)


class SendPaceService:
    def __init__(self, db, clock=None):
        # `db` is used only for read-only resolution helpers. The atomic reserve
        # uses its own short-lived session so it can commit independently.
        self.db = db
        from app.services.clock import SystemClock
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # Sending-domain resolution
    # ------------------------------------------------------------------
    def resolve_sending_domain(
        self,
        channel: str,
        project_id: int,
        instance_config: Optional[dict],
    ) -> Optional[str]:
        """Resolve the sending domain for this send. Email only in v1 (WhatsApp/
        SMS have their own provider quality systems). Returns a lowercased domain
        or None (None → governor bypassed)."""
        if channel != "email":
            return None
        from_email = None
        cfg = instance_config or {}
        if cfg.get("from_email"):
            from_email = cfg["from_email"]
        elif cfg.get("instance_id"):
            try:
                from app.models import EmailInstance
                inst = self.db.query(EmailInstance).filter(
                    EmailInstance.id == cfg["instance_id"],
                ).first()
                if inst and inst.from_email:
                    from_email = inst.from_email
            except Exception:
                pass
        elif cfg.get("smtp_config_id"):
            smtp = self.db.query(CustomerSMTPConfig).filter(
                CustomerSMTPConfig.id == cfg["smtp_config_id"],
                CustomerSMTPConfig.project_id == project_id,
                CustomerSMTPConfig.is_active == True,  # noqa: E712
            ).first()
            if smtp and smtp.from_email:
                from_email = smtp.from_email
        if not from_email:
            smtp = self.db.query(CustomerSMTPConfig).filter(
                CustomerSMTPConfig.project_id == project_id,
                CustomerSMTPConfig.is_active == True,  # noqa: E712
            ).first()
            if smtp and smtp.from_email:
                from_email = smtp.from_email
        if not from_email or "@" not in from_email:
            return None
        return from_email.rsplit("@", 1)[1].strip().lower()

    # ------------------------------------------------------------------
    # Policy resolution (read or create the domain row)
    # ------------------------------------------------------------------
    def _get_or_create_domain(self, session, domain: str, project_id: Optional[int]) -> SendingDomain:
        row = session.query(SendingDomain).filter(SendingDomain.domain == domain).first()
        if row:
            return row
        row = SendingDomain(
            domain=domain,
            project_id=project_id,
            status="active",
            warmup_enabled=True,
            warmup_started_at=self.clock.utcnow().date(),
        )
        session.add(row)
        session.flush()
        return row

    def _effective_per_minute(self, row: SendingDomain, project_cfg: Optional[ProjectSendConfig]) -> int:
        per_min = (
            row.max_per_minute
            or (project_cfg.max_per_minute if project_cfg else None)
            or _global_default_per_minute()
        )
        if row.status == "throttled":
            per_min = max(1, int(round(per_min * (row.throttle_factor or 0.3))))
        return max(1, per_min)

    def _warmup_cap(self, row: SendingDomain, today: date):
        """Return (cap|None, warmup_active, warmup_day). cap None = no warm-up limit."""
        if not row.warmup_enabled or not row.warmup_started_at:
            return None, False, None
        days = (today - row.warmup_started_at).days
        if days < 0:
            days = 0
        schedule = row.warmup_schedule or DEFAULT_WARMUP_SCHEDULE
        if days >= len(schedule):
            return None, False, days  # ramp complete
        return int(schedule[days]), True, days

    def _effective_per_day(self, row: SendingDomain, project_cfg: Optional[ProjectSendConfig], today: date):
        configured = row.max_per_day or (project_cfg.max_per_day if project_cfg else None) or _global_default_per_day()
        cap, warmup_active, warmup_day = self._warmup_cap(row, today)
        if cap is not None:
            configured = min(configured, cap) if configured else cap
        return configured, warmup_active, warmup_day

    def resolve_policy(self, domain: str, project_id: Optional[int]) -> PacePolicy:
        """Read-only effective policy (uses self.db; auto-creates if missing)."""
        today = self.clock.utcnow().date()
        row = self._get_or_create_domain(self.db, domain, project_id)
        project_cfg = None
        if project_id:
            project_cfg = self.db.query(ProjectSendConfig).filter(
                ProjectSendConfig.project_id == project_id,
            ).first()
        per_min = self._effective_per_minute(row, project_cfg)
        per_day, warmup_active, warmup_day = self._effective_per_day(row, project_cfg, today)
        return PacePolicy(
            domain=domain, max_per_minute=per_min, max_per_day=per_day,
            status=row.status, warmup_active=warmup_active, warmup_day=warmup_day,
        )

    # ------------------------------------------------------------------
    # Slot reservation (the atomic core)
    # ------------------------------------------------------------------
    def reserve_slot(self, domain: str, project_id: Optional[int], priority: int = 0,
                     apply: bool = True) -> ReserveResult:
        """Reserve the next send slot for `domain`. Atomic across processes via a
        Postgres advisory xact lock held only for the brief state update.

        apply=False (shadow mode): compute the decision WITHOUT advancing the
        bucket cursor or daily counter — nothing is persisted, so shadow runs
        don't corrupt the state that enforce mode will rely on."""
        now = self.clock.utcnow()
        today = now.date()
        session = SessionLocal()
        try:
            bind = session.get_bind()
            if bind is not None and bind.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                    {"k": f"sendpace:{domain}"},
                )

            row = self._get_or_create_domain(session, domain, project_id)
            project_cfg = None
            if project_id:
                project_cfg = session.query(ProjectSendConfig).filter(
                    ProjectSendConfig.project_id == project_id,
                ).first()

            per_min = self._effective_per_minute(row, project_cfg)
            per_day, warmup_active, warmup_day = self._effective_per_day(row, project_cfg, today)
            policy = PacePolicy(
                domain=domain, max_per_minute=per_min, max_per_day=per_day,
                status=row.status, warmup_active=warmup_active, warmup_day=warmup_day,
            )

            if row.status == "paused":
                session.commit()
                # Recheck in ~15 min rather than dropping.
                return ReserveResult(
                    decision="hold", scheduled_at=now + timedelta(minutes=15),
                    reason="domain_paused", policy=policy,
                    trace={"per_min": per_min, "status": row.status},
                )

            state = session.query(SendingRateState).filter(
                SendingRateState.scope_key == domain,
            ).with_for_update().first()
            if not state:
                state = SendingRateState(scope_key=domain, day=today, day_count=0)
                session.add(state)
                session.flush()

            # Roll the daily counter on date change.
            if state.day != today:
                state.day = today
                state.day_count = 0

            # Daily cap (incl. warm-up).
            if per_day is not None and state.day_count >= per_day:
                # Hold until start of next UTC day.
                next_day = datetime.combine(today + timedelta(days=1), datetime.min.time())
                session.rollback()
                return ReserveResult(
                    decision="hold", scheduled_at=next_day,
                    reason="daily_cap", policy=policy,
                    trace={"per_day": per_day, "day_count": state.day_count},
                )

            interval = timedelta(seconds=60.0 / per_min)
            cursor = state.next_slot_at or now
            candidate = max(now, cursor)
            if apply:
                state.next_slot_at = candidate + interval
                state.day_count = (state.day_count or 0) + 1
                session.commit()
            else:
                session.rollback()

            if candidate <= now + timedelta(seconds=GRACE_SECONDS):
                return ReserveResult(
                    decision="send_now", reason="within_pace", policy=policy,
                    trace={"per_min": per_min, "candidate": candidate.isoformat()},
                )
            return ReserveResult(
                decision="delay", scheduled_at=candidate, reason="paced", policy=policy,
                trace={"per_min": per_min, "candidate": candidate.isoformat()},
            )
        except Exception as exc:
            logger.warning("Pace reserve failed for domain %s: %s", domain, exc)
            try:
                session.rollback()
            except Exception:
                pass
            # Fail open — never block a send because the governor errored.
            return ReserveResult(decision="send_now", reason="governor_error")
        finally:
            session.close()
