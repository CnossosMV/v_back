"""Audit and confirmation helpers for MCP tools."""
from __future__ import annotations

from datetime import datetime, date, time, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import secrets
import uuid
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app import models
from app.mcp.auth import McpPrincipal


SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "client_secret",
    "password",
    "secret",
    "token",
}


def redact_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if any(sensitive in str(key).lower() for sensitive in SENSITIVE_KEYS):
                result[key] = "[redacted]"
            else:
                result[key] = redact_value(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item) for item in list(value)[:50]]
    if isinstance(value, str):
        return value[:500] + "...[truncated]" if len(value) > 500 else value
    # JSON-unsafe types the JSONB column can't store — make them safe so the
    # audit insert never fails (it must not break the tool it audits).
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")[:500]
    if isinstance(value, Enum):
        return redact_value(value.value)
    if hasattr(value, "isoformat"):  # other date-likes
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)[:500]


def summarize_payload(payload: Any) -> Any:
    return redact_value(payload)


def log_tool_call(
    db: Session,
    principal: Optional[McpPrincipal],
    tool_name: str,
    status: str,
    input_summary: Optional[Dict[str, Any]] = None,
    output_summary: Optional[Any] = None,
    error_message: Optional[str] = None,
    project_id: Optional[int] = None,
) -> models.McpToolAuditLog:
    log = models.McpToolAuditLog(
        project_id=project_id,
        user_id=principal.user_id if principal else None,
        connector_installation_id=principal.connector_installation_id if principal else None,
        server_type=principal.server_type if principal else "product",
        tool_name=tool_name,
        status=status,
        input_summary=summarize_payload(input_summary),
        output_summary=summarize_payload(output_summary),
        error_message=(error_message[:2000] if error_message else None),
        correlation_id=principal.correlation_id if principal else secrets.token_hex(16),
        ip_address=principal.ip_address if principal else None,
        user_agent=(principal.user_agent[:255] if principal and principal.user_agent else None),
        origin=(principal.origin[:255] if principal and principal.origin else None),
    )
    # Audit logging must NEVER break the tool it audits. If the insert fails,
    # roll back and continue — losing one audit row beats failing the call.
    try:
        db.add(log)
        db.commit()
        db.refresh(log)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "MCP audit log write failed for tool %s; continuing", tool_name, exc_info=True,
        )
        db.rollback()
    return log


def hash_confirm_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_pending_action(
    db: Session,
    principal: McpPrincipal,
    tool_name: str,
    payload: Dict[str, Any],
    project_id: Optional[int] = None,
    ttl_minutes: int = 15,
) -> Dict[str, Any]:
    raw_token = secrets.token_urlsafe(32)
    pending = models.McpPendingAction(
        project_id=project_id,
        user_id=principal.user_id,
        server_type=principal.server_type,
        tool_name=tool_name,
        action_payload=summarize_payload(payload),
        token_hash=hash_confirm_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(minutes=ttl_minutes),
    )
    db.add(pending)
    db.commit()
    db.refresh(pending)
    return {
        "pending_action_id": pending.id,
        "confirm_token": raw_token,
        "expires_at": pending.expires_at.isoformat(),
    }


def consume_pending_action(
    db: Session,
    principal: McpPrincipal,
    tool_name: str,
    confirm_token: str,
    expected_payload: Optional[Dict[str, Any]] = None,
) -> models.McpPendingAction:
    token_hash = hash_confirm_token(confirm_token)
    pending = (
        db.query(models.McpPendingAction)
        .filter(
            models.McpPendingAction.token_hash == token_hash,
            models.McpPendingAction.user_id == principal.user_id,
            models.McpPendingAction.tool_name == tool_name,
            models.McpPendingAction.status == "pending",
        )
        .first()
    )
    if not pending:
        raise ValueError("Confirmation token was not found")
    if pending.expires_at < datetime.utcnow():
        pending.status = "expired"
        db.commit()
        raise ValueError("Confirmation token has expired")
    if expected_payload is not None and pending.action_payload != summarize_payload(expected_payload):
        raise ValueError(
            "The approved action no longer matches current state; run dry-run and request a new confirmation token"
        )

    pending.status = "confirmed"
    pending.confirmed_at = datetime.utcnow()
    db.commit()
    db.refresh(pending)
    return pending
