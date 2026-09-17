"""UI endpoints for DB-managed MCP connector settings."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.dependencies import require_project_role
from app.mcp.auth import SUPPORTED_SCOPES, is_mcp_admin_user
from app.mcp.config import get_mcp_settings, mcp_resource_url
from app.routers.auth import get_current_user_from_token


router = APIRouter(tags=["mcp-settings"])

PRODUCT_SCOPES = sorted(scope for scope in SUPPORTED_SCOPES if scope != "versya.admin:ops")
DEFAULT_PRODUCT_SCOPES = PRODUCT_SCOPES


class McpConnectorUpdate(BaseModel):
    status: str = Field("active", pattern="^(active|revoked)$")
    scopes: List[str] = Field(default_factory=list)
    client_name: Optional[str] = Field(None, max_length=100)
    allowed_origins: List[str] = Field(default_factory=list)


class McpAdminConnectorUpdate(BaseModel):
    status: str = Field("active", pattern="^(active|revoked)$")
    client_name: Optional[str] = Field(None, max_length=100)
    allowed_origins: List[str] = Field(default_factory=list)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _validate_scopes(scopes: List[str], allowed: List[str]) -> List[str]:
    selected = sorted(set(scopes or allowed))
    invalid = [scope for scope in selected if scope not in allowed]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unsupported MCP scopes: {', '.join(invalid)}")
    return selected


def _connector_for(db: Session, user_id: int, connector_type: str) -> Optional[models.McpConnectorInstallation]:
    return (
        db.query(models.McpConnectorInstallation)
        .filter(
            models.McpConnectorInstallation.user_id == user_id,
            models.McpConnectorInstallation.connector_type == connector_type,
        )
        .order_by(models.McpConnectorInstallation.updated_at.desc())
        .first()
    )


def _connector_payload(connector: Optional[models.McpConnectorInstallation]) -> Optional[Dict[str, Any]]:
    if not connector:
        return None
    return {
        "id": connector.id,
        "project_id": connector.project_id,
        "connector_type": connector.connector_type,
        "client_name": connector.client_name,
        "scopes": connector.scopes or [],
        "allowed_origins": connector.allowed_origins or [],
        "status": connector.status,
        "last_used_at": _iso(connector.last_used_at),
        "created_at": _iso(connector.created_at),
        "updated_at": _iso(connector.updated_at),
    }


def _require_authenticated_user(
    authorization: str = Header(None),
    db: Session = Depends(get_db),
) -> models.User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    return get_current_user_from_token(authorization.split(" ", 1)[1], db)


def _upsert_connector(
    db: Session,
    user_id: int,
    connector_type: str,
    project_id: Optional[int],
    scopes: List[str],
    status: str,
    client_name: Optional[str],
    allowed_origins: List[str],
) -> models.McpConnectorInstallation:
    connector = _connector_for(db, user_id, connector_type)
    if not connector:
        connector = models.McpConnectorInstallation(
            user_id=user_id,
            connector_type=connector_type,
        )
        db.add(connector)

    connector.project_id = project_id
    connector.scopes = scopes
    connector.status = status
    connector.client_name = client_name or connector.client_name
    connector.allowed_origins = allowed_origins
    connector.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(connector)
    return connector


@router.get("/projects/{project_id}/mcp")
def get_project_mcp_settings(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_authenticated_user),
    _auth=Depends(require_project_role("admin")),
):
    connector = _connector_for(db, current_user.id, "product")
    mcp_settings = get_mcp_settings()
    admin_enabled = mcp_settings.admin_enabled
    admin_allowed = admin_enabled and is_mcp_admin_user(db, current_user)
    admin_connector = _connector_for(db, current_user.id, "admin") if admin_allowed else None
    return {
        "product_url": mcp_resource_url("/mcp/"),
        "admin_url": mcp_resource_url("/admin/mcp") if admin_enabled else None,
        "supported_scopes": PRODUCT_SCOPES,
        "default_scopes": DEFAULT_PRODUCT_SCOPES,
        "connector": _connector_payload(connector),
        "admin": {
            "enabled": admin_enabled,
            "allowed": admin_allowed,
            "connector": _connector_payload(admin_connector),
        },
        "current_project_id": project_id,
    }


@router.put("/projects/{project_id}/mcp")
def update_project_mcp_settings(
    project_id: int,
    data: McpConnectorUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_authenticated_user),
    _auth=Depends(require_project_role("admin")),
):
    connector = _upsert_connector(
        db=db,
        user_id=current_user.id,
        connector_type="product",
        project_id=project_id,
        scopes=_validate_scopes(data.scopes, PRODUCT_SCOPES),
        status=data.status,
        client_name=data.client_name,
        allowed_origins=data.allowed_origins,
    )
    return {"connector": _connector_payload(connector), "product_url": mcp_resource_url("/mcp/")}


@router.delete("/projects/{project_id}/mcp")
def revoke_project_mcp_settings(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_authenticated_user),
    _auth=Depends(require_project_role("admin")),
):
    connector = _connector_for(db, current_user.id, "product")
    if connector:
        connector.status = "revoked"
        connector.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(connector)
    return {"connector": _connector_payload(connector), "product_url": mcp_resource_url("/mcp/")}


@router.get("/projects/{project_id}/mcp/audit")
def list_project_mcp_audit_logs(
    project_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _current_user: models.User = Depends(_require_authenticated_user),
    _auth=Depends(require_project_role("admin")),
):
    logs = (
        db.query(models.McpToolAuditLog)
        .filter(models.McpToolAuditLog.project_id == project_id)
        .order_by(models.McpToolAuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "logs": [
            {
                "id": log.id,
                "server_type": log.server_type,
                "tool_name": log.tool_name,
                "status": log.status,
                "input_summary": log.input_summary,
                "output_summary": log.output_summary,
                "error_message": log.error_message,
                "correlation_id": log.correlation_id,
                "origin": log.origin,
                "user_agent": log.user_agent,
                "created_at": _iso(log.created_at),
            }
            for log in logs
        ]
    }


@router.get("/mcp/admin/status")
def get_admin_mcp_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_authenticated_user),
):
    admin_enabled = get_mcp_settings().admin_enabled
    allowed = admin_enabled and is_mcp_admin_user(db, current_user)
    connector = _connector_for(db, current_user.id, "admin") if allowed else None
    return {
        "enabled": admin_enabled,
        "allowed": allowed,
        "admin_url": mcp_resource_url("/admin/mcp") if admin_enabled else None,
        "connector": _connector_payload(connector),
    }


@router.put("/mcp/admin/connector")
def update_admin_mcp_connector(
    data: McpAdminConnectorUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_authenticated_user),
):
    if not get_mcp_settings().admin_enabled:
        raise HTTPException(status_code=404, detail="Admin MCP is disabled")
    if not is_mcp_admin_user(db, current_user):
        raise HTTPException(status_code=403, detail="Admin MCP is only available to platform admins")
    connector = _upsert_connector(
        db=db,
        user_id=current_user.id,
        connector_type="admin",
        project_id=None,
        scopes=["versya.admin:ops"],
        status=data.status,
        client_name=data.client_name,
        allowed_origins=data.allowed_origins,
    )
    return {"allowed": True, "admin_url": mcp_resource_url("/admin/mcp"), "connector": _connector_payload(connector)}
