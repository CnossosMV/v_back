"""
WhatsApp retry policy — classifies Meta Cloud API error codes as
retryable (transient) vs. permanent, and supplies the backoff schedule.

Reference: https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
"""
from datetime import timedelta
from typing import Optional

# ── Transient codes — retry with backoff ────────────────────────────────
#
# These mean the message could plausibly succeed on a later attempt,
# without any change on the sender side (no new template, no opt-in,
# no different number). Source: Meta Graph API error codes for WA Cloud.
RETRYABLE_META_CODES: frozenset[str] = frozenset({
    "131053",  # Media download error / timeout
    "131056",  # (Re-)Pair rate limit — back off and try again
    "131057",  # Account in maintenance mode
    "133015",  # Cannot send to deactivated account (very rare, transient on Meta side)
    "131047",  # Re-engagement window (24h expired) — see note below
})

# ── Permanent codes — do NOT retry ──────────────────────────────────────
#
# Listed for documentation; the scheduler ignores anything not in
# RETRYABLE_META_CODES, so this is informational. We keep the set
# explicit so future contributors know we considered them.
PERMANENT_META_CODES: frozenset[str] = frozenset({
    # 131026 ("Message undeliverable") and 130472 ("number is part of an
    # experiment") were previously retried as "transient". Prod evidence
    # (WABA 1386514339885607 disabled 2026-07) showed they are NOT: 131026
    # to a number that is not on WhatsApp / has not accepted ToS fails on
    # every retry, and 130472 is Meta throttling a MARKETING template — in
    # both cases the retry just re-hammers Meta with a known-bad send and
    # drags the quality rating down. Treat both as terminal: do not retry.
    "131026",  # Message undeliverable — recipient not on WhatsApp / ToS / device
    "130472",  # User's number is part of an experiment — Meta marketing throttle
    "131000",  # Generic recoverable error from Meta — actually rare, treated permanent
    "131005",  # Access denied
    "131008",  # Required parameter missing
    "131009",  # Parameter value invalid
    "131016",  # Service temporarily unavailable (but Meta says don't retry tight)
    "131021",  # Recipient cannot be sender
    "131031",  # Account has been locked
    "131045",  # Unsigned certificate
    "131051",  # Unsupported message type
    "131052",  # Media download failed (client side)
    "132000",  # Template parameter count mismatch
    "132001",  # Template does not exist
    "132005",  # Template hydrated text too long
    "132007",  # Template format character policy violated
    "132012",  # Template parameter format mismatch
    "132015",  # Template paused
    "132016",  # Template disabled
    "133000",  # Incomplete deregistration
    "133004",  # Server temporarily unavailable
    "133005",  # Two-step pin mismatch
    "133006",  # Phone number re-verification needed
})

# ── 131047 special note ─────────────────────────────────────────────────
#
# 131047 ("Re-engagement message") means the 24-h window expired and the
# message was free-form (not a template). Retrying the *same* free-form
# message is futile, but our retry path goes through WhatsAppSender which
# uses template fallback when the window is closed. So we DO retry — the
# retry will switch to template send if a template_fallback was attached.
# If there's no template_fallback, the retry will fail again with the same
# code and naturally exhaust the budget.

# ── Backoff schedule ────────────────────────────────────────────────────
#
# Attempt 0 = original send (already failed when this scheduler is called).
# Attempt 1 = first retry, after RETRY_BACKOFF[0].
# Attempt 2 = second retry, after RETRY_BACKOFF[1].
# Attempt 3 = third retry, after RETRY_BACKOFF[2].
# Anything beyond MAX_RETRY_ATTEMPTS is permanently failed.
RETRY_BACKOFF: tuple[timedelta, ...] = (
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=4),
)

MAX_RETRY_ATTEMPTS: int = len(RETRY_BACKOFF)  # = 3

# ── Retry TTL ───────────────────────────────────────────────────────────
#
# A retry that has not been delivered within this window past its
# scheduled time is stale and must not fire — the WhatsApp re-engagement
# context (24-h window, recipient state) will have moved on. The worker's
# expiry gate (SendLog.expires_at) enforces this. Without it, a retry row
# has no TTL and a multi-day-old retry could fire after the contact's
# situation changed.
RETRY_TTL: timedelta = timedelta(hours=24)


def retry_expires_at(scheduled_at, root_expires_at=None):
    """Expiry for a retry row: scheduled_at + RETRY_TTL, tightened to the
    root send's own expiry when it had one (a retry must never outlive the
    original intent's deadline)."""
    ttl_expiry = scheduled_at + RETRY_TTL
    if root_expires_at is not None:
        return min(ttl_expiry, root_expires_at)
    return ttl_expiry


def is_retryable(error_code: Optional[str]) -> bool:
    """Return True if a Meta Cloud API error code is transient."""
    if not error_code:
        return False
    return str(error_code) in RETRYABLE_META_CODES


def next_backoff(attempt_index: int) -> Optional[timedelta]:
    """Backoff delay before the (attempt_index)th retry. None when exhausted.

    attempt_index is 1-indexed: 1 = first retry, 2 = second, etc.
    """
    if attempt_index < 1 or attempt_index > MAX_RETRY_ATTEMPTS:
        return None
    return RETRY_BACKOFF[attempt_index - 1]
