"""
Email Instances CRUD + test + verify router.
"""
import logging
import os
from datetime import datetime
from typing import List

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EmailInstance
from app.routers.auth import get_current_user
from app.schemas.email_instances import (
    EmailInstanceCreate,
    EmailInstanceResponse,
    EmailInstanceTestRequest,
    EmailInstanceUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email-instances", tags=["email-instances"])


# ── Encryption helpers ────────────────────────────────────────────────────

def _get_encryption_key() -> bytes:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        import base64
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    return key.encode()


def _encrypt(value: str) -> str:
    f = Fernet(_get_encryption_key())
    return f.encrypt(value.encode()).decode()


# ── Helpers ───────────────────────────────────────────────────────────────

def _extract_user_info(current_user):
    if isinstance(current_user, dict):
        return {
            "user_id": int(current_user.get("id", 1)),
            "workspace_id": current_user.get("workspace", {}).get("id", 1),
        }
    return {
        "user_id": current_user.id,
        "workspace_id": current_user.workspace_id,
    }


def _to_response(inst: EmailInstance) -> EmailInstanceResponse:
    import os
    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
    feedback_url = f"{api_base}/webhooks/email/{inst.id}/feedback" if api_base else None

    return EmailInstanceResponse(
        id=inst.id,
        user_id=inst.user_id,
        workspace_id=inst.workspace_id,
        project_id=inst.project_id,
        provider_type=inst.provider_type,
        instance_name=inst.instance_name,
        from_email=inst.from_email,
        from_name=inst.from_name,
        smtp_server=inst.smtp_server if inst.provider_type == "smtp" else None,
        smtp_port=inst.smtp_port if inst.provider_type == "smtp" else None,
        smtp_username=inst.smtp_username if inst.provider_type == "smtp" else None,
        smtp_use_tls=inst.smtp_use_tls if inst.provider_type == "smtp" else None,
        smtp_use_ssl=inst.smtp_use_ssl if inst.provider_type == "smtp" else None,
        api_config=inst.api_config if inst.provider_type == "api" else None,
        has_api_key=bool(inst.api_key_enc) if inst.provider_type == "api" else False,
        connection_status=inst.connection_status or "pending",
        is_active=inst.is_active,
        created_at=inst.created_at,
        updated_at=inst.updated_at,
        last_verified_at=inst.last_verified_at,
        feedback_webhook_url=feedback_url,
        feedback_configured=bool(inst.feedback_webhook_secret),
    )


def _get_owned_instance(db: Session, instance_id: int, workspace_id: int) -> EmailInstance:
    inst = db.query(EmailInstance).filter(
        EmailInstance.id == instance_id,
        EmailInstance.workspace_id == workspace_id,
    ).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Email instance not found")
    return inst


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("", response_model=List[EmailInstanceResponse])
def list_instances(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    info = _extract_user_info(current_user)
    instances = db.query(EmailInstance).filter(
        EmailInstance.workspace_id == info["workspace_id"],
    ).order_by(EmailInstance.created_at.desc()).all()
    return [_to_response(i) for i in instances]


@router.post("", response_model=EmailInstanceResponse, status_code=status.HTTP_201_CREATED)
def create_instance(
    payload: EmailInstanceCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    info = _extract_user_info(current_user)

    # Check unique name
    existing = db.query(EmailInstance).filter(
        EmailInstance.instance_name == payload.instance_name,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Instance name already exists")

    inst = EmailInstance(
        user_id=info["user_id"],
        workspace_id=info["workspace_id"],
        project_id=payload.project_id,
        provider_type=payload.provider_type,
        instance_name=payload.instance_name,
        from_email=payload.from_email,
        from_name=payload.from_name,
    )

    if payload.provider_type == "smtp":
        inst.smtp_server = payload.smtp_server
        inst.smtp_port = payload.smtp_port
        inst.smtp_username = payload.smtp_username
        if payload.smtp_password:
            inst.smtp_password_enc = _encrypt(payload.smtp_password)
        inst.smtp_use_tls = payload.smtp_use_tls
        inst.smtp_use_ssl = payload.smtp_use_ssl
    elif payload.provider_type == "api":
        if payload.api_key:
            inst.api_key_enc = _encrypt(payload.api_key)
        if payload.api_config:
            inst.api_config = payload.api_config.dict()

    # Auto-generate feedback webhook secret
    import secrets
    inst.feedback_webhook_secret = secrets.token_hex(32)

    db.add(inst)
    db.commit()
    db.refresh(inst)
    return _to_response(inst)


@router.get("/{instance_id}", response_model=EmailInstanceResponse)
def get_instance(
    instance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    info = _extract_user_info(current_user)
    inst = _get_owned_instance(db, instance_id, info["workspace_id"])
    return _to_response(inst)


@router.put("/{instance_id}", response_model=EmailInstanceResponse)
def update_instance(
    instance_id: int,
    payload: EmailInstanceUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    info = _extract_user_info(current_user)
    inst = _get_owned_instance(db, instance_id, info["workspace_id"])

    update_data = payload.dict(exclude_unset=True)

    # Handle name uniqueness
    if "instance_name" in update_data and update_data["instance_name"] != inst.instance_name:
        existing = db.query(EmailInstance).filter(
            EmailInstance.instance_name == update_data["instance_name"],
            EmailInstance.id != instance_id,
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="Instance name already exists")

    # Handle secret fields
    if "smtp_password" in update_data:
        pw = update_data.pop("smtp_password")
        if pw:
            inst.smtp_password_enc = _encrypt(pw)

    if "api_key" in update_data:
        ak = update_data.pop("api_key")
        if ak:
            inst.api_key_enc = _encrypt(ak)

    if "api_config" in update_data and update_data["api_config"] is not None:
        inst.api_config = update_data.pop("api_config").dict()
    else:
        update_data.pop("api_config", None)

    for field, value in update_data.items():
        setattr(inst, field, value)

    # Reset status when credentials change
    if any(k in payload.dict(exclude_unset=True) for k in [
        "smtp_server", "smtp_port", "smtp_username", "smtp_password",
        "api_key", "api_config",
    ]):
        inst.connection_status = "pending"

    db.commit()
    db.refresh(inst)
    return _to_response(inst)


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_instance(
    instance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    info = _extract_user_info(current_user)
    inst = _get_owned_instance(db, instance_id, info["workspace_id"])
    db.delete(inst)
    db.commit()


@router.post("/{instance_id}/test")
async def test_instance(
    instance_id: int,
    payload: EmailInstanceTestRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    info = _extract_user_info(current_user)
    inst = _get_owned_instance(db, instance_id, info["workspace_id"])

    from app.services.email_sender import EmailSender
    sender = EmailSender(db)
    result = await sender.send_email(
        instance=inst,
        to_email=payload.to_email,
        subject=payload.subject,
        html_body=payload.message,
    )

    from app.services.channels.send_log_helper import record_direct_send
    record_direct_send(
        db=db, project_id=getattr(inst, "project_id", None), channel="email",
        recipient=payload.to_email, content_summary=payload.subject or "Email test",
        source_type="email_test", instance_id=inst.id,
        status="sent" if result.get("success") else "failed",
        provider_message_id=result.get("message_id"),
        error_message=result.get("error"),
        content_payload={"content_type": "rich", "html": payload.message, "subject": payload.subject},
    )
    db.commit()

    return result


@router.post("/{instance_id}/verify")
async def verify_instance(
    instance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    info = _extract_user_info(current_user)
    inst = _get_owned_instance(db, instance_id, info["workspace_id"])

    from app.services.email_sender import EmailSender
    sender = EmailSender(db)
    result = await sender.verify_instance(inst)

    inst.connection_status = "verified" if result["success"] else "failed"
    inst.last_verified_at = datetime.utcnow()
    db.commit()

    return result
