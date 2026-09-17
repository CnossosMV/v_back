"""FastAPI mounting helpers for MCP endpoints."""
from fastapi import APIRouter

from app.mcp.auth import (
    McpAuthMiddleware,
    PRODUCT_BOOTSTRAP_SCOPE,
    protected_resource_metadata,
    unauthorized_response,
)
from app.mcp.config import get_mcp_settings


metadata_router = APIRouter(tags=["mcp"])


@metadata_router.get("/.well-known/oauth-protected-resource")
def oauth_protected_resource_metadata():
    return protected_resource_metadata()


@metadata_router.get("/mcp/health")
def mcp_health():
    settings = get_mcp_settings()
    if not settings.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "product_endpoint": "/mcp/",
        "admin_enabled": settings.admin_enabled,
        "admin_endpoint": "/admin/mcp" if settings.admin_enabled else None,
    }


@metadata_router.get("/admin/mcp/health")
def admin_mcp_health():
    settings = get_mcp_settings()
    if not settings.enabled or not settings.admin_enabled:
        return {"enabled": False}
    return {"enabled": True, "admin_endpoint": "/admin/mcp"}


@metadata_router.get("/mcp/auth-challenge")
def mcp_auth_challenge():
    return unauthorized_response(
        "Authentication is required for Versya MCP",
        PRODUCT_BOOTSTRAP_SCOPE,
    )


def configure_mcp(app) -> None:
    app.include_router(metadata_router)

    settings = get_mcp_settings()
    app.state.mcp_servers = []
    if not settings.enabled:
        return

    from app.mcp.product_server import create_product_server

    product_server = create_product_server()

    app.mount("/mcp", McpAuthMiddleware(product_server.streamable_http_app(), "product"))
    app.state.mcp_servers = [product_server]

    if settings.admin_enabled:
        from app.mcp.admin_server import create_admin_server

        admin_server = create_admin_server()
        app.mount("/admin/mcp", McpAuthMiddleware(admin_server.streamable_http_app(), "admin"))
        app.state.mcp_servers.append(admin_server)
