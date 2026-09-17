"""
Router for Meta WhatsApp Cloud API (BYOC / tenant-provided credentials).

Project-scoped endpoints live under /whatsapp/meta/project/{project_id}/...
and enforce require_project_role. The old user-scoped routes are kept
temporarily as DEPRECATED compatibility shims that enforce the same
project RBAC derived from the instance; they will be removed once external
consumers are confirmed migrated.

Webhook routes (/whatsapp/meta/webhook/{instance_id}) are public and MUST
NOT change — the URLs are registered in tenants' Meta app dashboards.
Webhook payloads are rejected unless signed with the instance app_secret
(fail closed — unsigned payloads are never processed).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from sqlalchemy import and_
from datetime import datetime
import logging
import os
import secrets as secrets_mod

from app.database import get_db
from app import models
from app.dependencies import require_project_role
from app.routers.auth import get_current_user
from app.routers.whatsapp import extract_user_info
from app.schemas.meta_whatsapp import (
    MetaWhatsAppConnectRequest,
    MetaWhatsAppUpdateRequest,
    MetaWhatsAppInstanceResponse,
    MetaTestMessageRequest,
    ContactWindowResponse,
)
from app.services.meta_cloud_api_service import meta_cloud_api_service
from app.services.whatsapp_sender import WhatsAppSender
from app.services.whatsapp_window_service import WhatsAppWindowService
from app.services.encryption_service import (
    encrypt_value as _encrypt,
    decrypt_value as _decrypt,
    decrypt_value_or_none,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp/meta", tags=["whatsapp-meta"])


def _build_response(instance: models.WhatsAppInstance) -> MetaWhatsAppInstanceResponse:
    return MetaWhatsAppInstanceResponse(
        id=instance.id,
        provider_type=instance.provider_type,
        phone_number_id=instance.meta_phone_number_id,
        waba_id=instance.meta_waba_id,
        phone_number=instance.phone_number,
        business_name=instance.meta_business_name,
        connection_status=instance.connection_status,
        webhook_url=instance.webhook_url,
        webhook_verify_token=instance.meta_webhook_verify_token,
        default_country_code=instance.default_country_code,
        is_active=instance.is_active,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
    )


def _backend_base() -> str:
    backend_base = os.getenv("BACKEND_BASE_URL")
    if not backend_base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BACKEND_BASE_URL not configured. Contact the administrator.",
        )
    return backend_base.rstrip("/")


def _get_project_instance(
    db: Session, project_id: int, instance_id: int
) -> models.WhatsAppInstance:
    instance = (
        db.query(models.WhatsAppInstance)
        .filter(
            and_(
                models.WhatsAppInstance.id == instance_id,
                models.WhatsAppInstance.project_id == project_id,
                models.WhatsAppInstance.provider_type == "meta_cloud_api",
            )
        )
        .first()
    )
    if not instance:
        raise HTTPException(status_code=404, detail="Meta WhatsApp instance not found")
    return instance


def _find_phone_conflict(
    db: Session, phone_number_id: str, exclude_instance_id: int = None
):
    """Active meta_cloud_api instance already using this phone_number_id.

    Ownership rule: one active instance per phone number globally —
    enforced here and by uq_wa_meta_phone_active on PostgreSQL.
    """
    q = db.query(models.WhatsAppInstance).filter(
        models.WhatsAppInstance.provider_type == "meta_cloud_api",
        models.WhatsAppInstance.meta_phone_number_id == phone_number_id,
        models.WhatsAppInstance.is_active == True,
    )
    if exclude_instance_id:
        q = q.filter(models.WhatsAppInstance.id != exclude_instance_id)
    return q.first()


def _require_role_on_instance(
    db: Session, current_user, instance: models.WhatsAppInstance, min_role: str
) -> int:
    """RBAC for the deprecated instance-id routes: derive the project from
    the instance and require membership there. Legacy quarantined rows
    (project_id NULL) fall back to creator ownership."""
    from app.services.authorization_service import check_project_access, is_workspace_admin

    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    if is_workspace_admin(db, current_user):
        return user_id
    if instance.project_id:
        if check_project_access(db, user_id, instance.project_id, min_role):
            return user_id
    elif instance.user_id == user_id:
        return user_id
    raise HTTPException(
        status_code=403,
        detail=f"Requires at least '{min_role}' role on this instance's project",
    )


def _deprecation_warn(route: str, user_id) -> None:
    logger.warning(
        "DEPRECATED route %s used by user %s — use /whatsapp/meta/project/{project_id}/...",
        route,
        user_id,
    )


# ── Shared implementations ─────────────────────────────────────────────

async def _connect_impl(
    db: Session,
    body: MetaWhatsAppConnectRequest,
    project: models.Project,
    user_id: int,
) -> MetaWhatsAppInstanceResponse:
    backend_base = _backend_base()

    validation = await meta_cloud_api_service.validate_credentials(
        phone_number_id=body.phone_number_id,
        access_token=body.access_token,
    )
    if not validation.get("valid"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid Meta credentials: {validation.get('error', 'unknown error')}",
        )

    conflict = _find_phone_conflict(db, body.phone_number_id)
    if conflict:
        if conflict.project_id == project.id:
            detail = "This phone number is already connected in this project — update the existing instance instead."
        else:
            detail = "This phone number is already connected in another project."
        raise HTTPException(status_code=409, detail=detail)

    verify_token = secrets_mod.token_urlsafe(24)
    instance_name = f"meta_{body.phone_number_id}_{secrets_mod.token_hex(4)}"
    instance = models.WhatsAppInstance(
        user_id=user_id,  # audit: creator; authorization is project-based
        workspace_id=project.workspace_id,
        project_id=project.id,
        provider_type="meta_cloud_api",
        instance_name=instance_name,
        meta_phone_number_id=body.phone_number_id,
        meta_waba_id=body.waba_id,
        meta_access_token_enc=_encrypt(body.access_token),
        meta_app_secret_enc=_encrypt(body.app_secret),
        meta_webhook_verify_token=verify_token,
        meta_business_name=body.business_name or validation.get("verified_name"),
        default_country_code=body.default_country_code or None,
        phone_number=validation.get("phone_number"),
        connection_status="connected",
        is_active=True,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)

    instance.webhook_url = f"{backend_base}/api/v1/whatsapp/meta/webhook/{instance.id}"
    db.commit()
    db.refresh(instance)

    return _build_response(instance)


def _update_impl(
    db: Session, instance: models.WhatsAppInstance, body: MetaWhatsAppUpdateRequest
) -> MetaWhatsAppInstanceResponse:
    if body.access_token:
        instance.meta_access_token_enc = _encrypt(body.access_token)
    if body.app_secret:
        instance.meta_app_secret_enc = _encrypt(body.app_secret)
    if body.business_name is not None:
        instance.meta_business_name = body.business_name
    if body.default_country_code is not None:
        instance.default_country_code = body.default_country_code or None

    instance.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(instance)
    return _build_response(instance)


async def _status_impl(
    db: Session, instance: models.WhatsAppInstance
) -> MetaWhatsAppInstanceResponse:
    access_token = decrypt_value_or_none(instance.meta_access_token_enc)
    if access_token and instance.meta_phone_number_id:
        validation = await meta_cloud_api_service.validate_credentials(
            phone_number_id=instance.meta_phone_number_id,
            access_token=access_token,
        )
        if validation.get("valid"):
            instance.connection_status = "connected"
            instance.phone_number = validation.get("phone_number", instance.phone_number)
        else:
            instance.connection_status = "disconnected"

    # Webhooks fail closed without an app secret — surface it
    if not instance.meta_app_secret_enc:
        instance.connection_status = "needs_reauth"
    db.commit()
    db.refresh(instance)

    return _build_response(instance)


async def _templates_impl(db: Session, instance: models.WhatsAppInstance) -> list:
    access_token = decrypt_value_or_none(instance.meta_access_token_enc)
    waba_id = instance.meta_waba_id
    if not access_token or not waba_id:
        raise HTTPException(status_code=400, detail="Meta credentials not configured")

    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://graph.facebook.com/v21.0/{waba_id}/message_templates",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "name,language,status,category,components", "limit": 100},
        )
        data = resp.json()
        if resp.status_code != 200:
            detail = data.get("error", {}).get("message", "Failed to fetch templates")
            # Map upstream 401/403 to 502 so the frontend doesn't confuse it with a JWT auth failure
            http_status = 502 if resp.status_code in (401, 403) else resp.status_code
            raise HTTPException(status_code=http_status, detail=detail)

    return [
        {
            "name": t["name"],
            "language": t.get("language", ""),
            "status": t.get("status", ""),
            "category": t.get("category", ""),
            "components": t.get("components", []),
        }
        for t in data.get("data", [])
    ]


async def _test_impl(
    db: Session, instance: models.WhatsAppInstance, body: MetaTestMessageRequest
):
    sender = WhatsAppSender(db)

    if body.message_type == "template":
        if not body.template_name:
            raise HTTPException(status_code=400, detail="template_name is required for template messages")
        result = await sender.send_message(
            instance=instance,
            to_number=body.to_number,
            message="",
            template_fallback={
                "template_name": body.template_name,
                "language": body.template_language or "en_US",
                "components": body.template_components,
            },
        )
    else:
        text = body.text or "Hello from Versya!"
        # For test messages, bypass window check by sending template if window closed
        access_token = decrypt_value_or_none(instance.meta_access_token_enc)
        if not access_token or not instance.meta_phone_number_id:
            raise HTTPException(status_code=400, detail="Meta credentials not configured")
        result = await meta_cloud_api_service.send_text_message(
            phone_number_id=instance.meta_phone_number_id,
            access_token=access_token,
            to=body.to_number.lstrip("+"),
            text=text,
        )

    from app.services.channels.send_log_helper import record_direct_send
    record_direct_send(
        db=db, project_id=instance.project_id, channel="whatsapp",
        recipient=body.to_number, content_summary=body.text or body.template_name or "Meta WA test",
        source_type="meta_test", instance_id=instance.id,
        status="sent" if result.get("success", True) else "failed",
        provider_message_id=result.get("messages", [{}])[0].get("id") if isinstance(result, dict) else None,
        error_message=result.get("error") if isinstance(result, dict) else None,
    )
    db.commit()

    return result


def _disconnect_impl(db: Session, instance: models.WhatsAppInstance) -> dict:
    instance.is_active = False
    instance.connection_status = "disconnected"
    instance.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Meta WhatsApp instance disconnected"}


# ── Project-scoped endpoints ───────────────────────────────────────────

@router.get(
    "/project/{project_id}/instances",
    response_model=list[MetaWhatsAppInstanceResponse],
)
async def list_project_meta_instances(
    project_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("viewer")),
):
    """List Meta Cloud API instances of this project."""
    instances = (
        db.query(models.WhatsAppInstance)
        .filter(
            and_(
                models.WhatsAppInstance.project_id == project_id,
                models.WhatsAppInstance.provider_type == "meta_cloud_api",
            )
        )
        .order_by(models.WhatsAppInstance.id.desc())
        .all()
    )
    return [_build_response(i) for i in instances]


@router.post(
    "/project/{project_id}/connect",
    response_model=MetaWhatsAppInstanceResponse,
)
async def connect_project_meta_whatsapp(
    project_id: int,
    body: MetaWhatsAppConnectRequest,
    db: Session = Depends(get_db),
    membership=Depends(require_project_role("admin")),
):
    """Validate Meta credentials and create a project-scoped instance."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return await _connect_impl(db, body, project, membership["user_id"])


@router.put(
    "/project/{project_id}/instances/{instance_id}",
    response_model=MetaWhatsAppInstanceResponse,
)
async def update_project_meta_instance(
    project_id: int,
    instance_id: int,
    body: MetaWhatsAppUpdateRequest,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("admin")),
):
    instance = _get_project_instance(db, project_id, instance_id)
    return _update_impl(db, instance, body)


@router.get(
    "/project/{project_id}/instances/{instance_id}/status",
    response_model=MetaWhatsAppInstanceResponse,
)
async def get_project_meta_status(
    project_id: int,
    instance_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("editor")),
):
    instance = _get_project_instance(db, project_id, instance_id)
    return await _status_impl(db, instance)


@router.get("/project/{project_id}/instances/{instance_id}/templates")
async def list_project_meta_templates(
    project_id: int,
    instance_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("editor")),
):
    instance = _get_project_instance(db, project_id, instance_id)
    return await _templates_impl(db, instance)


@router.post("/project/{project_id}/instances/{instance_id}/test")
async def send_project_meta_test_message(
    project_id: int,
    instance_id: int,
    body: MetaTestMessageRequest,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("editor")),
):
    instance = _get_project_instance(db, project_id, instance_id)
    return await _test_impl(db, instance, body)


@router.delete("/project/{project_id}/instances/{instance_id}")
async def disconnect_project_meta_instance(
    project_id: int,
    instance_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("admin")),
):
    instance = _get_project_instance(db, project_id, instance_id)
    return _disconnect_impl(db, instance)


# ── DEPRECATED user-scoped compatibility shims ─────────────────────────

@router.get(
    "/instances",
    response_model=list[MetaWhatsAppInstanceResponse],
    deprecated=True,
)
async def list_meta_instances(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """DEPRECATED — use /whatsapp/meta/project/{project_id}/instances.
    Lists instances in projects where the user is a member (viewer+)."""
    from app.services.authorization_service import is_workspace_admin

    user_info = extract_user_info(current_user)
    _deprecation_warn("GET /whatsapp/meta/instances", user_info["user_id"])

    q = db.query(models.WhatsAppInstance).filter(
        models.WhatsAppInstance.provider_type == "meta_cloud_api",
    )
    if is_workspace_admin(db, current_user):
        q = q.filter(models.WhatsAppInstance.workspace_id == user_info["workspace_id"])
    else:
        member_project_ids = [
            m.project_id
            for m in db.query(models.ProjectMember)
            .filter(
                models.ProjectMember.user_id == user_info["user_id"],
                models.ProjectMember.is_active == True,
            )
            .all()
        ]
        q = q.filter(
            models.WhatsAppInstance.project_id.in_(member_project_ids)
            | (
                models.WhatsAppInstance.project_id.is_(None)
                & (models.WhatsAppInstance.user_id == user_info["user_id"])
            )
        )
    instances = q.order_by(models.WhatsAppInstance.id.desc()).all()
    return [_build_response(i) for i in instances]


@router.post("/connect", response_model=MetaWhatsAppInstanceResponse, deprecated=True)
async def connect_meta_whatsapp(
    body: MetaWhatsAppConnectRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """DEPRECATED — use /whatsapp/meta/project/{project_id}/connect.
    Works only when the user administers exactly one project."""
    from app.services.authorization_service import ROLE_HIERARCHY, is_workspace_admin

    user_info = extract_user_info(current_user)
    _deprecation_warn("POST /whatsapp/meta/connect", user_info["user_id"])

    if is_workspace_admin(db, current_user):
        projects = (
            db.query(models.Project)
            .filter(
                models.Project.workspace_id == user_info["workspace_id"],
                models.Project.is_active == True,
            )
            .all()
        )
    else:
        min_level = ROLE_HIERARCHY["admin"]
        memberships = (
            db.query(models.ProjectMember)
            .filter(
                models.ProjectMember.user_id == user_info["user_id"],
                models.ProjectMember.is_active == True,
            )
            .all()
        )
        project_ids = [
            m.project_id for m in memberships if ROLE_HIERARCHY.get(m.role, 0) >= min_level
        ]
        projects = (
            db.query(models.Project)
            .filter(models.Project.id.in_(project_ids), models.Project.is_active == True)
            .all()
        )

    if len(projects) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "This deprecated route cannot determine the target project. "
                "Use POST /whatsapp/meta/project/{project_id}/connect instead."
            ),
        )
    return await _connect_impl(db, body, projects[0], user_info["user_id"])


@router.put("/{instance_id}", response_model=MetaWhatsAppInstanceResponse, deprecated=True)
async def update_meta_instance(
    instance_id: int,
    body: MetaWhatsAppUpdateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """DEPRECATED — use the project-scoped route."""
    instance = _load_meta_instance(db, instance_id)
    user_id = _require_role_on_instance(db, current_user, instance, "admin")
    _deprecation_warn(f"PUT /whatsapp/meta/{instance_id}", user_id)
    return _update_impl(db, instance, body)


@router.get(
    "/{instance_id}/status", response_model=MetaWhatsAppInstanceResponse, deprecated=True
)
async def get_meta_status(
    instance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """DEPRECATED — use the project-scoped route."""
    instance = _load_meta_instance(db, instance_id)
    user_id = _require_role_on_instance(db, current_user, instance, "editor")
    _deprecation_warn(f"GET /whatsapp/meta/{instance_id}/status", user_id)
    return await _status_impl(db, instance)


@router.get("/{instance_id}/templates", deprecated=True)
async def list_meta_templates(
    instance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """DEPRECATED — use the project-scoped route."""
    instance = _load_meta_instance(db, instance_id)
    user_id = _require_role_on_instance(db, current_user, instance, "editor")
    _deprecation_warn(f"GET /whatsapp/meta/{instance_id}/templates", user_id)
    return await _templates_impl(db, instance)


@router.post("/{instance_id}/test", deprecated=True)
async def send_meta_test_message(
    instance_id: int,
    body: MetaTestMessageRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """DEPRECATED — use the project-scoped route."""
    instance = _load_meta_instance(db, instance_id)
    user_id = _require_role_on_instance(db, current_user, instance, "editor")
    _deprecation_warn(f"POST /whatsapp/meta/{instance_id}/test", user_id)
    return await _test_impl(db, instance, body)


@router.delete("/{instance_id}", deprecated=True)
async def disconnect_meta_instance(
    instance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """DEPRECATED — use the project-scoped route."""
    instance = _load_meta_instance(db, instance_id)
    user_id = _require_role_on_instance(db, current_user, instance, "admin")
    _deprecation_warn(f"DELETE /whatsapp/meta/{instance_id}", user_id)
    return _disconnect_impl(db, instance)


def _load_meta_instance(db: Session, instance_id: int) -> models.WhatsAppInstance:
    instance = (
        db.query(models.WhatsAppInstance)
        .filter(
            and_(
                models.WhatsAppInstance.id == instance_id,
                models.WhatsAppInstance.provider_type == "meta_cloud_api",
            )
        )
        .first()
    )
    if not instance:
        raise HTTPException(status_code=404, detail="Meta WhatsApp instance not found")
    return instance


# ── Public webhook endpoints (no auth) ─────────────────────────────────

@router.get("/webhook/{instance_id}")
async def meta_webhook_verify(
    instance_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Meta webhook verification (GET) — returns hub.challenge."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode != "subscribe":
        raise HTTPException(status_code=403, detail="Invalid mode")

    instance = db.query(models.WhatsAppInstance).filter(
        models.WhatsAppInstance.id == instance_id,
        models.WhatsAppInstance.provider_type == "meta_cloud_api",
    ).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    if token != instance.meta_webhook_verify_token:
        raise HTTPException(status_code=403, detail="Invalid verify token")

    return int(challenge) if challenge and challenge.isdigit() else challenge


@router.post("/webhook/{instance_id}")
async def meta_webhook_receive(
    instance_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Receive incoming messages and status updates from Meta.

    Fails closed: payloads are processed ONLY after HMAC verification with
    the instance's app_secret. Instances without a stored secret reject all
    deliveries until the tenant adds one (status surfaces 'needs_reauth')."""
    instance = db.query(models.WhatsAppInstance).filter(
        models.WhatsAppInstance.id == instance_id,
        models.WhatsAppInstance.provider_type == "meta_cloud_api",
        models.WhatsAppInstance.is_active == True,
    ).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    body_bytes = await request.body()
    sig_header = request.headers.get("X-Hub-Signature-256", "")

    app_secret = decrypt_value_or_none(instance.meta_app_secret_enc)
    if not app_secret:
        logger.error(
            f"SECURITY: rejecting unsigned-verifiable webhook for instance {instance_id} — "
            f"app_secret missing or undecryptable; tenant must re-enter it"
        )
        raise HTTPException(status_code=403, detail="Webhook signature secret not configured")
    if not meta_cloud_api_service.verify_webhook_signature(body_bytes, sig_header, app_secret):
        logger.warning(f"Invalid webhook signature for instance {instance_id}")
        raise HTTPException(status_code=403, detail="Invalid signature")

    import json
    payload = json.loads(body_bytes)

    # Process each entry
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Process incoming messages
            for message in value.get("messages", []):
                await _process_meta_message(db, instance, message, value)

            # Process status updates
            for status_update in value.get("statuses", []):
                _process_meta_status(db, instance, status_update)

    return {"status": "ok"}


# ── Internal helpers ────────────────────────────────────────────────────

async def _process_meta_message(
    db: Session,
    instance: models.WhatsAppInstance,
    message: dict,
    value: dict,
):
    """Process a single incoming Meta webhook message via InboundRouter."""
    from_number = message.get("from", "")

    # Update 24-h window. The Meta wa_id ("from") is already canonical E.164
    # digits without "+", so it IS the window key — store it raw. Outbound uses
    # normalize_phone() to converge on this same key (strips "+", and only
    # prepends default_country_code to local/national numbers). Normalizing here
    # would wrongly prepend the country code to a foreign wa_id.
    window_service = WhatsAppWindowService(db)
    window_service.update_window(instance.id, from_number)

    # Normalise and route
    try:
        from app.services.inbound.adapters import normalize_meta_cloud_api
        from app.services.inbound.router import InboundRouter

        inbound_msg = normalize_meta_cloud_api(
            message, value,
            instance_id=instance.id,
            instance_name=instance.instance_name,
        )
        logger.warning(f"[META_WH] Inbound message from={from_number} type={message.get('type')} instance={instance.id}")
        router = InboundRouter(db)
        result = await router.route(inbound_msg)
        logger.warning(f"[META_WH] Route result: {result}")
    except Exception as e:
        logger.error(f"Error processing Meta message: {e}", exc_info=True)


def _process_meta_status(
    db: Session,
    instance: models.WhatsAppInstance,
    status_update: dict,
):
    """Process a message status update from Meta webhook."""
    msg_id = status_update.get("id", "")
    status_val = status_update.get("status", "")  # sent, delivered, read, failed
    errors = status_update.get("errors", [])
    logger.warning(f"Meta status webhook: msg_id={msg_id}, status={status_val}, errors={errors}")

    if not msg_id:
        return

    # Update stored message status
    msg = db.query(models.WhatsAppMessage).filter(
        models.WhatsAppMessage.message_id == msg_id,
        models.WhatsAppMessage.instance_id == instance.id,
    ).first()
    if msg:
        msg.status = status_val
        msg.updated_at = datetime.utcnow()
        db.commit()

    # Propagate delivery status to ChatMessage (Support Inbox visibility)
    chat_msg = db.query(models.ChatMessage).filter(
        models.ChatMessage.external_message_id == msg_id,
    ).first()
    if chat_msg:
        new_status = None
        if status_val == "failed":
            new_status = "failed"
        elif status_val == "delivered":
            new_status = "delivered"
        elif status_val == "read":
            new_status = "read"
        if new_status:
            chat_msg.delivery_status = new_status
            db.commit()

            # Notify Support Inbox UI via WebSocket
            try:
                ticket = db.query(models.SupportTicket).filter(
                    models.SupportTicket.session_id == chat_msg.session_id,
                ).first()
                if ticket:
                    from app.services.support_inbox_service import SupportInboxService
                    error_info = status_update.get("errors", [{}])[0] if status_update.get("errors") else {}
                    svc = SupportInboxService(db)
                    svc.publish_delivery_status(
                        project_id=ticket.project_id,
                        ticket_id=ticket.id,
                        message_id=chat_msg.id,
                        delivery_status=new_status,
                        error=error_info.get("title") if error_info else None,
                    )
            except Exception as ws_err:
                logger.debug(f"WS delivery status publish skipped: {ws_err}")

    # Update unified SendLog via DeliveryTracker
    try:
        from app.services.channels.delivery_tracker import DeliveryTracker
        error_info = status_update.get("errors", [{}])[0] if status_update.get("errors") else {}
        DeliveryTracker(db).record_status(
            provider_message_id=msg_id,
            status=status_val,
            provider_status=status_val,
            error_code=str(error_info.get("code", "")) if error_info else None,
            error_message=error_info.get("title") if error_info else None,
            provider_timestamp=datetime.utcfromtimestamp(
                int(status_update["timestamp"])
            ) if status_update.get("timestamp") else None,
        )
        db.commit()
    except Exception as dt_err:
        logger.debug(f"DeliveryTracker update skipped: {dt_err}")
