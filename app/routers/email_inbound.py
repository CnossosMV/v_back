"""
Email Inbound Router — webhook + CRUD for inbound email addresses.

Two sub-routers:
- webhook_router (public, HMAC-verified): POST /webhooks/email/inbound/{instance_id}
- router (auth required): CRUD for inbound addresses, enable/disable, MX verification
"""
import hmac as hmac_module
import logging
import os
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_project_role
from app.models import CustomerSMTPConfig, EmailInboundAddress
from app.schemas.email_inbound import (
    EmailInboundAddressCreate,
    EmailInboundAddressResponse,
    EmailInboundAddressUpdate,
    InboundEnableRequest,
    InboundStatusResponse,
    MXVerificationResponse,
)

logger = logging.getLogger(__name__)

# ── Public webhook router ─────────────────────────────────────────────

webhook_router = APIRouter(tags=["Email Inbound Webhooks"])


@webhook_router.post("/webhooks/email/inbound/{instance_id}")
async def email_inbound_webhook(
    instance_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Receive inbound email webhook from SES/Mailgun/SendGrid."""
    config = db.query(CustomerSMTPConfig).filter(
        CustomerSMTPConfig.id == instance_id,
        CustomerSMTPConfig.inbound_enabled == True,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Instance not found or inbound not enabled")

    provider_type = config.inbound_provider or "ses_inbound"

    # Parse body
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        raw_payload = await request.json()
    else:
        form = await request.form()
        raw_payload = dict(form)

    # Auth verification — Cloudflare Workers use direct secret, others use HMAC
    versya_secret = request.headers.get("x-versya-secret")
    if versya_secret:
        # Direct secret comparison for Cloudflare Email Workers
        from cryptography.fernet import Fernet
        enc_key = os.getenv("ENCRYPTION_KEY")
        secret = config.inbound_webhook_secret or ""
        if enc_key and secret:
            try:
                f = Fernet(enc_key.encode())
                secret = f.decrypt(secret.encode()).decode()
            except Exception:
                pass
        if not secret or not hmac_module.compare_digest(secret, versya_secret):
            raise HTTPException(status_code=403, detail="Invalid secret")
    else:
        # HMAC verification (optional — depends on provider)
        signature = request.headers.get("x-mailgun-signature") or request.headers.get("x-webhook-signature", "")
        if config.inbound_webhook_secret and signature:
            from app.services.inbound.email_inbound_service import EmailInboundService
            svc = EmailInboundService(db)
            body_bytes = await request.body()
            if not svc.verify_hmac(instance_id, body_bytes, signature):
                raise HTTPException(status_code=403, detail="Invalid signature")

    # Process
    from app.services.inbound.email_inbound_service import EmailInboundService
    svc = EmailInboundService(db)
    result = await svc.handle_webhook(instance_id, raw_payload, provider_type)
    return result


# ── Auth-protected router ─────────────────────────────────────────────

router = APIRouter(tags=["Email Inbound"])


@router.get(
    "/projects/{project_id}/email-instances/{instance_id}/inbound/addresses",
    response_model=List[EmailInboundAddressResponse],
)
def list_inbound_addresses(
    project_id: int,
    instance_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_project_role("viewer")),
):
    """List inbound email addresses for an SMTP instance."""
    addrs = db.query(EmailInboundAddress).filter(
        EmailInboundAddress.instance_id == instance_id,
        EmailInboundAddress.project_id == project_id,
    ).order_by(EmailInboundAddress.created_at).all()
    return addrs


@router.post(
    "/projects/{project_id}/email-instances/{instance_id}/inbound/addresses",
    response_model=EmailInboundAddressResponse,
    status_code=201,
)
def create_inbound_address(
    project_id: int,
    instance_id: int,
    payload: EmailInboundAddressCreate,
    db: Session = Depends(get_db),
    _=Depends(require_project_role("editor")),
):
    """Add an inbound email address."""
    # Verify instance belongs to project
    config = db.query(CustomerSMTPConfig).filter(
        CustomerSMTPConfig.id == instance_id,
        CustomerSMTPConfig.project_id == project_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="SMTP instance not found")

    # Check uniqueness
    existing = db.query(EmailInboundAddress).filter(
        EmailInboundAddress.instance_id == instance_id,
        EmailInboundAddress.address == payload.address.lower(),
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Address already exists")

    addr = EmailInboundAddress(
        instance_id=instance_id,
        project_id=project_id,
        address=payload.address.lower(),
        label=payload.label,
        is_active=payload.is_active,
        default_handler_type=payload.default_handler_type,
        default_handler_id=payload.default_handler_id,
    )
    db.add(addr)
    db.commit()
    db.refresh(addr)
    return addr


@router.put(
    "/projects/{project_id}/email-instances/{instance_id}/inbound/addresses/{address_id}",
    response_model=EmailInboundAddressResponse,
)
def update_inbound_address(
    project_id: int,
    instance_id: int,
    address_id: int,
    payload: EmailInboundAddressUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_project_role("editor")),
):
    """Update an inbound email address."""
    addr = db.query(EmailInboundAddress).filter(
        EmailInboundAddress.id == address_id,
        EmailInboundAddress.instance_id == instance_id,
        EmailInboundAddress.project_id == project_id,
    ).first()
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(addr, field, value)
    db.commit()
    db.refresh(addr)
    return addr


@router.delete(
    "/projects/{project_id}/email-instances/{instance_id}/inbound/addresses/{address_id}",
    status_code=204,
)
def delete_inbound_address(
    project_id: int,
    instance_id: int,
    address_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_project_role("editor")),
):
    """Delete an inbound email address."""
    addr = db.query(EmailInboundAddress).filter(
        EmailInboundAddress.id == address_id,
        EmailInboundAddress.instance_id == instance_id,
        EmailInboundAddress.project_id == project_id,
    ).first()
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")

    db.delete(addr)
    db.commit()


@router.post(
    "/projects/{project_id}/email-instances/{instance_id}/inbound/enable",
    response_model=InboundStatusResponse,
)
def enable_inbound(
    project_id: int,
    instance_id: int,
    payload: InboundEnableRequest,
    db: Session = Depends(get_db),
    _=Depends(require_project_role("admin")),
):
    """Enable or configure inbound email on an SMTP instance."""
    config = db.query(CustomerSMTPConfig).filter(
        CustomerSMTPConfig.id == instance_id,
        CustomerSMTPConfig.project_id == project_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="SMTP instance not found")

    valid_providers = ["ses_inbound", "mailgun_inbound", "sendgrid_inbound", "imap_poll", "cloudflare_email_workers"]
    if payload.provider not in valid_providers:
        raise HTTPException(status_code=400, detail=f"Provider must be one of: {', '.join(valid_providers)}")

    config.inbound_enabled = True
    config.inbound_provider = payload.provider

    if payload.provider == "cloudflare_email_workers":
        # Cloudflare manages MX records automatically — auto-verify
        config.mx_record_status = "verified"
        config.mx_record_verified_at = datetime.utcnow()
    elif not config.mx_record_status:
        config.mx_record_status = "pending"

    # Generate webhook secret if not set
    if not config.inbound_webhook_secret:
        import secrets as _secrets
        secret = _secrets.token_urlsafe(32)
        enc_key = os.getenv("ENCRYPTION_KEY")
        if enc_key:
            from cryptography.fernet import Fernet
            f = Fernet(enc_key.encode())
            secret = f.encrypt(secret.encode()).decode()
        config.inbound_webhook_secret = secret

    db.commit()
    db.refresh(config)

    api_base = os.getenv("API_BASE_URL", "")
    webhook_url = f"{api_base}/webhooks/email/inbound/{instance_id}" if api_base else None

    return InboundStatusResponse(
        inbound_enabled=config.inbound_enabled,
        inbound_provider=config.inbound_provider,
        mx_record_status=config.mx_record_status,
        mx_record_verified_at=config.mx_record_verified_at,
        webhook_url=webhook_url,
        address_count=db.query(EmailInboundAddress).filter(
            EmailInboundAddress.instance_id == instance_id
        ).count(),
    )


@router.post(
    "/projects/{project_id}/email-instances/{instance_id}/inbound/verify-mx",
    response_model=MXVerificationResponse,
)
def verify_mx(
    project_id: int,
    instance_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_project_role("admin")),
):
    """Verify MX DNS records for the domain."""
    config = db.query(CustomerSMTPConfig).filter(
        CustomerSMTPConfig.id == instance_id,
        CustomerSMTPConfig.project_id == project_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="SMTP instance not found")

    # Extract domain from from_email
    if not config.from_email or "@" not in config.from_email:
        raise HTTPException(status_code=400, detail="No valid from_email configured")

    domain = config.from_email.split("@")[1]

    from app.services.email_mx_verifier import verify_domain
    result = verify_domain(domain)

    config.mx_record_status = result["status"]
    if result["status"] == "verified":
        config.mx_record_verified_at = datetime.utcnow()
    db.commit()
    db.refresh(config)

    return MXVerificationResponse(
        status=result["status"],
        mx_records_found=result.get("mx_records_found", []),
        message=result.get("message"),
    )


@router.get(
    "/projects/{project_id}/email-instances/{instance_id}/inbound/status",
    response_model=InboundStatusResponse,
)
def get_inbound_status(
    project_id: int,
    instance_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_project_role("viewer")),
):
    """Get inbound email status for an SMTP instance."""
    config = db.query(CustomerSMTPConfig).filter(
        CustomerSMTPConfig.id == instance_id,
        CustomerSMTPConfig.project_id == project_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="SMTP instance not found")

    api_base = os.getenv("API_BASE_URL", "")
    webhook_url = f"{api_base}/webhooks/email/inbound/{instance_id}" if api_base and config.inbound_enabled else None

    return InboundStatusResponse(
        inbound_enabled=config.inbound_enabled,
        inbound_provider=config.inbound_provider,
        mx_record_status=config.mx_record_status,
        mx_record_verified_at=config.mx_record_verified_at,
        webhook_url=webhook_url,
        address_count=db.query(EmailInboundAddress).filter(
            EmailInboundAddress.instance_id == instance_id
        ).count(),
    )


CLOUDFLARE_WORKER_SNIPPET = '''export default {
  async email(message, env, ctx) {
    const response = await fetch("__WEBHOOK_URL__", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Versya-Secret": env.VERSYA_SECRET,
      },
      body: JSON.stringify({
        from: message.from,
        to: message.to,
        subject: message.headers.get("subject") || "",
        text: await new Response(message.raw).text(),
        messageId: message.headers.get("message-id") || "",
        inReplyTo: message.headers.get("in-reply-to") || "",
        references: message.headers.get("references") || "",
        headers: Object.fromEntries(message.headers),
      }),
    });
    if (!response.ok) {
      message.setReject("Webhook delivery failed");
    }
  },
};'''


@router.get(
    "/projects/{project_id}/email-instances/{instance_id}/inbound/worker-snippet",
)
def get_worker_snippet(
    project_id: int,
    instance_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_project_role("viewer")),
):
    """Return the Cloudflare Worker snippet with instance-specific values."""
    config = db.query(CustomerSMTPConfig).filter(
        CustomerSMTPConfig.id == instance_id,
        CustomerSMTPConfig.project_id == project_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="SMTP instance not found")

    api_base = os.getenv("API_BASE_URL", "")
    webhook_url = f"{api_base}/webhooks/email/inbound/{instance_id}"

    snippet = CLOUDFLARE_WORKER_SNIPPET.replace("__WEBHOOK_URL__", webhook_url)
    return {"snippet": snippet, "webhook_url": webhook_url}
