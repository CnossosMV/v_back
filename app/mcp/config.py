"""Configuration helpers for Versya MCP endpoints."""
from dataclasses import dataclass
import os
from urllib.parse import urlparse
from typing import List, Optional


@dataclass(frozen=True)
class McpSettings:
    enabled: bool
    admin_enabled: bool
    public_base_url: Optional[str]
    auth_issuer: Optional[str]
    auth_audience: Optional[str]
    jwks_url: Optional[str]
    allowed_hosts: List[str]
    allowed_origins: List[str]
    readonly_sql_timeout_ms: int


def _csv_env(name: str) -> List[str]:
    value = os.getenv(name) or ""
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_allowed_hosts(public_base_url: Optional[str]) -> List[str]:
    if not public_base_url:
        return []
    hostname = urlparse(public_base_url).hostname
    if not hostname:
        return []
    return [hostname, f"{hostname}:*"]


def get_mcp_settings() -> McpSettings:
    try:
        sql_timeout = int(os.getenv("MCP_READONLY_SQL_TIMEOUT_MS", "5000"))
    except ValueError:
        sql_timeout = 5000

    keycloak_url = (os.getenv("KEYCLOAK_URL") or "").rstrip("/")
    keycloak_realm = os.getenv("KEYCLOAK_REALM")
    derived_issuer = f"{keycloak_url}/realms/{keycloak_realm}" if keycloak_url and keycloak_realm else None

    public_base_url = os.getenv("MCP_PUBLIC_BASE_URL") or os.getenv("BACKEND_BASE_URL")

    return McpSettings(
        enabled=os.getenv("MCP_ENABLED", "").lower() in {"1", "true", "yes"},
        admin_enabled=os.getenv("MCP_ADMIN_ENABLED", "").lower() in {"1", "true", "yes"},
        public_base_url=public_base_url,
        auth_issuer=os.getenv("MCP_AUTH_ISSUER") or derived_issuer,
        auth_audience=os.getenv("MCP_AUTH_AUDIENCE") or os.getenv("KEYCLOAK_CLIENT_ID"),
        jwks_url=os.getenv("MCP_JWKS_URL") or os.getenv("KEYCLOAK_JWKS_URL"),
        allowed_hosts=_csv_env("MCP_ALLOWED_HOSTS") or _default_allowed_hosts(public_base_url),
        allowed_origins=_csv_env("MCP_ALLOWED_ORIGINS"),
        readonly_sql_timeout_ms=sql_timeout,
    )


def mcp_resource_url(path: str = "") -> str:
    settings = get_mcp_settings()
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}{path}" if base else path
