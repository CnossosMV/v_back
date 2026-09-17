"""Authentication and authorization for remote MCP calls."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Set
import uuid

import jwt
from jwt import PyJWKClient
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app import models
from app.mcp.config import McpSettings, get_mcp_settings, mcp_resource_url
from app.services.authorization_service import check_project_access, is_workspace_admin


class McpAuthError(Exception):
    def __init__(self, detail: str, required_scope: Optional[str] = None):
        super().__init__(detail)
        self.detail = detail
        self.required_scope = required_scope


class McpAuthorizationError(Exception):
    pass


@dataclass(frozen=True)
class McpPrincipal:
    user_id: int
    email: Optional[str]
    scopes: Set[str]
    token_claims: Dict[str, Any]
    server_type: str
    correlation_id: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    origin: Optional[str] = None
    connector_installation_id: Optional[int] = None
    default_project_id: Optional[int] = None
    connector_status: Optional[str] = None


current_principal: ContextVar[Optional[McpPrincipal]] = ContextVar("mcp_principal", default=None)


_jwks_client_cache: dict[str, PyJWKClient] = {}


def _www_authenticate(scope: Optional[str] = None) -> str:
    metadata_url = mcp_resource_url("/.well-known/oauth-protected-resource")
    value = f'Bearer resource_metadata="{metadata_url}"'
    if scope:
        value += f', scope="{scope}"'
    return value


def unauthorized_response(detail: str, required_scope: Optional[str] = None) -> JSONResponse:
    return JSONResponse(
        {"detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": _www_authenticate(required_scope)},
    )


def protected_resource_metadata() -> Dict[str, Any]:
    settings = get_mcp_settings()
    resource = mcp_resource_url("/mcp/")
    scopes_supported = set(SUPPORTED_SCOPES)
    # This metadata describes the tenant product endpoint. The administrative
    # MCP may coexist on the same host, but its scope must never be advertised
    # to dynamically registered tenant agents.
    scopes_supported.discard("versya.admin:ops")
    scopes_supported.add(AGENT_SESSION_SCOPE)
    return {
        "resource": resource,
        "authorization_servers": [settings.auth_issuer] if settings.auth_issuer else [],
        "scopes_supported": sorted(scopes_supported),
        "resource_documentation": mcp_resource_url("/docs/official/modules/MCP.md"),
    }


SUPPORTED_SCOPES = {
    "versya.projects:read",
    "versya.projects:write",
    "versya.templates:read",
    "versya.templates:write",
    "versya.variables:read",
    "versya.variables:write",
    "versya.funnels:read",
    "versya.funnels:write",
    "versya.automations:read",
    "versya.automations:write",
    "versya.contacts:read",
    "versya.agents:read",
    "versya.channels:read",
    "versya.imports:read",
    "versya.imports:write",
    "versya.lifecycle:read",
    "versya.lifecycle:write",
    "versya.ingestion:read",
    "versya.ingestion:write",
    "versya.campaigns:read",
    "versya.campaigns:write",
    "versya.admin:ops",
}

PRODUCT_BOOTSTRAP_SCOPE = "versya.projects:read"
AGENT_SESSION_SCOPE = "offline_access"


def oauth_challenge_scope(server_type: str, required_scope: Optional[str] = None) -> str:
    """Return the minimum scope that makes an MCP OAuth token usable."""
    if required_scope:
        return required_scope
    if server_type == "admin":
        return "versya.admin:ops"
    return f"{PRODUCT_BOOTSTRAP_SCOPE} {AGENT_SESSION_SCOPE}"


def _extract_bearer(headers: Dict[str, str]) -> str:
    authorization = headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise McpAuthError("Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def _extract_scopes(payload: Dict[str, Any]) -> Set[str]:
    scopes: Set[str] = set()
    scope_claim = payload.get("scope")
    if isinstance(scope_claim, str):
        scopes.update(scope_claim.split())
    scp_claim = payload.get("scp")
    if isinstance(scp_claim, list):
        scopes.update(str(item) for item in scp_claim)
    realm_access = payload.get("realm_access") or {}
    if isinstance(realm_access, dict):
        scopes.update(str(item) for item in realm_access.get("roles") or [])
    return scopes


def _jwks_client_for(settings: McpSettings) -> PyJWKClient:
    if not settings.auth_issuer:
        raise McpAuthError("MCP_AUTH_ISSUER is not configured")
    jwks_url = settings.jwks_url or f"{settings.auth_issuer.rstrip('/')}/protocol/openid-connect/certs"
    if jwks_url not in _jwks_client_cache:
        _jwks_client_cache[jwks_url] = PyJWKClient(jwks_url)
    return _jwks_client_cache[jwks_url]


def verify_oauth_token(raw_token: str, settings: Optional[McpSettings] = None) -> Dict[str, Any]:
    settings = settings or get_mcp_settings()
    if not settings.auth_issuer:
        raise McpAuthError("MCP_AUTH_ISSUER is not configured")
    if not settings.auth_audience:
        raise McpAuthError("MCP_AUTH_AUDIENCE is not configured")

    try:
        signing_key = _jwks_client_for(settings).get_signing_key_from_jwt(raw_token)
        return jwt.decode(
            raw_token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512"],
            audience=settings.auth_audience,
            issuer=settings.auth_issuer,
        )
    except jwt.PyJWTError as exc:
        raise McpAuthError("Invalid or expired bearer token") from exc


def resolve_user(db: Session, payload: Dict[str, Any]) -> models.User:
    subject = payload.get("sub")
    email = payload.get("email")

    query = db.query(models.User).filter(models.User.is_active == True)
    if subject:
        user = query.filter(models.User.keycloak_id == str(subject)).first()
        if user:
            return user

    if email:
        user = query.filter(models.User.email == str(email)).first()
        if user:
            return user

    raise McpAuthError("Token user is not linked to an active Versya user")


def require_principal() -> McpPrincipal:
    principal = current_principal.get()
    if not principal:
        raise McpAuthError("MCP request is not authenticated")
    return principal


def require_scopes(required: Iterable[str]) -> McpPrincipal:
    principal = require_principal()
    missing = [scope for scope in required if scope not in principal.scopes]
    if missing:
        raise McpAuthError("Missing required MCP scope", missing[0])
    return principal


def require_admin_principal() -> McpPrincipal:
    principal = require_scopes(["versya.admin:ops"])
    return principal


def is_mcp_admin_user(db: Session, user: models.User) -> bool:
    return user.role == "admin"


def require_project_access(db: Session, project_id: int, min_role: str = "viewer") -> McpPrincipal:
    principal = require_principal()
    user = db.query(models.User).filter(models.User.id == principal.user_id).first()
    if not user:
        raise McpAuthError("MCP user no longer exists")
    if is_workspace_admin(db, user):
        return principal
    if not check_project_access(db, principal.user_id, project_id, min_role):
        raise McpAuthorizationError(f"Requires at least '{min_role}' role on this project")
    return principal


class McpAuthMiddleware:
    """ASGI middleware for OAuth and Origin checks before FastMCP handles JSON-RPC."""

    def __init__(self, app: ASGIApp, server_type: str):
        self.app = app
        self.server_type = server_type

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        if method == "OPTIONS":
            response = Response(status_code=204)
            await response(scope, receive, send)
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        settings = get_mcp_settings()
        origin = headers.get("origin")

        from app.database import SessionLocal

        db = SessionLocal()
        try:
            token = _extract_bearer(headers)
            payload = verify_oauth_token(token, settings)
            user = resolve_user(db, payload)
            token_scopes = _extract_scopes(payload) & SUPPORTED_SCOPES
            if self.server_type == "admin" and "versya.admin:ops" not in token_scopes:
                raise McpAuthError("Missing required MCP scope", "versya.admin:ops")
            if self.server_type == "admin":
                if not is_mcp_admin_user(db, user):
                    raise McpAuthorizationError("User is not allowed to use admin MCP")

            installation = load_active_connector_installation(
                db=db,
                user_id=user.id,
                server_type=self.server_type,
                client_name=headers.get("user-agent"),
            )
            allowed_origins = installation.allowed_origins or []
            if origin and allowed_origins and origin not in allowed_origins:
                raise McpAuthorizationError("Origin is not allowed for this MCP connector")
            scopes = token_scopes & set(installation.scopes or [])
            if self.server_type == "admin" and "versya.admin:ops" not in scopes:
                raise McpAuthError("Missing required MCP scope", "versya.admin:ops")
            client = scope.get("client")
            ip_address = client[0] if client else None
            principal = McpPrincipal(
                user_id=user.id,
                email=user.email,
                scopes=scopes,
                token_claims=payload,
                server_type=self.server_type,
                correlation_id=headers.get("x-request-id") or uuid.uuid4().hex,
                ip_address=ip_address,
                user_agent=headers.get("user-agent"),
                origin=origin,
                connector_installation_id=installation.id,
                default_project_id=installation.project_id,
                connector_status=installation.status,
            )
            token_var = current_principal.set(principal)
            try:
                await self.app(scope, receive, send)
            finally:
                current_principal.reset(token_var)
        except McpAuthError as exc:
            response = unauthorized_response(
                exc.detail,
                oauth_challenge_scope(self.server_type, exc.required_scope),
            )
            await response(scope, receive, send)
        except McpAuthorizationError as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=403)
            await response(scope, receive, send)
        finally:
            db.close()


def load_active_connector_installation(
    db: Session,
    user_id: int,
    server_type: str,
    client_name: Optional[str],
) -> models.McpConnectorInstallation:
    installation = (
        db.query(models.McpConnectorInstallation)
        .filter(
            models.McpConnectorInstallation.user_id == user_id,
            models.McpConnectorInstallation.connector_type == server_type,
            models.McpConnectorInstallation.status == "active",
        )
        .order_by(models.McpConnectorInstallation.updated_at.desc())
        .first()
    )
    if not installation:
        raise McpAuthorizationError("MCP connector is not enabled for this user")

    from datetime import datetime

    installation.client_name = (client_name or "")[:100] or installation.client_name
    installation.last_used_at = datetime.utcnow()
    db.commit()
    db.refresh(installation)
    return installation
