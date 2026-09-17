"""
FastAPI dependency factories for role-based access control.
"""

from datetime import datetime
import hashlib

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Project
from app.routers.auth import get_current_user
from app.services.authorization_service import check_project_access, is_workspace_admin


def _authorize_project_user(db: Session, current_user, project_id: int, min_role: str):
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    workspace_id = (
        current_user.workspace_id
        if hasattr(current_user, "workspace_id")
        else current_user.get("workspace_id")
    )
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True,  # noqa: E712
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if is_workspace_admin(db, current_user):
        return {"user_id": user_id, "role": "owner", "bypass": True}
    membership = check_project_access(db, user_id, project_id, min_role)
    if not membership:
        raise HTTPException(
            status_code=403,
            detail=f"Requires at least '{min_role}' role on this project",
        )
    return {"user_id": user_id, "role": membership.role, "member_id": membership.id}


def require_project_role(min_role: str = "viewer"):
    """
    FastAPI dependency factory. Returns a dependency that validates
    the current user has at least `min_role` on the given project_id path param.

    Usage:
        @router.get("/projects/{project_id}/something")
        async def handler(
            project_id: int,
            membership=Depends(require_project_role("support_agent")),
            ...
        ):
    """

    async def _check(
        project_id: int,
        db: Session = Depends(get_db),
        current_user=Depends(get_current_user),
    ):
        return _authorize_project_user(db, current_user, project_id, min_role)

    return _check


def require_project_role_or_mcp_scope(min_role: str, required_scope: str):
    """Authorize either the dashboard JWT or the OAuth token used by product MCP.

    Binary Project Import payloads intentionally travel outside JSON-RPC, but
    remain under the same MCP connector, scope, tenant and project boundary.
    """

    async def _check(
        project_id: int,
        request: Request,
        authorization: str = Header(None),
        db: Session = Depends(get_db),
    ):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        raw_token = authorization.split(" ", 1)[1].strip()

        from app.routers.auth import get_current_user_from_token

        try:
            current_user = get_current_user_from_token(raw_token, db)
            result = _authorize_project_user(db, current_user, project_id, min_role)
            return {**result, "auth_kind": "dashboard"}
        except HTTPException as app_error:
            if app_error.status_code != 401:
                raise

        from app.mcp.auth import (
            McpAuthError,
            McpAuthorizationError,
            SUPPORTED_SCOPES,
            _extract_scopes,
            load_active_connector_installation,
            resolve_user,
            verify_oauth_token,
        )
        from app.mcp.config import get_mcp_settings

        try:
            claims = verify_oauth_token(raw_token, get_mcp_settings())
            current_user = resolve_user(db, claims)
            installation = load_active_connector_installation(
                db=db,
                user_id=current_user.id,
                server_type="product",
                client_name=request.headers.get("user-agent"),
            )
            origin = request.headers.get("origin")
            allowed_origins = installation.allowed_origins or []
            if origin and allowed_origins and origin not in allowed_origins:
                raise McpAuthorizationError("Origin is not allowed for this MCP connector")
            scopes = (_extract_scopes(claims) & SUPPORTED_SCOPES) & set(installation.scopes or [])
            if required_scope not in scopes:
                raise McpAuthError("Missing required MCP scope", required_scope)
            result = _authorize_project_user(db, current_user, project_id, min_role)
            return {
                **result,
                "auth_kind": "mcp",
                "connector_installation_id": installation.id,
            }
        except McpAuthError as exc:
            raise HTTPException(status_code=401, detail=exc.detail) from exc
        except McpAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    return _check


def require_project_import_upload_access():
    """Accept a one-time MCP upload grant, with normal authenticated fallback."""
    oauth_fallback = require_project_role_or_mcp_scope("editor", "versya.imports:write")

    async def _check(
        project_id: int,
        import_id: str,
        request: Request,
        authorization: str = Header(None),
        upload_token: str = Header(None, alias="X-Versya-Upload-Token"),
        db: Session = Depends(get_db),
    ):
        if upload_token:
            from app import models
            from app.models.project_import import ProjectImportUploadGrant

            token_hash = hashlib.sha256(upload_token.encode("utf-8")).hexdigest()
            grant = db.query(ProjectImportUploadGrant).filter(
                ProjectImportUploadGrant.token_hash == token_hash,
                ProjectImportUploadGrant.project_id == project_id,
                ProjectImportUploadGrant.import_id == import_id,
                ProjectImportUploadGrant.status == "active",
            ).first()
            if not grant:
                raise HTTPException(status_code=401, detail="Upload token is invalid or already used")
            if grant.expires_at < datetime.utcnow():
                grant.status = "expired"
                db.commit()
                raise HTTPException(status_code=401, detail="Upload token has expired")
            current_user = db.query(models.User).filter(
                models.User.id == grant.user_id,
                models.User.is_active == True,  # noqa: E712
            ).first()
            if not current_user:
                raise HTTPException(status_code=401, detail="Upload grant owner is no longer active")
            _authorize_project_user(db, current_user, project_id, "editor")
            return {
                "user_id": grant.user_id,
                "role": "upload_grant",
                "auth_kind": "project_import_upload_grant",
                "upload_grant_id": grant.id,
            }
        return await oauth_fallback(project_id, request, authorization, db)

    return _check
