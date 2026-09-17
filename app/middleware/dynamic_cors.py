"""
Dynamic CORS Middleware for SDK Endpoints

Allows CORS for SDK endpoints (/api/v1/track, /identify, /page, /verify-install)
based on registered domains in the database, rather than a static list.
"""
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
from sqlalchemy.orm import Session
from urllib.parse import urlparse
from typing import Optional
import re

from app.database import SessionLocal
from app.models.messaging import MessagingDomain


# SDK endpoints that need dynamic CORS
SDK_ENDPOINTS = [
    "/api/v1/track",
    "/api/v1/identify",
    "/api/v1/page",
    "/api/v1/verify-install",
    "/api/v1/visitor-data",
    "/api/v1/config",
    "/sdk/versya-messaging.js",
    "/sdk/version",
]


def get_origin_domain(origin: str) -> Optional[str]:
    """Extract domain from origin URL, removing port."""
    if not origin:
        return None
    try:
        parsed = urlparse(origin)
        return parsed.netloc.lower().split(':')[0]
    except:
        return None


def is_origin_allowed(db: Session, origin: str) -> bool:
    """Check if origin matches any registered domain."""
    origin_domain = get_origin_domain(origin)
    if not origin_domain:
        return False

    # Get all active domains
    domains = db.query(MessagingDomain).filter(
        MessagingDomain.is_active == True
    ).all()

    for domain in domains:
        # Check main domain
        registered_domain = domain.domain.lower().split(':')[0]

        # Exact match or subdomain match
        if origin_domain == registered_domain or origin_domain.endswith('.' + registered_domain):
            return True

        # Check allowed_origins
        if domain.allowed_origins:
            for allowed in domain.allowed_origins:
                allowed_domain = allowed.lower().split(':')[0]
                if origin_domain == allowed_domain or origin_domain.endswith('.' + allowed_domain):
                    return True

    # Also allow localhost variants for development
    if origin_domain in ['localhost', '127.0.0.1', '0.0.0.0'] or origin_domain.endswith('.local'):
        return True

    return False


def add_cors_headers(response: Response, origin: str) -> Response:
    """Add CORS headers to response."""
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Write-Key, X-API-Key, X-Timestamp, X-Signature, X-Nonce, X-SDK-Version, Authorization"
    response.headers["Access-Control-Max-Age"] = "600"
    return response


class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """
    Middleware that handles CORS for SDK endpoints dynamically
    based on registered domains in the database.
    """

    async def dispatch(self, request: Request, call_next):
        # Check if this is an SDK endpoint
        path = request.url.path
        is_sdk_endpoint = any(path.startswith(endpoint) for endpoint in SDK_ENDPOINTS)

        if not is_sdk_endpoint:
            # Not an SDK endpoint, let the default CORS middleware handle it
            return await call_next(request)

        origin = request.headers.get("origin", "")

        # Handle OPTIONS preflight
        if request.method == "OPTIONS":
            if origin:
                db = SessionLocal()
                try:
                    if is_origin_allowed(db, origin):
                        response = StarletteResponse(status_code=200)
                        return add_cors_headers(response, origin)
                finally:
                    db.close()

            # Origin not allowed or missing
            return StarletteResponse(status_code=204)

        # Handle actual request
        response = await call_next(request)

        # Add CORS headers if origin is allowed
        if origin:
            db = SessionLocal()
            try:
                if is_origin_allowed(db, origin):
                    add_cors_headers(response, origin)
            finally:
                db.close()

        return response
