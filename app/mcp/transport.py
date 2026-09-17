"""Transport-security helpers for remote MCP endpoints."""
from __future__ import annotations

from typing import Optional

from app.mcp.config import get_mcp_settings


def transport_security_settings() -> Optional[object]:
    from mcp.server.transport_security import TransportSecuritySettings

    settings = get_mcp_settings()
    if not settings.allowed_hosts:
        return None

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
