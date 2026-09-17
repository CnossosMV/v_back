"""
Gate-threshold calibration simulator (Phase 4).

The plan mandates calibrating per-decision-class GateThresholds by SendLog-
history simulation BEFORE enabling earned_auto (ruling 13). This harness
reconstructs the channel-arm evidence directly from history (send_logs +
messaging_events — the same outcome rule the RewardSweepWorker uses, but as a
read-only SELECT, so it works whether or not BANDIT_REWARD_SWEEP has run),
then evaluates the gate at a grid of candidate thresholds and reports, for
each, how many slots would graduate to earned_auto.

Use it to pick thresholds that graduate real winners without graduating noise
— then set them in code before flipping the bandit to act.
"""
import logging
from typing import Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.bandit.core import posterior, evaluate_gate, GateThresholds

logger = logging.getLogger(__name__)

ATTRIBUTION_HOURS = 24

# Reconstruct (scope_key, arm_key) trials/successes from history, read-only.
_RECONSTRUCT = text("""
SELECT scope_key, arm_key,
       count(*) AS trials,
       count(*) FILTER (WHERE responded) AS successes
FROM (
  SELECT 'channel:' || sl.slot_id AS scope_key,
         COALESCE(sl.resolved_channel, sl.channel, 'unknown') AS arm_key,
         EXISTS (
           SELECT 1 FROM messaging_events me
           WHERE me.project_id = sl.project_id AND me.user_id = sl.user_id
             AND me.created_at > sl.sent_at
             AND me.created_at <= sl.sent_at + (:hrs || ' hours')::interval
             AND me.source NOT IN ('send_service', 'delivery_tracker', 'system')
         ) AS responded
  FROM send_logs sl
  WHERE sl.project_id = :pid
    AND sl.slot_id IS NOT NULL AND sl.status = 'sent'
    AND sl.user_id IS NOT NULL AND sl.sent_at IS NOT NULL
    AND sl.sent_at < now() - (:hrs || ' hours')::interval
) t
GROUP BY scope_key, arm_key
""")

# Candidate threshold grid swept by the calibration run.
_CONFIGS = [
    {"min_trials": 20, "max_ci_width": 0.20, "strict": True},
    {"min_trials": 30, "max_ci_width": 0.15, "strict": True},
    {"min_trials": 30, "max_ci_width": 0.15, "strict": False},
    {"min_trials": 50, "max_ci_width": 0.10, "strict": True},
]


class BanditSimulator:
    def __init__(self, db: Session):
        self.db = db

    def _reconstruct(self, project_id: int) -> Dict[str, Dict[str, "tuple[int, int]"]]:
        rows = self.db.execute(_RECONSTRUCT, {"pid": project_id, "hrs": ATTRIBUTION_HOURS}).all()
        scopes: Dict[str, Dict[str, "tuple[int, int]"]] = {}
        for r in rows:
            scopes.setdefault(r.scope_key, {})[r.arm_key] = (int(r.trials), int(r.successes))
        return scopes

    def calibrate(self, project_id: int) -> Dict:
        """Sweep the threshold grid over reconstructed history. Returns per-config
        graduation counts + a conservative recommendation."""
        scopes = self._reconstruct(project_id)
        total_slots = len(scopes)
        total_sends = sum(t for arms in scopes.values() for (t, _s) in arms.values())

        results = []
        for cfg in _CONFIGS:
            th = GateThresholds(
                min_trials=cfg["min_trials"], max_ci_width=cfg["max_ci_width"],
                strict_separation=cfg["strict"],
            )
            graduated = 0
            for arms in scopes.values():
                ranked = sorted(arms, key=lambda k: arms[k][0], reverse=True)
                posts = [
                    posterior(i, arms[a][0], arms[a][1], arm_key=a)
                    for i, a in enumerate(ranked)
                ]
                if evaluate_gate(posts, th).earned_auto:
                    graduated += 1
            results.append({**cfg, "graduated_slots": graduated})

        # Conservative recommendation: the strictest config that still graduates
        # at least one slot (real winners exist); else strictest overall.
        with_grads = [r for r in results if r["graduated_slots"] > 0]
        recommended = (
            min(with_grads, key=lambda r: r["graduated_slots"])
            if with_grads else results[-1]
        )
        return {
            "decision_class": "channel",
            "slots_with_evidence": total_slots,
            "total_attributed_sends": total_sends,
            "configs": results,
            "recommended": {
                "min_trials": recommended["min_trials"],
                "max_ci_width": recommended["max_ci_width"],
                "strict_separation": recommended["strict"],
            },
            "note": (
                "Reconstructed from history (read-only). Pick a config that "
                "graduates real winners without noise, then set GateThresholds "
                "in code before enabling earned_auto."
            ),
        }
