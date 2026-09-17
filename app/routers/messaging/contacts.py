"""
Contact Management API — admin endpoints for identity resolution.
"""
import csv
import io
import json
import logging
import uuid
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from app.database import get_db
from app.dependencies import require_project_role
from app.models import WhatsAppInstance, User
from app.routers.auth import get_current_user
from app.models.messaging import (
    MessagingUser, ContactIdentity, ContactMergeLog, MergeSuggestion
)
from app.schemas.identity import (
    ContactIdentityResponse, MergeLogResponse,
    MergeSuggestionResponse, ManualMergeRequest
)
from app.schemas.messaging import (
    MessagingUserResponse,
    ContactCreateRequest, ContactCreateResponse,
    CSVImportPreviewResponse, CSVImportResultResponse,
    SendToContactRequest, SendToContactResponse,
    ExternalTouchRequest, ExternalTouchResponse,
)
from app.services.messaging.contact_merge_service import ContactMergeService
from app.services.messaging.phone_normalizer import PhoneNormalizer
from app.services.messaging.pii_hasher import pii_hasher
from app.services.messaging.contact_lifecycle_events import lifecycle_emitter
from app.services.messaging.contact_profile_service import ContactProfileService

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/contacts",
    tags=["contacts"],
    dependencies=[Depends(require_project_role("viewer"))],
)


# ── Column alias map for CSV auto-detection ───────────────────────────
COLUMN_ALIASES = {
    'email': ['email', 'e-mail', 'email_address', 'emailaddress'],
    'phone': ['phone', 'telephone', 'tel', 'mobile', 'phone_number', 'phonenumber', 'celular'],
    'name': ['name', 'full_name', 'fullname', 'nome', 'contact_name'],
    'external_id': ['external_id', 'externalid', 'id', 'user_id', 'userid', 'customer_id'],
    'tags': ['tags', 'tag', 'labels'],
    'lifecycle_stage': ['lifecycle_stage', 'stage', 'lifecycle'],
}


def _get_fallback_cc(db: Session, project_id: int) -> Optional[str]:
    """Get fallback country code from WhatsApp instance."""
    instance = db.query(WhatsAppInstance).filter(
        WhatsAppInstance.project_id == project_id,
        WhatsAppInstance.is_active == True,
        WhatsAppInstance.default_country_code != None,
    ).first()
    return instance.default_country_code if instance else None


def _create_contact_identities(
    db: Session, project_id: int, user_id: int,
    external_id: str, email: Optional[str], phone_e164: Optional[str],
):
    """Create ContactIdentity records for a new contact."""
    identities = [
        ('contact_id', external_id),
    ]
    if email:
        identities.append(('email', email.lower().strip()))
    if phone_e164:
        identities.append(('phone', phone_e164))

    for id_type, id_value in identities:
        existing = db.query(ContactIdentity).filter(
            ContactIdentity.project_id == project_id,
            ContactIdentity.identity_type == id_type,
            ContactIdentity.identity_value == id_value,
        ).first()
        if not existing:
            db.add(ContactIdentity(
                project_id=project_id,
                user_id=user_id,
                identity_type=id_type,
                identity_value=id_value,
                verified=False,
                source='manual',
            ))


def _suggest_column_mapping(headers: list[str]) -> dict[str, str]:
    """Suggest a mapping from CSV headers to contact fields via alias matching."""
    mapping = {}
    for header in headers:
        normalized = header.lower().strip().replace(' ', '_')
        for field, aliases in COLUMN_ALIASES.items():
            if normalized in aliases:
                mapping[header] = field
                break
    return mapping


def _get_project_id(project_id: int) -> int:
    return project_id


# ── Create single contact ─────────────────────────────────────────────


@router.post("/create")
def create_contact(
    project_id: int,
    data: ContactCreateRequest,
    db: Session = Depends(get_db),
    _auth=Depends(require_project_role("editor")),
):
    """Manually create a single contact."""
    warnings: list[str] = []

    # Generate external_id if not provided
    external_id = data.external_id or str(uuid.uuid4())

    # Check uniqueness
    existing = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.external_id == external_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="A contact with this external_id already exists")

    # Phone normalization
    phone_e164 = None
    phone_norm_status = None
    if data.phone:
        fallback_cc = _get_fallback_cc(db, project_id)
        phone_e164, phone_norm_status = PhoneNormalizer.normalize(
            data.phone, locale=None, fallback_country_code=fallback_cc
        )
        if phone_norm_status not in ('valid_e164', 'inferred'):
            warnings.append(f"Phone normalization status: {phone_norm_status}")

    # PII hashing
    email_hash, phone_hash = pii_hasher.hash_user_pii(db, project_id, data.email, data.phone)

    # Create user
    user = MessagingUser(
        project_id=project_id,
        external_id=external_id,
        email=data.email,
        phone=data.phone,
        phone_e164=phone_e164,
        phone_norm_status=phone_norm_status,
        name=data.name,
        properties=data.properties,
        tags=data.tags,
        lifecycle_stage=data.lifecycle_stage,
        email_hash=email_hash,
        phone_hash=phone_hash,
        created_via='manual',
        status='active',
        is_subscribed=True,
    )
    db.add(user)
    db.flush()

    # Create identities
    _create_contact_identities(db, project_id, user.id, external_id, data.email, phone_e164)
    ContactProfileService(db).sync_user(user, source="manual")

    db.commit()
    db.refresh(user)

    # Emit lifecycle event (separate commit inside emitter)
    try:
        lifecycle_emitter.contact_created(db, project_id, user, created_via='manual')
    except Exception:
        logger.warning(f"Failed to emit contact.created for user={user.id}", exc_info=True)

    logger.info(f"Contact created manually: project={project_id} user={user.id} external_id={external_id}")
    return ContactCreateResponse(
        contact=MessagingUserResponse.model_validate(user),
        warnings=warnings,
    )


# ── CSV Import ────────────────────────────────────────────────────────


@router.post("/import-csv")
async def import_csv(
    project_id: int,
    preview: bool = Query(False),
    file: UploadFile = File(...),
    column_mapping: Optional[str] = Form(None),
    on_duplicate: str = Form("skip"),
    db: Session = Depends(get_db),
    _auth=Depends(require_project_role("editor")),
):
    """Import contacts from a CSV file. Use ?preview=true to preview headers and mapping."""
    # Validate file
    if not file.filename or not file.filename.lower().endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a .csv file")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")

    # Try UTF-8, fallback to latin-1
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = content.decode('latin-1')

    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if not headers:
        raise HTTPException(status_code=400, detail="CSV file has no headers")

    # ── Preview mode ──
    if preview:
        rows = []
        sample_rows: list[list[str]] = []
        for i, row in enumerate(reader):
            rows.append(row)
            if i < 5:
                sample_rows.append([row.get(h, '') for h in headers])
        return CSVImportPreviewResponse(
            headers=headers,
            sample_rows=sample_rows,
            total_rows=len(rows),
            suggested_mapping=_suggest_column_mapping(headers),
        )

    # ── Import mode ──
    if not column_mapping:
        raise HTTPException(status_code=400, detail="column_mapping is required for import")

    try:
        mapping = json.loads(column_mapping)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="column_mapping must be valid JSON")

    if on_duplicate not in ('skip', 'update', 'error'):
        raise HTTPException(status_code=400, detail="on_duplicate must be 'skip', 'update', or 'error'")

    # Get fallback CC once
    fallback_cc = _get_fallback_cc(db, project_id)

    created = 0
    updated = 0
    skipped = 0
    errors: list[dict] = []

    rows_list = list(reader)
    if len(rows_list) > 50000:
        raise HTTPException(status_code=400, detail="CSV file too large (max 50,000 rows)")

    for row_num, row in enumerate(rows_list, start=2):  # start=2 because row 1 is header
        try:
            # Map columns
            mapped: dict = {}
            extra_props: dict = {}
            for csv_header, csv_value in row.items():
                if not csv_value or not csv_value.strip():
                    continue
                contact_field = mapping.get(csv_header)
                if contact_field:
                    mapped[contact_field] = csv_value.strip()
                else:
                    extra_props[csv_header] = csv_value.strip()

            # Skip empty rows
            if not mapped.get('name') and not mapped.get('email') and not mapped.get('phone'):
                skipped += 1
                continue

            external_id = mapped.get('external_id') or str(uuid.uuid4())

            # Handle tags
            tags = None
            if mapped.get('tags'):
                tags = [t.strip() for t in mapped['tags'].split(',') if t.strip()]

            # Merge extra properties
            properties = extra_props if extra_props else None

            # Check for duplicates
            existing = db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.external_id == external_id,
            ).first()

            if existing:
                if on_duplicate == 'error':
                    raise HTTPException(
                        status_code=409,
                        detail=f"Duplicate external_id '{external_id}' at row {row_num}"
                    )
                elif on_duplicate == 'skip':
                    skipped += 1
                    continue
                elif on_duplicate == 'update':
                    # Update existing contact
                    if mapped.get('name'):
                        existing.name = mapped['name']
                    if mapped.get('email'):
                        existing.email = mapped['email']
                    if mapped.get('phone'):
                        existing.phone = mapped['phone']
                        if fallback_cc or mapped['phone'].startswith('+'):
                            e164, status = PhoneNormalizer.normalize(
                                mapped['phone'], locale=None, fallback_country_code=fallback_cc
                            )
                            existing.phone_e164 = e164
                            existing.phone_norm_status = status
                    if tags:
                        existing.tags = list(set((existing.tags or []) + tags))
                    if mapped.get('lifecycle_stage'):
                        existing.lifecycle_stage = mapped['lifecycle_stage']
                    if properties:
                        existing.properties = {**(existing.properties or {}), **properties}
                    # Re-hash PII
                    eh, ph = pii_hasher.hash_user_pii(db, project_id, existing.email, existing.phone)
                    existing.email_hash = eh
                    existing.phone_hash = ph
                    ContactProfileService(db).sync_user(existing, source="csv_import")
                    updated += 1
                    continue

            # Phone normalization
            phone_e164 = None
            phone_norm_status = None
            raw_phone = mapped.get('phone')
            if raw_phone:
                phone_e164, phone_norm_status = PhoneNormalizer.normalize(
                    raw_phone, locale=None, fallback_country_code=fallback_cc
                )

            # PII hashing
            email_hash, phone_hash = pii_hasher.hash_user_pii(
                db, project_id, mapped.get('email'), raw_phone
            )

            # Create user
            user = MessagingUser(
                project_id=project_id,
                external_id=external_id,
                email=mapped.get('email'),
                phone=raw_phone,
                phone_e164=phone_e164,
                phone_norm_status=phone_norm_status,
                name=mapped.get('name'),
                properties=properties,
                tags=tags,
                lifecycle_stage=mapped.get('lifecycle_stage'),
                email_hash=email_hash,
                phone_hash=phone_hash,
                created_via='csv_import',
                status='active',
                is_subscribed=True,
            )
            db.add(user)
            db.flush()

            # Create identities
            _create_contact_identities(
                db, project_id, user.id, external_id,
                mapped.get('email'), phone_e164,
            )
            ContactProfileService(db).sync_user(user, source="csv_import")

            created += 1

            # Batch commit every 100 rows
            if (created + updated) % 100 == 0:
                db.commit()

        except HTTPException:
            raise
        except Exception as e:
            errors.append({"row": row_num, "message": str(e)})
            if len(errors) > 100:
                break

    # Final commit
    db.commit()

    logger.info(
        f"CSV import: project={project_id} created={created} updated={updated} "
        f"skipped={skipped} errors={len(errors)}"
    )
    return CSVImportResultResponse(
        created=created,
        updated=updated,
        skipped=skipped,
        errors=errors,
    )


# ── Send message to contact ───────────────────────────────────────────


@router.post("/{contact_id}/send")
async def send_to_contact(
    project_id: int,
    contact_id: int,
    data: SendToContactRequest,
    db: Session = Depends(get_db),
    _auth=Depends(require_project_role("editor")),
):
    """Send a message to a specific contact with optional reply routing.

    Three modes:
    - template_id → internal template (email, SMS or WhatsApp)
    - free_text + channel → free-text WhatsApp or SMS
    - meta_template_name → Meta-approved WhatsApp template

    Every provider delivery goes through SendService. The explicit ``manual_send``
    source is protected from automatic Selection supersession, but still passes
    hard recipient, opt-out, verification and ledger gates.
    """
    from datetime import datetime, timedelta
    from app.models.messaging import MessagingTemplate
    from app.services.messaging import template_renderer
    from app.services.whatsapp_sender import infer_media_type

    # 1. Load contact
    user = db.query(MessagingUser).filter(
        MessagingUser.id == contact_id,
        MessagingUser.project_id == project_id,
        MessagingUser.status == 'active',
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Determine mode
    mode = None
    if data.meta_template_name:
        mode = "meta_template"
    elif data.free_text and data.channel:
        mode = "free_text"
    elif data.template_id:
        mode = "internal_template"
    else:
        raise HTTPException(status_code=400, detail="Provide template_id, free_text+channel, or meta_template_name")

    send_log_id = None
    rendered_body = None
    rendered_subject = None
    channel_type = None

    # Helper: build user variables context
    def _build_variables() -> dict:
        variables = dict(data.variables) if data.variables else {}
        variables["user"] = {
            "name": user.name or "",
            "email": user.email or "",
            "phone": user.phone_e164 or user.phone or "",
            "external_id": user.external_id,
        }
        if user.properties:
            for k, v in user.properties.items():
                if k not in variables:
                    variables[k] = v
        try:
            from app.services.project_variable_service import ProjectVariableService
            proj_vars = ProjectVariableService(db).render_project_variables(project_id, variables)
            variables["project"] = proj_vars
            variables["projects"] = proj_vars
        except Exception:
            pass
        return variables

    # Helper: resolve WhatsApp instance
    def _get_whatsapp_instance(require_provider: str = None):
        query = db.query(WhatsAppInstance).filter(
            WhatsAppInstance.is_active == True,
            WhatsAppInstance.connection_status.in_(["open", "connected"]),
        )
        if require_provider:
            query = query.filter(WhatsAppInstance.provider_type == require_provider)
        return query.first()

    # Helper: resolve recipient for channel
    def _resolve_recipient(ch: str) -> str:
        if ch == "email":
            r = user.email
            if not r:
                raise HTTPException(status_code=400, detail="Contact has no email address")
            return r
        elif ch in ("whatsapp", "sms"):
            r = user.phone_e164 or user.phone
            if not r:
                raise HTTPException(status_code=400, detail="Contact has no phone number")
            return r
        raise HTTPException(status_code=400, detail=f"Channel '{ch}' is not supported")

    # ── Mode: Meta WhatsApp template ──────────────────────────────────
    if mode == "meta_template":
        channel_type = "whatsapp"
        recipient = _resolve_recipient("whatsapp")

        instance = _get_whatsapp_instance(require_provider="meta_cloud_api")
        if not instance:
            return SendToContactResponse(
                success=False, message="No connected Meta WhatsApp instance found", channel=channel_type,
            )

        from app.services.channels.base import OutboundContent
        from app.services.channels.send_service import SendService
        result = await SendService(db).send(
            project_id=project_id,
            user_id=user.id,
            recipient=recipient,
            content=OutboundContent(
                content_type="template",
                template_name=data.meta_template_name,
                template_language=data.meta_template_language or "en_US",
                template_components=data.meta_template_components,
            ),
            channel="whatsapp",
            source_type="manual_send",
            source_id=None,
            instance_config={"instance_id": instance.id},
        )

        rendered_body = f"[Template: {data.meta_template_name}]"
        send_log_id = result.send_log_id
        db.commit()

        if not result.success:
            return SendToContactResponse(
                success=False,
                message=f"Failed to send: {result.error or 'Unknown error'}",
                channel=channel_type, send_log_id=send_log_id, rendered_body=rendered_body,
            )

    # ── Mode: Free text (WhatsApp or SMS) ──────────────────────────
    elif mode == "free_text":
        channel_type = data.channel
        if channel_type not in ("whatsapp", "sms"):
            raise HTTPException(status_code=400, detail="Free text is only supported for WhatsApp and SMS")

        if channel_type == "sms":
            recipient = _resolve_recipient("sms")
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            content = OutboundContent(content_type="text", text=data.free_text)
            svc = SendService(db)
            result = await svc.send(
                project_id=project_id,
                user_id=user.id,
                recipient=recipient,
                content=content,
                channel="sms",
                source_type="manual_send",
                source_id=0,
            )
            rendered_body = data.free_text
            send_log_id = result.send_log_id
            db.commit()

            if not result.success:
                return SendToContactResponse(
                    success=False,
                    message=f"Failed to send: {result.error or 'Unknown error'}",
                    channel=channel_type, send_log_id=send_log_id, rendered_body=rendered_body,
                )
        else:
            recipient = _resolve_recipient("whatsapp")
            instance = _get_whatsapp_instance()
            if not instance:
                return SendToContactResponse(
                    success=False, message="No connected WhatsApp instance found", channel=channel_type,
                )

            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            result = await SendService(db).send(
                project_id=project_id,
                user_id=user.id,
                recipient=recipient,
                content=OutboundContent(content_type="text", text=data.free_text),
                channel="whatsapp",
                source_type="manual_send",
                source_id=None,
                instance_config={"instance_id": instance.id},
            )

            rendered_body = data.free_text
            send_log_id = result.send_log_id
            db.commit()

            if not result.success:
                return SendToContactResponse(
                    success=False,
                    message=f"Failed to send: {result.error or 'Unknown error'}",
                    channel=channel_type, send_log_id=send_log_id, rendered_body=rendered_body,
                )

    # ── Mode: Internal template (email or WhatsApp) ───────────────────
    elif mode == "internal_template":
        template = db.query(MessagingTemplate).filter(
            MessagingTemplate.id == data.template_id,
            MessagingTemplate.project_id == project_id,
        ).first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")

        channel_type = template.channel_type.value if template.channel_type else "email"
        recipient = _resolve_recipient(channel_type)

        variables = _build_variables()
        rendered_body, rendered_subject, _, missing = template_renderer.render_template(
            template.body, variables, template.subject
        )

        if channel_type == "sms":
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            content = OutboundContent(content_type="text", text=rendered_body)
            svc = SendService(db)
            result = await svc.send(
                project_id=project_id,
                user_id=user.id,
                recipient=recipient,
                content=content,
                channel="sms",
                source_type="manual_send",
                source_id=template.id,
            )
            db.commit()
            return SendToContactResponse(
                success=result.success,
                message="SMS sent" if result.success else (result.error or "Failed to send SMS"),
                channel="sms", send_log_id=result.send_log_id,
                rendered_body=rendered_body,
            )

        if channel_type == "whatsapp":
            instance = _get_whatsapp_instance()
            if not instance:
                return SendToContactResponse(
                    success=False, message="No connected WhatsApp instance found", channel=channel_type,
                )

            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService
            result = await SendService(db).send(
                project_id=project_id,
                user_id=user.id,
                recipient=recipient,
                content=OutboundContent(content_type="text", text=rendered_body),
                channel="whatsapp",
                source_type="manual_send",
                source_id=template.id,
                template_id=template.id,
                instance_config={"instance_id": instance.id},
            )

            if result.success and template.media_url:
                m_type = infer_media_type(template.media_url)
                media_result = await SendService(db).send(
                    project_id=project_id,
                    user_id=user.id,
                    recipient=recipient,
                    content=OutboundContent(
                        content_type="media",
                        media_url=template.media_url,
                        media_type=m_type,
                    ),
                    channel="whatsapp",
                    source_type="manual_send",
                    source_id=template.id,
                    template_id=template.id,
                    instance_config={"instance_id": instance.id},
                )
                if not media_result.success:
                    logger.warning(
                        "Manual WhatsApp media send failed for contact=%s: %s",
                        contact_id, media_result.error,
                    )

            send_log_id = result.send_log_id
            db.commit()

            if not result.success:
                return SendToContactResponse(
                    success=False,
                    message=f"Failed to send: {result.error or 'Unknown error'}",
                    channel=channel_type, send_log_id=send_log_id, rendered_body=rendered_body,
                )

        elif channel_type == "email":
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService

            is_html = template.body_format != "plain" and "<" in (rendered_body or "")
            content = OutboundContent(
                content_type="rich" if is_html else "text",
                text=rendered_body,
                html=rendered_body if is_html else None,
                subject=rendered_subject or template.name,
                attachments=data.attachments or None,
            )

            instance_cfg = {}
            if template.from_email:
                instance_cfg["from_email"] = template.from_email
            if template.from_name:
                instance_cfg["from_name"] = template.from_name
            if template.reply_to:
                instance_cfg["reply_to"] = template.reply_to

            svc = SendService(db)
            decision = await svc.send(
                project_id=project_id, user_id=user.id, recipient=recipient,
                content=content, channel="email",
                source_type="manual_send", source_id=template.id,
                template_id=template.id, instance_config=instance_cfg or None,
                skip_policy=True,
            )
            send_log_id = decision.send_log_id
            db.commit()

            if not decision.success:
                return SendToContactResponse(
                    success=False,
                    message=f"Failed: {decision.error or 'Send failed'}",
                    channel=channel_type, send_log_id=send_log_id,
                    rendered_subject=rendered_subject, rendered_body=rendered_body,
                )

    # ── Set reply routing (all modes) ─────────────────────────────────
    if data.route_reply_to and data.route_reply_to in ("human", "chatbot", "agent_team"):
        try:
            from app.services.inbound.routing_state_service import RoutingStateService
            routing_svc = RoutingStateService(db)
            routing_svc.set_state(
                project_id=project_id,
                identifier=recipient,
                channel=channel_type,
                handler_type=data.route_reply_to,
                handler_id=data.route_reply_handler_id,
                expires_at=datetime.utcnow() + timedelta(hours=24),
                metadata={"set_by": "manual_send", "contact_id": contact_id},
            )
            db.commit()
        except Exception:
            logger.warning(f"Failed to set routing state for contact={contact_id}", exc_info=True)

    logger.info(f"Message sent to contact={contact_id} channel={channel_type} mode={mode}")
    return SendToContactResponse(
        success=True,
        message=f"Message sent via {channel_type}",
        channel=channel_type,
        send_log_id=send_log_id,
        rendered_subject=rendered_subject,
        rendered_body=rendered_body,
    )


@router.post("/{contact_id}/external-touch", response_model=ExternalTouchResponse)
def log_external_touch(
    project_id: int,
    contact_id: int,
    data: ExternalTouchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _auth=Depends(require_project_role("editor")),
):
    """Log an out-of-band interaction (call, visit, personal message...).

    Recorded as a SendLog (messages tab) + timeline event, and — when
    count_as_message is true — a ContactLedger row so guardian budget,
    caps and cooldowns treat it as an outbound message. Works for paused/
    blocked/opted-out contacts: it records history, it does not send.
    """
    from app.routers.messaging.users import get_project_or_404
    get_project_or_404(db, project_id, current_user.workspace_id)

    from app.services.messaging.external_touch_service import ExternalTouchService
    try:
        return ExternalTouchService(db).log_touch(
            project_id=project_id,
            user_id=contact_id,
            touch_type=data.touch_type,
            note=data.note,
            occurred_at=data.occurred_at,
            count_as_message=data.count_as_message,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("")
def list_contacts(
    project_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query("active", description="Filter: active, merged, deleted, all"),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """List contacts, excluding merged/deleted by default."""
    query = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.is_sandbox == False,
    )
    if status and status != "all":
        query = query.filter(MessagingUser.status == status)
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (MessagingUser.email.ilike(search_filter)) |
            (MessagingUser.name.ilike(search_filter)) |
            (MessagingUser.external_id.ilike(search_filter)) |
            (MessagingUser.phone.ilike(search_filter))
        )
    total = query.count()
    contacts = query.order_by(MessagingUser.created_at.desc()).offset(skip).limit(limit).all()
    return {"items": [MessagingUserResponse.model_validate(c) for c in contacts], "total": total}


@router.post("/merge")
def manual_merge(
    project_id: int,
    data: ManualMergeRequest,
    db: Session = Depends(get_db),
    auth=Depends(require_project_role("admin")),
):
    """Manually merge two contacts."""
    merge_svc = ContactMergeService(db)
    try:
        merge_log = merge_svc.merge_contacts(
            project_id=project_id,
            winner_id=data.winner_id,
            loser_id=data.loser_id,
            triggered_by='manual',
            triggered_by_user_id=auth["user_id"],
        )
        return MergeLogResponse.model_validate(merge_log)
    except ValueError as e:
        logger.warning(f"Merge failed project={project_id} winner={data.winner_id} loser={data.loser_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Merge error project={project_id} winner={data.winner_id} loser={data.loser_id}")
        raise HTTPException(status_code=500, detail="Internal merge error")


@router.get("/merge-suggestions", name="list_merge_suggestions")
def list_merge_suggestions(
    project_id: int,
    status: str = Query("pending"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db)
):
    """List merge suggestions."""
    query = db.query(MergeSuggestion).filter(
        MergeSuggestion.project_id == project_id
    )
    if status != "all":
        query = query.filter(MergeSuggestion.status == status)
    total = query.count()
    suggestions = query.order_by(MergeSuggestion.created_at.desc()).offset(skip).limit(limit).all()

    results = []
    for s in suggestions:
        resp = MergeSuggestionResponse.model_validate(s)
        # Add contact summaries
        contact_a = db.query(MessagingUser).filter(MessagingUser.id == s.contact_a_id).first()
        contact_b = db.query(MessagingUser).filter(MessagingUser.id == s.contact_b_id).first()
        if contact_a:
            resp.contact_a_summary = {
                "id": contact_a.id, "external_id": contact_a.external_id,
                "email": contact_a.email, "name": contact_a.name, "phone": contact_a.phone
            }
        if contact_b:
            resp.contact_b_summary = {
                "id": contact_b.id, "external_id": contact_b.external_id,
                "email": contact_b.email, "name": contact_b.name, "phone": contact_b.phone
            }
        results.append(resp)
    return {"items": results, "total": total}


@router.post("/merge-suggestions/{suggestion_id}/accept")
def accept_suggestion(
    project_id: int,
    suggestion_id: int,
    db: Session = Depends(get_db),
    auth=Depends(require_project_role("admin")),
):
    """Accept a merge suggestion."""
    merge_svc = ContactMergeService(db)
    try:
        merge_log = merge_svc.accept_merge_suggestion(
            suggestion_id,
            user_id=auth["user_id"],
            project_id=project_id,
        )
        return MergeLogResponse.model_validate(merge_log)
    except ValueError as e:
        logger.warning(f"Accept suggestion failed project={project_id} suggestion={suggestion_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Accept suggestion error project={project_id} suggestion={suggestion_id}")
        raise HTTPException(status_code=500, detail="Internal merge error")


@router.post("/merge-suggestions/{suggestion_id}/reject")
def reject_suggestion(
    project_id: int,
    suggestion_id: int,
    db: Session = Depends(get_db),
    auth=Depends(require_project_role("admin")),
):
    """Reject a merge suggestion."""
    merge_svc = ContactMergeService(db)
    try:
        merge_svc.reject_merge_suggestion(
            suggestion_id,
            user_id=auth["user_id"],
            project_id=project_id,
        )
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/merge-history/{merge_log_id}/undo")
def undo_merge(
    project_id: int,
    merge_log_id: int,
    db: Session = Depends(get_db),
    _auth=Depends(require_project_role("admin")),
):
    """Undo a merge: restore the loser contact and move back identities."""
    merge_svc = ContactMergeService(db)
    try:
        merge_log = merge_svc.undo_merge(merge_log_id, project_id)
        return MergeLogResponse.model_validate(merge_log)
    except ValueError as e:
        logger.warning(f"Undo merge failed project={project_id} log={merge_log_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Undo merge error project={project_id} log={merge_log_id}")
        raise HTTPException(status_code=500, detail="Internal undo error")


@router.patch("/update-phone")
def update_contact_phone(
    project_id: int,
    data: dict,
    db: Session = Depends(get_db),
    _auth=Depends(require_project_role("editor")),
):
    """Update a contact's phone number and re-normalize to E.164."""
    contact_id = data.get("contact_id")
    phone = data.get("phone", "").strip()
    if not contact_id or not phone:
        raise HTTPException(status_code=400, detail="contact_id and phone are required")

    user = db.query(MessagingUser).filter(
        MessagingUser.id == contact_id,
        MessagingUser.project_id == project_id,
        MessagingUser.status == 'active'
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Get fallback CC
    instance = db.query(WhatsAppInstance).filter(
        WhatsAppInstance.project_id == project_id,
        WhatsAppInstance.is_active == True,
        WhatsAppInstance.default_country_code != None,
    ).first()
    fallback_cc = instance.default_country_code if instance else None

    normalized, status = PhoneNormalizer.normalize(phone, locale=None, fallback_country_code=fallback_cc)

    user.phone = phone
    user.phone_e164 = normalized or phone
    user.phone_norm_status = status

    # Re-hash
    from app.services.messaging.pii_hasher import pii_hasher
    email_hash, phone_hash = pii_hasher.hash_user_pii(db, project_id, user.email, user.phone)
    user.phone_hash = phone_hash

    # Record normalized phone as identity
    if normalized:
        existing = db.query(ContactIdentity).filter(
            ContactIdentity.project_id == project_id,
            ContactIdentity.identity_type == 'phone',
            ContactIdentity.identity_value == normalized,
        ).first()
        if not existing:
            db.add(ContactIdentity(
                project_id=project_id,
                user_id=user.id,
                identity_type='phone',
                identity_value=normalized,
                verified=False,
                source='manual',
            ))

    db.commit()
    db.refresh(user)
    logger.info(f"Phone updated: user={user.id} phone={phone} e164={normalized} status={status}")
    return MessagingUserResponse.model_validate(user)


@router.post("/backfill-phones")
def backfill_phone_normalization(
    project_id: int,
    db: Session = Depends(get_db),
    _auth=Depends(require_project_role("admin")),
):
    """Re-normalize phone_e164 for all contacts in this project using the
    project's WhatsApp instance default_country_code. Safe to run multiple times."""
    # Get fallback CC from the project's WhatsApp instance
    from sqlalchemy import or_
    instance = db.query(WhatsAppInstance).filter(
        WhatsAppInstance.project_id == project_id,
        WhatsAppInstance.is_active == True,
        WhatsAppInstance.default_country_code != None,
    ).first()
    fallback_cc = instance.default_country_code if instance else None

    if not fallback_cc:
        raise HTTPException(
            status_code=400,
            detail="No WhatsApp instance with default_country_code configured. Set it first."
        )

    users = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.phone != None,
        MessagingUser.status == 'active',
        or_(
            MessagingUser.phone_norm_status == None,
            MessagingUser.phone_norm_status.in_(['missing', 'assumed_e164', 'invalid']),
        ),
    ).all()

    updated = 0
    for user in users:
        normalized, status = PhoneNormalizer.normalize(user.phone, locale=None, fallback_country_code=fallback_cc)
        if normalized and normalized != user.phone_e164:
            old_e164 = user.phone_e164
            user.phone_e164 = normalized
            user.phone_norm_status = status
            # Also record normalized phone as identity if not exists
            existing = db.query(ContactIdentity).filter(
                ContactIdentity.project_id == project_id,
                ContactIdentity.identity_type == 'phone',
                ContactIdentity.identity_value == normalized,
            ).first()
            if not existing:
                db.add(ContactIdentity(
                    project_id=project_id,
                    user_id=user.id,
                    identity_type='phone',
                    identity_value=normalized,
                    verified=False,
                    source='backfill',
                ))
            updated += 1
            logger.info(f"Backfill phone: user={user.id} old={old_e164} new={normalized} status={status}")

    db.commit()
    return {"updated": updated, "total_checked": len(users), "fallback_cc": fallback_cc}


def _duplicate_contact_groups(
    db: Session,
    project_id: int,
    *,
    field: str,
    limit_groups: int,
) -> List[Dict[str, Any]]:
    if field == "email":
        normalized = func.lower(func.trim(MessagingUser.email))
        base_filters = [
            MessagingUser.project_id == project_id,
            MessagingUser.status == "active",
            MessagingUser.is_sandbox == False,
            MessagingUser.email != None,
            normalized != "",
        ]
    elif field == "phone":
        normalized = func.trim(MessagingUser.phone_e164)
        base_filters = [
            MessagingUser.project_id == project_id,
            MessagingUser.status == "active",
            MessagingUser.is_sandbox == False,
            MessagingUser.phone_e164 != None,
            normalized != "",
        ]
    else:
        return []

    duplicate_keys = db.query(
        normalized.label("identity_key"),
        func.count(MessagingUser.id).label("contact_count"),
    ).filter(*base_filters).group_by(normalized).having(
        func.count(MessagingUser.id) > 1
    ).order_by(func.count(MessagingUser.id).desc()).limit(limit_groups).all()

    groups: List[Dict[str, Any]] = []
    for identity_key, contact_count in duplicate_keys:
        contacts = db.query(MessagingUser).filter(
            *base_filters,
            normalized == identity_key,
        ).order_by(MessagingUser.first_seen_at.asc(), MessagingUser.created_at.asc(), MessagingUser.id.asc()).all()
        if len(contacts) < 2:
            continue
        groups.append({
            "field": field,
            "identity_key": identity_key,
            "contact_count": int(contact_count),
            "winner_candidate_id": contacts[0].id,
            "loser_candidate_ids": [contact.id for contact in contacts[1:]],
            "contacts": [
                {
                    "id": contact.id,
                    "email": contact.email,
                    "phone": contact.phone,
                    "phone_e164": contact.phone_e164,
                    "name": contact.name,
                    "external_id": contact.external_id,
                    "first_seen_at": contact.first_seen_at.isoformat() if contact.first_seen_at else None,
                    "created_at": contact.created_at.isoformat() if contact.created_at else None,
                }
                for contact in contacts
            ],
        })
    return groups


@router.post("/backfill-duplicate-identities")
def backfill_duplicate_identities(
    project_id: int,
    dry_run: bool = Query(True),
    match_on: str = Query("both", pattern="^(email|phone|both)$"),
    limit_groups: int = Query(50, ge=1, le=500),
    limit_merges: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    _auth=Depends(require_project_role("admin")),
):
    """Find and optionally merge duplicate active contacts by normalized email/phone.

    Dry-run is the default. Executing the backfill uses ContactMergeService so
    history, events, identities, audience memberships, and merge logs remain
    auditable.
    """
    fields = ["email", "phone"] if match_on == "both" else [match_on]
    groups: List[Dict[str, Any]] = []
    for field in fields:
        groups.extend(_duplicate_contact_groups(db, project_id, field=field, limit_groups=limit_groups))

    seen_pairs = set()
    planned: List[Dict[str, Any]] = []
    for group in groups:
        winner_id = group["winner_candidate_id"]
        for loser_id in group["loser_candidate_ids"]:
            pair = tuple(sorted((winner_id, loser_id)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            planned.append({
                "field": group["field"],
                "identity_key": group["identity_key"],
                "winner_candidate_id": winner_id,
                "loser_candidate_id": loser_id,
            })
            if len(planned) >= limit_merges:
                break
        if len(planned) >= limit_merges:
            break

    if dry_run:
        return {
            "dry_run": True,
            "groups_found": len(groups),
            "merges_planned": len(planned),
            "groups": groups[:25],
            "planned": planned[:100],
        }

    merge_svc = ContactMergeService(db)
    merged: List[Dict[str, Any]] = []
    suggestions_created: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for item in planned:
        try:
            result = merge_svc.merge_contacts(
                project_id=project_id,
                winner_id=item["winner_candidate_id"],
                loser_id=item["loser_candidate_id"],
                triggered_by="audience_csv_backfill",
            )
            if isinstance(result, ContactMergeLog):
                merged.append({
                    **item,
                    "merge_log_id": result.id,
                    "winner_id": result.winner_id,
                    "loser_id": result.loser_id,
                })
            elif isinstance(result, MergeSuggestion):
                suggestions_created.append({
                    **item,
                    "suggestion_id": result.id,
                    "reason": result.match_reason,
                    "confidence": result.match_confidence,
                })
        except Exception as exc:
            db.rollback()
            logger.warning(
                "Duplicate identity backfill failed project=%s winner=%s loser=%s: %s",
                project_id,
                item["winner_candidate_id"],
                item["loser_candidate_id"],
                exc,
            )
            errors.append({**item, "error": str(exc)})

    return {
        "dry_run": False,
        "groups_found": len(groups),
        "merges_planned": len(planned),
        "merged": len(merged),
        "suggestions_created": len(suggestions_created),
        "errors": len(errors),
        "merged_items": merged[:100],
        "suggestions": suggestions_created[:100],
        "error_items": errors[:100],
    }


@router.get("/{contact_id}")
def get_contact(project_id: int, contact_id: int, db: Session = Depends(get_db)):
    """Get contact detail with identities."""
    user = db.query(MessagingUser).filter(
        MessagingUser.id == contact_id,
        MessagingUser.project_id == project_id
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Contact not found")

    identities = db.query(ContactIdentity).filter(
        ContactIdentity.user_id == contact_id,
        ContactIdentity.project_id == project_id
    ).all()

    result = MessagingUserResponse.model_validate(user).model_dump()
    result["identities"] = [ContactIdentityResponse.model_validate(i).model_dump() for i in identities]
    return result


@router.get("/{contact_id}/identities")
def get_contact_identities(project_id: int, contact_id: int, db: Session = Depends(get_db)):
    """List identities for a contact."""
    identities = db.query(ContactIdentity).filter(
        ContactIdentity.user_id == contact_id,
        ContactIdentity.project_id == project_id
    ).all()
    return [ContactIdentityResponse.model_validate(i) for i in identities]


@router.get("/{contact_id}/merge-history")
def get_merge_history(project_id: int, contact_id: int, db: Session = Depends(get_db)):
    """Get merge log history for a contact."""
    logs = db.query(ContactMergeLog).filter(
        ContactMergeLog.project_id == project_id,
        (ContactMergeLog.winner_id == contact_id) | (ContactMergeLog.loser_id == contact_id)
    ).order_by(ContactMergeLog.created_at.desc()).all()
    return [MergeLogResponse.model_validate(l) for l in logs]
