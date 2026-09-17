"""
Bandit core (Phase 4) — pure, dependency-free functions. No DB, no I/O.

ONE system: a Beta-Bernoulli bandit seeded with the authored ordinal priority
as its prior, so with zero evidence it reproduces the authored ordering by
construction. Each arm earns its way off the prior via an objective,
per-decision-class confidence gate. Scope is EXECUTION ONLY (variant /
channel / send_time / tie_break) — intent ordering is declared, never learned.

Everything here is a re-sweepable function over (trials, successes), which is
what keeps the contested choices (reward definition, gate formula, thresholds)
reversible: re-run over the retained ArmObservation rows to change them.
"""
import math
from dataclasses import dataclass
from typing import List, Optional


# ── Ordinal prior → Beta pseudo-counts (log-odds ladder) ─────────────────

def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def ordinal_prior(
    rank: int,
    *,
    base_rate: float = 0.2,
    step: float = 0.5,
    mass: float = 30.0,
) -> "tuple[float, float]":
    """Beta (alpha, beta) prior for an arm at ordinal `rank` (0 = author's top
    choice). Worse ranks get a lower prior mean via a log-odds ladder, so the
    prior reproduces the authored ordering. `mass` is the pseudo-count weight
    (how sticky the opinion is — higher = more evidence needed to overturn).

    Log-odds ladder (not multiplicative) so the ranking survives low base
    rates — adjacent ranks stay separated even when base_rate is small.
    """
    p = _sigmoid(_logit(base_rate) - rank * step)
    alpha = mass * p
    beta = mass * (1.0 - p)
    return alpha, beta


# ── Posterior + credible interval ────────────────────────────────────────

@dataclass
class ArmPosterior:
    arm_key: str
    rank: int
    trials: int
    successes: int
    alpha: float          # posterior alpha (prior + successes)
    beta: float           # posterior beta  (prior + failures)
    mean: float
    ci_low: float
    ci_high: float

    @property
    def ci_width(self) -> float:
        return self.ci_high - self.ci_low


def posterior(
    rank: int,
    trials: int,
    successes: int,
    *,
    base_rate: float = 0.2,
    step: float = 0.5,
    mass: float = 30.0,
    z: float = 1.96,
    arm_key: str = "",
) -> ArmPosterior:
    """Posterior for one arm given its authored rank + observed evidence.
    Normal-approximation credible interval (cheap; adequate above the
    per-class min-N floor the gate enforces)."""
    successes = max(0, min(successes, trials))
    a0, b0 = ordinal_prior(rank, base_rate=base_rate, step=step, mass=mass)
    a = a0 + successes
    b = b0 + (trials - successes)
    mean = a / (a + b)
    sd = math.sqrt(mean * (1 - mean) / (a + b))
    return ArmPosterior(
        arm_key=arm_key, rank=rank, trials=trials, successes=successes,
        alpha=a, beta=b, mean=mean,
        ci_low=max(0.0, mean - z * sd), ci_high=min(1.0, mean + z * sd),
    )


# ── Confidence gate ──────────────────────────────────────────────────────

@dataclass
class GateThresholds:
    """Per-decision-class gate. Calibrate by SendLog-history simulation before
    enforcing earned_auto (decision-log ruling 13). Two published strengths:
    `strict` (interval non-overlap) and `relaxed` (lower-bound beats incumbent
    mean) — softens over-conservatism."""
    min_trials: int = 30          # real trials floor, excluded from prior mass
    max_ci_width: float = 0.15
    strict_separation: bool = True


@dataclass
class GateResult:
    challenger_arm: Optional[str]
    incumbent_arm: Optional[str]
    earned_auto: bool
    reason: str
    challenger_mean: float = 0.0
    incumbent_mean: float = 0.0
    effect_size: float = 0.0      # challenger_mean - incumbent_mean
    confidence: float = 0.0       # P(challenger > incumbent), normal approx


def _prob_superior(a: ArmPosterior, b: ArmPosterior) -> float:
    """P(arm a's rate > arm b's rate), normal approx on the difference."""
    ma, mb = a.mean, b.mean
    va = ma * (1 - ma) / (a.alpha + a.beta)
    vb = mb * (1 - mb) / (b.alpha + b.beta)
    sd = math.sqrt(va + vb) or 1e-9
    return _sigmoid(1.702 * (ma - mb) / sd)  # logistic approx to Phi


def evaluate_gate(
    arms: List[ArmPosterior],
    thresholds: Optional[GateThresholds] = None,
) -> GateResult:
    """Decide whether a challenger has earned auto over the authored incumbent.

    Incumbent = the authored top choice (rank 0). Challenger = the highest
    posterior-mean arm. earned_auto only if the challenger differs from the
    incumbent, clears the real-trials floor, the interval is tight enough, and
    (strict) its lower bound beats the incumbent's upper bound — else its lower
    bound beats the incumbent mean. Otherwise the authored ordering stands
    (recommend-by-default). `earned_auto` is RENTED — recompute every sweep.
    """
    th = thresholds or GateThresholds()
    if not arms:
        return GateResult(None, None, False, "no_arms")

    incumbent = next((a for a in arms if a.rank == 0), min(arms, key=lambda a: a.rank))
    challenger = max(arms, key=lambda a: a.mean)

    eff = challenger.mean - incumbent.mean
    conf = _prob_superior(challenger, incumbent)
    base = dict(
        challenger_arm=challenger.arm_key, incumbent_arm=incumbent.arm_key,
        challenger_mean=challenger.mean, incumbent_mean=incumbent.mean,
        effect_size=eff, confidence=conf,
    )

    if challenger.arm_key == incumbent.arm_key:
        return GateResult(earned_auto=False, reason="incumbent_leads", **base)
    if challenger.trials < th.min_trials:
        return GateResult(earned_auto=False, reason="below_min_trials", **base)
    if challenger.ci_width > th.max_ci_width:
        return GateResult(earned_auto=False, reason="interval_too_wide", **base)

    separated = (
        challenger.ci_low > incumbent.ci_high if th.strict_separation
        else challenger.ci_low > incumbent.mean
    )
    if not separated:
        return GateResult(earned_auto=False, reason="not_separated", **base)
    return GateResult(earned_auto=True, reason="earned", **base)
