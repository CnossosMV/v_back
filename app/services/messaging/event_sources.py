"""Server-owned provenance labels for messaging events."""

from typing import Optional
from urllib.parse import urlparse


FRONTEND = "frontend"
PUBLIC_API = "public_api"
BACKEND = "backend"
SYSTEM = "system"
SEND_SERVICE = "send_service"
DELIVERY_TRACKER = "delivery_tracker"
MANUAL_TRIGGER = "manual_trigger"
SANDBOX = "sandbox"



def origin_domain(origin: Optional[str]) -> Optional[str]:
    """Return the lower-case hostname from an Origin or Referer header."""
    if not origin:
        return None
    try:
        return (urlparse(origin).hostname or "").lower() or None
    except (TypeError, ValueError):
        return None


def origin_matches_domain(origin: Optional[str], domain) -> bool:
    """Check an Origin/Referer against a MessagingDomain and its aliases."""
    request_domain = origin_domain(origin)
    if not request_domain:
        return False

    allowed = [str(domain.domain).lower()]
    if domain.allowed_origins:
        allowed.extend(str(value).lower() for value in domain.allowed_origins)

    normalized = []
    for value in allowed:
        parsed = origin_domain(value if "://" in value else f"https://{value}")
        if parsed:
            normalized.append(parsed)

    return any(
        request_domain == candidate or request_domain.endswith("." + candidate)
        for candidate in normalized
    )


def source_for_public_request(request, domain) -> str:
    """Classify write-key ingestion without trusting a payload field."""
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    return FRONTEND if origin_matches_domain(origin, domain) else PUBLIC_API
