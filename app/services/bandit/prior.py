"""
PriorProvider seam (Phase 4) — the cross-tenant pooling hook, STUBBED in v1.

v1 uses per-tenant evidence only. The moat is per-tenant tuned posteriors;
cross-tenant learning is DESIGNED here but not built: when a tenant base
exists, a population provider pools content-free sufficient statistics
(trials/successes per arm fingerprint) into a hierarchical prior. Population
priors only seed the prior — they NEVER open a gate (that stays per-tenant
evidence). Privacy: aggregates/effect-sizes only, never raw events.
"""
from typing import Optional


class PriorProvider:
    """Returns extra pseudo-count mass for an arm fingerprint, or None.

    The default per-tenant provider returns None — no pooling. A future
    population provider implements `population_pseudocounts` over aggregated
    fingerprints. `arm_fingerprint` is intentionally content-free (e.g.
    decision_class + a hash of normalized option features) so nothing
    tenant-identifying crosses the seam.
    """

    def population_pseudocounts(
        self, arm_fingerprint: str,
    ) -> Optional["tuple[float, float]"]:
        return None


# v1 singleton — per-tenant only.
prior_provider = PriorProvider()
