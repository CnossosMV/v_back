"""
Guardian (Phase 4) — the single component that decides whether an attention
slot may open, wrapping the selection winner before dispatch.

It is the named home for the three thorniest config questions (attention
budget per channel + global, cooldown/quiet-hours, expiry) and it makes the
four-lane policy (decision-log ruling 12) one explicit thing instead of
scattered checks:

  transactional  -> always send (bypasses marketing budget; opt-out still in send layer)
  conversational -> send; never held; counts attention cost (may replenish budget)
  manual         -> send; WARN on cap overage, never block (human override)
  promotional    -> fully gated: send / delay / drop via the pacing primitives

v1 is a FACADE over the existing PolicyService pacing (caps/cooldowns/quiet)
plus a contact attention-budget snapshot for the UI. It is not yet wired into
the send path — that cutover is a separate flag-gated step. The verbs are
send / delay / drop / change_channel (change_channel + promote-next are send-
layer concerns, surfaced here as recommendations).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ContactLedger
from app.services.scoring.policy_service import PolicyService

logger = logging.getLogger(__name__)

# Lanes whose marketing budget is bypassed (still counted for the snapshot).
_BUDGET_EXEMPT = ("transactional", "conversational")


@dataclass
class GuardianVerdict:
    verb: str                 # send | delay | drop
    lane: str
    reason: str
    defer_until: Optional[datetime] = None
    warnings: List[str] = field(default_factory=list)


class GuardianService:
    def __init__(self, db: Session, clock=None):
        self.db = db
        from app.services.clock import SystemClock
        self.clock = clock or SystemClock()

    # ── Attention budget snapshot (per contact) ──────────────────────────

    def _periods(self):
        now = self.clock.utcnow()
        return {
            "daily": now.replace(hour=0, minute=0, second=0, microsecond=0),
            "weekly": (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0),
            "monthly": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        }

    def _used(self, project_id: int, user_id: int, since: datetime, channel: Optional[str]) -> int:
        q = self.db.query(func.count(ContactLedger.id)).filter(
            ContactLedger.project_id == project_id,
            ContactLedger.user_id == user_id,
            ContactLedger.sent_at >= since,
        )
        if channel:
            q = q.filter(ContactLedger.channel == channel)
        return q.scalar() or 0

    def budget_snapshot(self, project_id: int, user_id: int) -> Dict:
        """Per-period attention budget for a contact: cap / used / remaining,
        total and per-channel. Powers the Decision view's attention view."""
        policy = PolicyService(self.db).get_project_policy(project_id)
        caps = (policy.contact_caps if policy else None) or {}
        out: Dict[str, Dict] = {}
        for pname, since in self._periods().items():
            pcaps = caps.get(pname) or {}
            total_cap = pcaps.get("total")
            used_total = self._used(project_id, user_id, since, None)
            channels = {}
            for ch, cap in pcaps.items():
                if ch == "total":
                    continue
                used = self._used(project_id, user_id, since, ch)
                channels[ch] = {
                    "cap": cap, "used": used,
                    "remaining": max(0, cap - used) if cap is not None else None,
                }
            out[pname] = {
                "total": {
                    "cap": total_cap, "used": used_total,
                    "remaining": max(0, total_cap - used_total) if total_cap is not None else None,
                },
                "channels": channels,
            }
        return out

    # ── Verdict (lane-aware gate) ────────────────────────────────────────

    def assess(
        self,
        project_id: int,
        user_id: Optional[int],
        channel: str,
        lane: str,
        source: str = "manual",
        source_id: Optional[int] = None,
        ignored_policies: Optional[set[str]] = None,
    ) -> GuardianVerdict:
        """Decide whether this contact's slot may open for a send of `lane`."""
        lane = (lane or "promotional").lower()

        # No user → can't evaluate per-contact budget; let the send layer's
        # own gates (opt-out/quiet/rate) handle it.
        if user_id is None or lane == "transactional":
            return GuardianVerdict(verb="send", lane=lane, reason="exempt_or_no_user")

        if lane == "conversational":
            return GuardianVerdict(verb="send", lane=lane, reason="conversational_short_circuit")

        decision = PolicyService(self.db, clock=self.clock).check_can_contact(
            project_id=project_id, user_id=user_id, channel=channel,
            source=source, source_id=source_id,
            ignored_policies=ignored_policies,
        )

        if lane == "manual":
            # Warn, never block (human override, ruling 12).
            if not decision.allowed:
                return GuardianVerdict(
                    verb="send", lane=lane, reason="manual_override",
                    warnings=[f"over budget: {decision.reason}"],
                )
            return GuardianVerdict(verb="send", lane=lane, reason="within_budget")

        # promotional (and anything else) — fully gated.
        if decision.allowed:
            return GuardianVerdict(verb="send", lane=lane, reason="within_budget")
        if decision.deferrable and decision.defer_until:
            return GuardianVerdict(
                verb="delay", lane=lane, reason=decision.reason or "deferrable",
                defer_until=decision.defer_until,
            )
        return GuardianVerdict(verb="drop", lane=lane, reason=decision.reason or "blocked")
