"""
Messaging Templates Router
Manages message templates with variable substitution
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models import Project, User, WhatsAppInstance
from app.models.messaging import MessagingTemplate, MessagingChannel, MessagingLog, MessageStatus, ChannelType
from app.schemas.messaging import (
    MessagingTemplateCreate,
    MessagingTemplateUpdate,
    MessagingTemplateResponse,
    MessagingTemplatePreviewRequest,
    MessagingTemplatePreviewResponse,
    SendTestRequest,
    SendTestResponse,
    TemplateFolderResponse,
    TemplateLintRequest,
)
from app.schemas.orchestration_attention import AttentionActivationApproval
from app.routers.auth import get_current_user
from app.services.messaging import template_renderer, event_processor
from app.services.email_service import EmailService
from app.services.evolution_api_service import evolution_api_service
from app.services.whatsapp_sender import WhatsAppSender, infer_media_type

router = APIRouter(prefix="/projects/{project_id}/messaging/templates", tags=["messaging-templates"])


def get_project_or_404(db: Session, project_id: int, workspace_id: int) -> Project:
    """Get project and verify workspace access"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/", response_model=List[MessagingTemplateResponse])
def list_templates(
    project_id: int,
    folder: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all messaging templates for a project, optionally filtered by folder"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(MessagingTemplate).filter(
        MessagingTemplate.project_id == project_id
    )

    if folder is not None:
        if folder == "__uncategorized__":
            query = query.filter(MessagingTemplate.folder.is_(None))
        else:
            query = query.filter(MessagingTemplate.folder == folder)

    templates = query.order_by(MessagingTemplate.created_at.desc()).all()

    return templates


@router.post("/", response_model=MessagingTemplateResponse, status_code=status.HTTP_201_CREATED)
def create_template(
    project_id: int,
    data: MessagingTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new messaging template"""
    project = get_project_or_404(db, project_id, current_user.workspace_id)

    # i18n: a template is a family keyed by (slug, locale). Default to the project
    # locale so every row carries one. Uniqueness is per (slug, locale).
    locale = data.locale or project.default_locale
    existing = db.query(MessagingTemplate).filter(
        MessagingTemplate.project_id == project_id,
        MessagingTemplate.slug == data.slug,
        MessagingTemplate.locale == locale,
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="A template with this slug already exists for this locale"
        )

    # Validate channel_id if provided
    if data.channel_id:
        channel = db.query(MessagingChannel).filter(
            MessagingChannel.id == data.channel_id,
            MessagingChannel.project_id == project_id
        ).first()
        if not channel:
            raise HTTPException(status_code=400, detail="Invalid channel_id")

    template = MessagingTemplate(
        project_id=project_id,
        channel_id=data.channel_id,
        channel_type=data.channel_type or ChannelType.email,
        from_email=data.from_email,
        from_name=data.from_name,
        reply_to=data.reply_to,
        folder=data.folder,
        body_format=data.body_format or "html",
        media_url=data.media_url,
        slug=data.slug,
        name=data.name,
        subject=data.subject,
        body=data.body,
        template_metadata=data.metadata,
        trigger_events=data.trigger_events,
        automation_enabled=False,
        purpose_key=data.purpose_key,
        attention_policy=(
            data.attention_policy.model_dump(mode="json")
            if data.attention_policy else None
        ),
        locale=locale,
        source_locale=data.source_locale or locale,
        translation_status=data.translation_status or "source",
    )

    db.add(template)
    db.commit()
    db.refresh(template)

    return template


@router.get("/meta-preview")
async def preview_meta_templates(
    project_id: int,
    whatsapp_instance_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch Meta-approved templates for preview, without persisting anything."""
    project = get_project_or_404(db, project_id, current_user.workspace_id)

    # Instances may be scoped to the workspace (project_id NULL) or to a specific
    # project. Accept either — mirrors how the funnel/support dialogs list them.
    instance = db.query(WhatsAppInstance).filter(
        WhatsAppInstance.id == whatsapp_instance_id,
        WhatsAppInstance.workspace_id == project.workspace_id,
    ).first()
    if not instance:
        raise HTTPException(status_code=404, detail="WhatsApp instance not found")
    if instance.provider_type != "meta_cloud_api":
        raise HTTPException(status_code=400, detail="Instance is not a Meta Cloud API provider")

    from app.services.meta_template_importer import fetch_meta_templates
    try:
        rows = await fetch_meta_templates(instance)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=502, detail=str(e))

    already_imported = {
        (t.meta_template_name, t.meta_language)
        for t in db.query(MessagingTemplate).filter(
            MessagingTemplate.whatsapp_instance_id == instance.id,
            MessagingTemplate.external_source == "meta_cloud",
        ).all()
    }

    return [
        {
            "name": r.get("name"),
            "language": r.get("language") or "",
            "status": (r.get("status") or "").upper(),
            "category": r.get("category") or "",
            "components": r.get("components") or [],
            "already_imported": (r.get("name"), r.get("language") or "") in already_imported,
        }
        for r in rows
    ]


class MetaTemplateImportRequest(BaseModel):
    whatsapp_instance_id: int
    selected: Optional[List[dict]] = None  # [{"name": ..., "language": ...}]


@router.post("/import-from-meta")
async def import_from_meta(
    project_id: int,
    body: MetaTemplateImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Import Meta-approved templates into MessagingTemplate (upsert, idempotent)."""
    project = get_project_or_404(db, project_id, current_user.workspace_id)

    instance = db.query(WhatsAppInstance).filter(
        WhatsAppInstance.id == body.whatsapp_instance_id,
        WhatsAppInstance.workspace_id == project.workspace_id,
    ).first()
    if not instance:
        raise HTTPException(status_code=404, detail="WhatsApp instance not found")
    if instance.provider_type != "meta_cloud_api":
        raise HTTPException(status_code=400, detail="Instance is not a Meta Cloud API provider")

    from app.services.meta_template_importer import fetch_meta_templates, import_templates
    try:
        rows = await fetch_meta_templates(instance)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=502, detail=str(e))

    selected_keys = None
    if body.selected is not None:
        selected_keys = [(s.get("name", ""), s.get("language", "")) for s in body.selected]

    counts = import_templates(
        db=db,
        instance=instance,
        template_rows=rows,
        selected_keys=selected_keys,
        only_approved=True,
        project_id=project_id,
    )
    return counts


@router.get("/folders", response_model=List[TemplateFolderResponse])
def list_folders(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List distinct template folders with counts"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    rows = (
        db.query(MessagingTemplate.folder, sa_func.count(MessagingTemplate.id))
        .filter(
            MessagingTemplate.project_id == project_id,
            MessagingTemplate.folder.isnot(None),
        )
        .group_by(MessagingTemplate.folder)
        .order_by(MessagingTemplate.folder)
        .all()
    )

    return [TemplateFolderResponse(folder=folder, count=count) for folder, count in rows]


@router.put("/folders/{folder_name}")
def rename_folder(
    project_id: int,
    folder_name: str,
    new_name: str = Query(..., max_length=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Rename a folder (bulk update all templates in the folder)"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    updated = (
        db.query(MessagingTemplate)
        .filter(
            MessagingTemplate.project_id == project_id,
            MessagingTemplate.folder == folder_name,
        )
        .update({MessagingTemplate.folder: new_name}, synchronize_session="fetch")
    )

    if updated == 0:
        raise HTTPException(status_code=404, detail="Folder not found")

    db.commit()
    return {"updated": updated, "folder": new_name}


@router.delete("/folders/{folder_name}")
def delete_folder(
    project_id: int,
    folder_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a folder (set templates to uncategorized)"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    updated = (
        db.query(MessagingTemplate)
        .filter(
            MessagingTemplate.project_id == project_id,
            MessagingTemplate.folder == folder_name,
        )
        .update({MessagingTemplate.folder: None}, synchronize_session="fetch")
    )

    if updated == 0:
        raise HTTPException(status_code=404, detail="Folder not found")

    db.commit()
    return {"updated": updated}


@router.get("/{template_id}", response_model=MessagingTemplateResponse)
def get_template(
    project_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific messaging template"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == template_id,
        MessagingTemplate.project_id == project_id
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    return template


@router.put("/{template_id}", response_model=MessagingTemplateResponse)
def update_template(
    project_id: int,
    template_id: int,
    data: MessagingTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a messaging template"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == template_id,
        MessagingTemplate.project_id == project_id
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    changes = data.model_dump(exclude_unset=True)
    if data.automation_enabled is True:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "dedicated_activation_required",
                "message": "Use attention-impact then activate-automation; generic update cannot enable an automation.",
            },
        )
    if template.automation_enabled and changes != {"automation_enabled": False}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "active_automation_immutable",
                "message": "Disable template automation before changing its definition or content.",
            },
        )

    # Update fields
    if data.name is not None:
        template.name = data.name

    if data.channel_id is not None:
        if data.channel_id:
            channel = db.query(MessagingChannel).filter(
                MessagingChannel.id == data.channel_id,
                MessagingChannel.project_id == project_id
            ).first()
            if not channel:
                raise HTTPException(status_code=400, detail="Invalid channel_id")
        template.channel_id = data.channel_id

    if data.subject is not None:
        template.subject = data.subject

    if data.body is not None:
        template.body = data.body

    if data.metadata is not None:
        template.template_metadata = data.metadata

    if data.trigger_events is not None:
        template.trigger_events = data.trigger_events

    if data.purpose_key is not None:
        template.purpose_key = data.purpose_key

    if data.attention_policy is not None:
        template.attention_policy = data.attention_policy.model_dump(mode="json")

    if data.channel_type is not None:
        template.channel_type = data.channel_type

    if data.from_email is not None:
        template.from_email = data.from_email if data.from_email else None

    if data.from_name is not None:
        template.from_name = data.from_name if data.from_name else None

    if data.reply_to is not None:
        template.reply_to = data.reply_to if data.reply_to else None

    if data.folder is not None:
        template.folder = data.folder if data.folder else None

    if data.body_format is not None:
        template.body_format = data.body_format

    if data.media_url is not None:
        template.media_url = data.media_url if data.media_url else None

    if data.is_active is not None:
        template.is_active = data.is_active

    if data.automation_enabled is False:
        template.automation_enabled = False

    # i18n: locale is fixed at create; allow status/source updates (e.g. review→publish)
    if data.translation_status is not None:
        template.translation_status = data.translation_status
    if data.source_locale is not None:
        template.source_locale = data.source_locale

    db.commit()
    db.refresh(template)

    return template


@router.get("/{template_id}/attention-impact")
def get_template_automation_impact(
    project_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Preview this event template against every active automation."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == template_id,
        MessagingTemplate.project_id == project_id,
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    from app.services.orchestration_impact_service import OrchestrationImpactService

    return OrchestrationImpactService(db).preview("template", template)


@router.post("/{template_id}/activate-automation", response_model=MessagingTemplateResponse)
def activate_template_automation(
    project_id: int,
    template_id: int,
    approval: AttentionActivationApproval,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Enable event-driven execution after exact impact approval."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == template_id,
        MessagingTemplate.project_id == project_id,
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    from app.services.orchestration_impact_service import (
        OrchestrationImpactError,
        OrchestrationImpactService,
    )

    try:
        OrchestrationImpactService(db).ensure_approved(
            "template", template, approval.impact_fingerprint,
        )
    except OrchestrationImpactError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "orchestration_impact_blocked", "impact": exc.report},
        ) from exc
    template.automation_enabled = True
    db.commit()
    db.refresh(template)
    return template


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    project_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a messaging template"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == template_id,
        MessagingTemplate.project_id == project_id
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if template.automation_enabled:
        raise HTTPException(
            status_code=409,
            detail="Disable template automation before deleting the template",
        )

    db.delete(template)
    db.commit()


@router.post("/lint")
def lint_template_body(
    project_id: int,
    data: TemplateLintRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Localization lint for arbitrary body/subject (in-editor live check)."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.services.messaging.localization_lint import lint_template
    return lint_template(data.body, data.subject)


@router.get("/{template_id}/lint")
def lint_existing_template(
    project_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Localization lint of an existing template (e.g. before duplicating to a new locale)."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == template_id,
        MessagingTemplate.project_id == project_id,
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    from app.services.messaging.localization_lint import lint_template
    return lint_template(template.body, template.subject)


@router.post("/{template_id}/preview", response_model=MessagingTemplatePreviewResponse)
def preview_template(
    project_id: int,
    template_id: int,
    data: MessagingTemplatePreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Preview a template with sample or provided variables"""
    project = get_project_or_404(db, project_id, current_user.workspace_id)

    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == template_id,
        MessagingTemplate.project_id == project_id
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Inject project variables
    variables = dict(data.variables) if data.variables else {}
    try:
        from app.services.project_variable_service import ProjectVariableService
        proj_vars = ProjectVariableService(db).render_project_variables(project_id, variables)
        variables["project"] = proj_vars
        variables["projects"] = proj_vars
    except Exception:
        pass

    # i18n: preview this variant in its own locale (drives |money/|date/|number)
    preview_locale = template.locale or project.default_locale
    rendered_body, rendered_subject, variables_used, missing_variables = template_renderer.render_template(
        template.body,
        variables,
        template.subject,
        locale=preview_locale,
        timezone=project.default_timezone,
    )

    return MessagingTemplatePreviewResponse(
        rendered_subject=rendered_subject,
        rendered_body=rendered_body,
        variables_used=variables_used,
        missing_variables=missing_variables
    )


@router.get("/{template_id}/variables")
def get_template_variables(
    project_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get list of variables extracted from template body and subject.
    Returns unique variable names found in {{variable}} placeholders.
    """
    get_project_or_404(db, project_id, current_user.workspace_id)

    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == template_id,
        MessagingTemplate.project_id == project_id
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Extract variables from body and subject
    variables = template_renderer.extract_variables(template.body, template.subject)

    return {
        "template_id": template_id,
        "variables": variables
    }


@router.get("/{template_id}/analytics/summary")
def template_analytics_summary(
    project_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.services.template_analytics_service import TemplateAnalyticsService
    svc = TemplateAnalyticsService(db)
    return svc.engagement_summary(template_id, current_user.workspace_id)


@router.get("/{template_id}/analytics/trend")
def template_analytics_trend(
    project_id: int,
    template_id: int,
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.services.template_analytics_service import TemplateAnalyticsService
    svc = TemplateAnalyticsService(db)
    return svc.engagement_trend(template_id, current_user.workspace_id, days=days)


@router.get("/{template_id}/analytics/top-links")
def template_analytics_top_links(
    project_id: int,
    template_id: int,
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.services.template_analytics_service import TemplateAnalyticsService
    svc = TemplateAnalyticsService(db)
    return svc.top_links(template_id, current_user.workspace_id, limit=limit)


@router.post("/send-test", response_model=SendTestResponse)
async def send_test_message(
    project_id: int,
    data: SendTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Send a test message using a template"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    # Get template
    template = db.query(MessagingTemplate).filter(
        MessagingTemplate.id == data.template_id,
        MessagingTemplate.project_id == project_id
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Render template
    variables = dict(data.variables) if data.variables else {}
    try:
        from app.services.project_variable_service import ProjectVariableService
        proj_vars = ProjectVariableService(db).render_project_variables(project_id, variables)
        variables["project"] = proj_vars
        variables["projects"] = proj_vars
    except Exception:
        pass
    rendered_body, rendered_subject, _, missing = template_renderer.render_template(
        template.body,
        variables,
        template.subject
    )

    # Determine channel type from template (default to email)
    tpl_channel_type = (template.channel_type.value if template.channel_type else "email")

    # --- SMS: send via Twilio ---
    if tpl_channel_type == "sms":
        from app.services.channels.base import OutboundContent
        from app.services.channels.send_service import SendService
        content = OutboundContent(content_type="text", text=rendered_body)
        svc = SendService(db)
        result = await svc.send(
            project_id=project_id,
            user_id=0,
            recipient=request.recipient,
            content=content,
            channel="sms",
            source_type="test_send",
            source_id=template.id,
        )
        return SendTestResponse(
            success=result.success,
            message="SMS test sent successfully" if result.success else (result.error or "Failed to send SMS"),
            rendered_body=rendered_body,
        )

    if tpl_channel_type == "push":
        raise HTTPException(status_code=501, detail="Push notification sending is not yet implemented")

    if tpl_channel_type == "inapp":
        raise HTTPException(status_code=501, detail="In-app notification sending is not yet implemented")

    # --- WhatsApp: send via Evolution API ---
    if tpl_channel_type == "whatsapp":
        instance = db.query(WhatsAppInstance).filter(
            WhatsAppInstance.workspace_id == current_user.workspace_id,
            WhatsAppInstance.is_active == True,
            WhatsAppInstance.connection_status.in_(["open", "connected"])
        ).first()

        if not instance:
            return SendTestResponse(
                success=False,
                message="No connected WhatsApp instance found. Please connect a WhatsApp instance first.",
                rendered_body=rendered_body
            )

        sender = WhatsAppSender(db)
        result = await sender.send_message(
            instance=instance,
            to_number=data.recipient,
            message=rendered_body,
        )

        # Send media attachment if template has media_url
        if result.get("success") and template.media_url:
            m_type = infer_media_type(template.media_url)
            await sender.send_media_message(
                instance=instance,
                to_number=data.recipient,
                media_url=template.media_url,
                media_type=m_type,
            )

        from app.services.channels.send_log_helper import record_direct_send
        record_direct_send(
            db=db, project_id=project_id, channel="whatsapp",
            recipient=data.recipient, content_summary=rendered_body[:500] if rendered_body else "",
            source_type="template_test", source_id=template.id,
            template_id=template.id, instance_id=instance.id,
            status="sent" if result.get("success") else "failed",
            provider_message_id=result.get("message_id"),
            error_message=result.get("error"),
            content_payload={"text": rendered_body, "content_type": "text"},
        )
        db.commit()

        return SendTestResponse(
            success=result.get("success", False),
            message="WhatsApp test message sent successfully" if result.get("success") else f"Failed to send: {result.get('error', 'Unknown error')}",
            rendered_body=rendered_body
        )

    # --- Email: route through SendService for pixel + tracking ---
    from app.services.channels.base import OutboundContent
    from app.services.channels.send_service import SendService

    resolved_from = data.from_email or template.from_email or None
    resolved_name = data.from_name or template.from_name or None
    resolved_reply_to = data.reply_to or template.reply_to or None

    # Support multiple recipients (comma-separated)
    recipients = [r.strip() for r in data.recipient.split(",") if r.strip()]
    if not recipients:
        raise HTTPException(status_code=400, detail="No valid recipients provided")

    is_html = template.body_format != "plain" and "<" in (rendered_body or "")
    content = OutboundContent(
        content_type="rich" if is_html else "text",
        text=rendered_body,
        html=rendered_body if is_html else None,
        subject=rendered_subject or template.name,
    )

    instance_cfg = {}
    if resolved_from:
        instance_cfg["from_email"] = resolved_from
    if resolved_name:
        instance_cfg["from_name"] = resolved_name
    if resolved_reply_to:
        instance_cfg["reply_to"] = resolved_reply_to
    if data.smtp_config_id:
        instance_cfg["send_via"] = str(data.smtp_config_id)

    svc = SendService(db)
    results = []
    all_success = True

    for rcpt in recipients:
        try:
            decision = await svc.send(
                project_id=project_id,
                user_id=None,
                recipient=rcpt,
                content=content,
                channel="email",
                source_type="template_test",
                source_id=template.id,
                template_id=template.id,
                instance_config=instance_cfg or None,
                skip_policy=True,
            )
            results.append({"recipient": rcpt, "success": decision.success, "send_log_id": decision.send_log_id})
            if not decision.success:
                all_success = False
        except Exception as e:
            results.append({"recipient": rcpt, "success": False, "error": str(e)})
            all_success = False

    db.commit()

    if len(recipients) == 1:
        r = results[0]
        msg = "Test email sent successfully" if r["success"] else f"Failed: {r.get('error', 'Send failed')}"
    else:
        ok = sum(1 for r in results if r["success"])
        msg = f"Sent to {ok}/{len(recipients)} recipients"

    return SendTestResponse(
        success=all_success,
        message=msg,
        rendered_subject=rendered_subject,
        rendered_body=rendered_body,
    )
