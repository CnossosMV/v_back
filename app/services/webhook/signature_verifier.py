"""
Webhook Signature Verification Service

Verifies webhook signatures for built-in providers (Stripe, Calendly, Typeform)
and custom sources using HMAC-SHA256.
"""
import hashlib
import hmac
import time
import base64
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Tolerance for Stripe timestamp validation (5 minutes)
STRIPE_TIMESTAMP_TOLERANCE = 300


def verify(
    source_type: str,
    source_slug: str,
    headers: Dict[str, str],
    raw_body: bytes,
    secret: str,
) -> bool:
    """
    Verify webhook signature based on source type.

    Args:
        source_type: 'built_in' or 'custom'
        source_slug: Slug name (e.g. 'stripe', 'calendly', 'typeform' for built_in)
        headers: HTTP request headers (lowercased keys)
        raw_body: Raw request body bytes
        secret: Decrypted signing secret

    Returns:
        True if signature is valid, False otherwise
    """
    if not secret:
        logger.warning("No secret configured for source %s — skipping verification", source_slug)
        return True

    # Normalize header keys to lowercase
    lower_headers = {k.lower(): v for k, v in headers.items()}

    if source_type == "built_in":
        slug_lower = source_slug.lower()
        if slug_lower == "stripe":
            return _verify_stripe(lower_headers, raw_body, secret)
        elif slug_lower == "calendly":
            return _verify_calendly(lower_headers, raw_body, secret)
        elif slug_lower == "typeform":
            return _verify_typeform(lower_headers, raw_body, secret)
        else:
            logger.warning("Unknown built_in source slug: %s — falling back to custom verification", source_slug)
            return _verify_custom(lower_headers, raw_body, secret)
    else:
        return _verify_custom(lower_headers, raw_body, secret)


def _verify_stripe(headers: Dict[str, str], raw_body: bytes, secret: str) -> bool:
    """
    Stripe signature verification.
    Header: Stripe-Signature: t=<timestamp>,v1=<signature>
    Payload: "{timestamp}.{body}"
    """
    sig_header = headers.get("stripe-signature", "")
    if not sig_header:
        return False

    try:
        # Parse t= and v1= from header
        elements = {}
        for item in sig_header.split(","):
            key, _, value = item.strip().partition("=")
            elements[key] = value

        timestamp = elements.get("t", "")
        expected_sig = elements.get("v1", "")
        if not timestamp or not expected_sig:
            return False

        # Check timestamp freshness
        ts_int = int(timestamp)
        if abs(time.time() - ts_int) > STRIPE_TIMESTAMP_TOLERANCE:
            logger.warning("Stripe webhook timestamp too old: %s", timestamp)
            return False

        # Compute expected signature
        signed_payload = f"{timestamp}.".encode() + raw_body
        computed = hmac.new(
            secret.encode(), signed_payload, hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(computed, expected_sig)
    except Exception as e:
        logger.error("Stripe signature verification error: %s", e)
        return False


def _verify_calendly(headers: Dict[str, str], raw_body: bytes, secret: str) -> bool:
    """
    Calendly signature verification.
    Header: Calendly-Webhook-Signature: t=<timestamp>,v1=<signature>
    Payload: "{timestamp}.{body}"
    """
    sig_header = headers.get("calendly-webhook-signature", "")
    if not sig_header:
        return False

    try:
        elements = {}
        for item in sig_header.split(","):
            key, _, value = item.strip().partition("=")
            elements[key] = value

        timestamp = elements.get("t", "")
        expected_sig = elements.get("v1", "")
        if not timestamp or not expected_sig:
            return False

        signed_payload = f"{timestamp}.".encode() + raw_body
        computed = hmac.new(
            secret.encode(), signed_payload, hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(computed, expected_sig)
    except Exception as e:
        logger.error("Calendly signature verification error: %s", e)
        return False


def _verify_typeform(headers: Dict[str, str], raw_body: bytes, secret: str) -> bool:
    """
    Typeform signature verification.
    Header: Typeform-Signature: sha256=<base64-encoded-hmac>
    """
    sig_header = headers.get("typeform-signature", "")
    if not sig_header:
        return False

    try:
        prefix = "sha256="
        if not sig_header.startswith(prefix):
            return False

        expected_b64 = sig_header[len(prefix):]

        computed = hmac.new(
            secret.encode(), raw_body, hashlib.sha256
        ).digest()
        computed_b64 = base64.b64encode(computed).decode()

        return hmac.compare_digest(computed_b64, expected_b64)
    except Exception as e:
        logger.error("Typeform signature verification error: %s", e)
        return False


def _verify_custom(headers: Dict[str, str], raw_body: bytes, secret: str) -> bool:
    """
    Custom webhook signature verification.
    Option 1: X-Versya-Signature header with HMAC-SHA256 hex digest
    Option 2: Authorization: Bearer <secret> (simple token match)
    """
    # Try HMAC signature first
    sig_header = headers.get("x-versya-signature", "")
    if sig_header:
        try:
            computed = hmac.new(
                secret.encode(), raw_body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(computed, sig_header)
        except Exception as e:
            logger.error("Custom HMAC verification error: %s", e)
            return False

    # Fall back to Bearer token
    auth_header = headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        return hmac.compare_digest(token, secret)

    return False
