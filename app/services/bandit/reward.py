"""
RewardService (Phase 4) — writes ArmObservation evidence and reads it back as
posteriors for the gate. Thin DB layer over the pure core; reward DEFINITION
stays a re-sweepable function over retained rows.

Reward in v1 is clean Bernoulli (success = the configured positive signal
within the attribution window). Complaints/opt-outs are handled as GOVERNOR
suspension, NOT negative reward, so the reward function stays un-gameable and
cross-tenant comparable for the pooling seam.
"""
import logging
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import ArmObservation
from app.services.bandit.core import (
    ArmPosterior, GateResult, GateThresholds, posterior, evaluate_gate,
)

logger = logging.getLogger(__name__)

VALID_CLASSES = ("variant", "channel", "send_time", "tie_break")
DEFAULT_WINDOW_DAYS = 90  # drift: only the rolling window counts


def scope_key(decision_class: str, slot_id: str) -> str:
    """Canonical scope key — the authored slot's identity for a decision class."""
    return f"{decision_class}:{slot_id}"


class RewardService:
    def __init__(self, db: Session):
        self.db = db

    def record_observation(
        self,
        project_id: int,
        decision_class: str,
        slot_id: str,
        arm_key: str,
        success: bool,
        *,
        send_log_id: Optional[int] = None,
        arm_version: int = 1,
        reward: Optional[float] = None,
    ) -> bool:
        """Append one observation (idempotent per send_log_id+class). Returns
        True if written, False if a duplicate/no-op. Unknown class is rejected
        — intent/episode decisions have no arm identity by design."""
        if decision_class not in VALID_CLASSES:
            logger.warning("Rejected arm observation for non-execution class %r", decision_class)
            return False
        obs = ArmObservation(
            project_id=project_id,
            decision_class=decision_class,
            scope_key=scope_key(decision_class, slot_id),
            arm_key=arm_key,
            arm_version=arm_version,
            send_log_id=send_log_id,
            success=bool(success),
            reward=reward,
        )
        self.db.add(obs)
        try:
            self.db.flush()
            return True
        except Exception:
            # Append-once unique (send_log_id, decision_class) violated → no-op.
            self.db.rollback()
            return False

    def _counts(self, project_id: int, scope: str, window_days: int) -> Dict[str, "tuple[int, int]"]:
        rows = self.db.execute(text("""
            SELECT arm_key,
                   count(*) AS trials,
                   count(*) FILTER (WHERE success) AS successes
            FROM arm_observations
            WHERE project_id = :p AND scope_key = :s
              AND observed_at >= now() - (:w || ' days')::interval
            GROUP BY arm_key
        """), {"p": project_id, "s": scope, "w": window_days}).all()
        return {r.arm_key: (int(r.trials), int(r.successes)) for r in rows}

    def arm_posteriors(
        self,
        project_id: int,
        decision_class: str,
        slot_id: str,
        ranked_arms: List[str],
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> List[ArmPosterior]:
        """Posteriors for the arms of one authored slot. `ranked_arms` is the
        author's ordering (index 0 = top choice) and seeds each arm's prior."""
        scope = scope_key(decision_class, slot_id)
        counts = self._counts(project_id, scope, window_days)
        out: List[ArmPosterior] = []
        for rank, arm in enumerate(ranked_arms):
            trials, successes = counts.get(arm, (0, 0))
            out.append(posterior(rank, trials, successes, arm_key=arm))
        return out

    def observed_arms(self, project_id: int, decision_class: str, slot_id: str,
                      window_days: int = DEFAULT_WINDOW_DAYS) -> List[str]:
        """Arms that have evidence for a slot, most-tried first (the de-facto
        incumbent leads). Used when the caller doesn't supply an authored order."""
        scope = scope_key(decision_class, slot_id)
        counts = self._counts(project_id, scope, window_days)
        return sorted(counts, key=lambda k: counts[k][0], reverse=True)

    def recommendation(
        self,
        project_id: int,
        decision_class: str,
        slot_id: str,
        ranked_arms: Optional[List[str]] = None,
        *,
        thresholds: Optional[GateThresholds] = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> Dict:
        """UI-facing recommendation for a slot: per-arm posteriors + the gate
        verdict (incumbent vs challenger, earned_auto, effect size, confidence).
        Read-only. ranked_arms defaults to observed-arms-by-trials."""
        arms = ranked_arms or self.observed_arms(project_id, decision_class, slot_id, window_days)
        posts = self.arm_posteriors(project_id, decision_class, slot_id, arms, window_days=window_days)
        gate = evaluate_gate(posts, thresholds)
        return {
            "decision_class": decision_class,
            "slot_id": slot_id,
            "arms": [
                {
                    "arm_key": p.arm_key, "rank": p.rank,
                    "trials": p.trials, "successes": p.successes,
                    "mean": round(p.mean, 4),
                    "ci_low": round(p.ci_low, 4), "ci_high": round(p.ci_high, 4),
                }
                for p in posts
            ],
            "gate": {
                "earned_auto": gate.earned_auto,
                "reason": gate.reason,
                "incumbent_arm": gate.incumbent_arm,
                "challenger_arm": gate.challenger_arm,
                "effect_size": round(gate.effect_size, 4),
                "confidence": round(gate.confidence, 4),
            },
        }

    def evaluate(
        self,
        project_id: int,
        decision_class: str,
        slot_id: str,
        ranked_arms: List[str],
        *,
        thresholds: Optional[GateThresholds] = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> GateResult:
        """Gate result for one slot: has a challenger earned auto, or does the
        authored ordering stand? Recommend-by-default — caller decides whether
        to auto-apply (execution only) or just surface the recommendation."""
        arms = self.arm_posteriors(
            project_id, decision_class, slot_id, ranked_arms, window_days=window_days,
        )
        return evaluate_gate(arms, thresholds)
