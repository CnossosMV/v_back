"""
Recipient validation — block synthetic/placeholder addresses from being sent to.

Some tenants represent anonymous/unidentified contacts with a synthetic
placeholder email (e.g. Tabloide's ``guest_<uuid>@guest.tabloide.pro``). These
are NOT real mailboxes: sending to them hard-bounces (no MX / DNS) and hurts the
sending domain's reputation.

The placeholder convention is **per tenant**, so it is NOT hardcoded here — the
pattern comes from ``ProjectSendConfig.placeholder_email_pattern`` (a regex). A
project with no pattern configured has no placeholder concept and nothing is
blocked on that basis. Structural validity (has a local part and a domain) is
the only universal, agnostic check.

This module is the single source of truth used at every send entry point
(funnel resolution, the Send Layer, and the low-level SMTP sender).
"""
import re
import logging
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def _compile(pattern: str):
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        logger.warning("Invalid placeholder_email_pattern ignored: %r", pattern)
        return None


def is_structurally_valid(addr) -> bool:
    """Universal, tenant-agnostic sanity: non-empty with a local part + domain."""
    if not addr or not isinstance(addr, str):
        return False
    a = addr.strip()
    return "@" in a and not a.startswith("@") and not a.endswith("@")


def is_placeholder_email(addr, pattern: Optional[str]) -> bool:
    """True if `addr` matches the tenant's placeholder pattern. If the tenant has
    no pattern configured, nothing is a placeholder."""
    if not pattern or not addr or not isinstance(addr, str):
        return False
    rx = _compile(pattern)
    return bool(rx.search(addr.strip())) if rx else False


def is_sendable_email(addr, pattern: Optional[str] = None) -> bool:
    """True only for a structurally-valid, non-placeholder address."""
    if not is_structurally_valid(addr):
        return False
    return not is_placeholder_email(addr, pattern)


def first_sendable_email(*candidates, pattern: Optional[str] = None):
    """Return the first candidate that is a real, sendable email, else None."""
    for c in candidates:
        if is_sendable_email(c, pattern):
            return c.strip()
    return None


def project_placeholder_pattern(db, project_id: Optional[int]) -> Optional[str]:
    """Load the project's configured placeholder-email regex (or None)."""
    if not project_id:
        return None
    try:
        from app.models import ProjectSendConfig
        row = (
            db.query(ProjectSendConfig.placeholder_email_pattern)
            .filter(ProjectSendConfig.project_id == project_id)
            .first()
        )
        return row[0] if row and row[0] else None
    except Exception:
        return None
