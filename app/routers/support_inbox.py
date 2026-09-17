"""
Support Inbox Router

Handles support ticket management, conversation takeover, and agent messaging.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel
import logging

from sqlalchemy import func as sa_func
from sqlalchemy.orm import joinedload
from app.database import get_db
from app.dependencies import require_project_role
from app.routers.auth import get_current_user
from app.models import SupportTicket, Project, ChatMessage, User as UserModel, WhatsAppInstance
from app.services.support_inbox_service import get_support_inbox_service
from app.services.unified_chat_service import get_unified_chat_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["support"])


# ============================================================================
# Pydantic Schemas
# ============================================================================

class TicketResponse(BaseModel):
    id: int
    project_id: int
    chatbot_id: Optional[int]
    session_id: int
    ticket_number: str
    status: str
    priority: str
    assigned_to_user_id: Optional[int]
    assigned_to_user_name: Optional[str] = None
    human_takeover: bool
    bot_can_resume: bool
    customer_identifier: str
    customer_name: Optional[str]
    customer_phone: Optional[str]
    channel: str
    contact_id: Optional[int] = None
    channel_instance_name: Optional[str] = None
    tags: Optional[List[str]]
    escalation_reason: Optional[str]
    escalation_origin: Optional[dict] = None
    ticket_metadata: Optional[dict] = None
    first_response_at: Optional[datetime]
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    sender_type: Optional[str]
    sent_by_user_id: Optional[int]
    sent_by_user_name: Optional[str] = None
    delivery_status: Optional[str]
    content_pieces: Optional[list] = None
    channel: Optional[str] = None
    message_metadata: Optional[dict] = None
    timestamp: datetime

    class Config:
        from_attributes = True


class TicketWithMessagesResponse(BaseModel):
    ticket: TicketResponse
    messages: List[MessageResponse]


class ContactTicketSummary(BaseModel):
    id: int
    ticket_number: str
    status: str
    channel: str
    created_at: datetime
    updated_at: datetime
    customer_name: Optional[str] = None
    message_count: int = 0

    class Config:
        from_attributes = True


class InboxResponse(BaseModel):
    tickets: List[TicketResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class InboxStatsResponse(BaseModel):
    total: int
    open: int
    in_progress: int
    waiting_customer: int
    resolved: int
    closed: int
    urgent: int
    unassigned: int


class CreateTicketRequest(BaseModel):
    session_id: int
    reason: Optional[str] = None
    priority: str = "medium"
    tags: Optional[List[str]] = None


class UpdateTicketRequest(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    tags: Optional[List[str]] = None
    internal_notes: Optional[str] = None
    bot_can_resume: Optional[bool] = None
    customer_phone: Optional[str] = None  # Phone number for WhatsApp replies


class AssignTicketRequest(BaseModel):
    user_id: int


class SendMessageRequest(BaseModel):
    content: str
    media_url: Optional[str] = None
    media_type: Optional[str] = None  # image, video, audio, document
    filename: Optional[str] = None
    channel: Optional[str] = None  # omnichannel: override the reply channel
    attachments: Optional[List[dict]] = None  # email only: [{url, filename}]


class CommentReplyRequest(BaseModel):
    mode: str  # public | private
    content: str


class BulkUpdateRequest(BaseModel):
    ticket_ids: List[int]
    status: str


class BlockContactRequest(BaseModel):
    identifier: str
    reason: Optional[str] = None


class UnblockContactRequest(BaseModel):
    identifier: str


class BlockedContactResponse(BaseModel):
    id: int
    project_id: int
    external_id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    blocked_at: Optional[datetime] = None
    blocked_reason: Optional[str] = None


class BlockedContactsListResponse(BaseModel):
    contacts: List[BlockedContactResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class AvailableChannelResponse(BaseModel):
    channel: str
    available: bool
    instance_id: Optional[int] = None
    session_window_open: Optional[bool] = None
    recipient: Optional[str] = None
    reason: Optional[str] = None


# ============================================================================
# Helpers
# ============================================================================

def _serialize_ticket(ticket: SupportTicket) -> TicketResponse:
    """Serialize a SupportTicket, including the assigned agent's name.

    Also enriches contact_id/customer_name/customer_phone from MessagingUser
    when missing (backfills old tickets created without enrichment).
    """
    from app.models.messaging import MessagingUser, ContactIdentity
    from sqlalchemy import or_
    from sqlalchemy.orm import object_session

    db = object_session(ticket)
    changed = False

    # Step 1: resolve contact_id if missing
    if not ticket.contact_id and ticket.customer_identifier and db:
        contact = None
        identifier = ticket.customer_identifier
        clean_id = identifier.split("@")[0].lstrip("+")  # strip @lid, @s.whatsapp.net, +

        # Strategy A: ContactIdentity channel lookup (most reliable)
        channel = ticket.channel or "whatsapp"
        ci = db.query(ContactIdentity).filter(
            ContactIdentity.project_id == ticket.project_id,
            ContactIdentity.identity_type == "channel",
            ContactIdentity.identity_value == f"{channel}:{identifier}",
        ).first()
        if ci:
            contact = db.query(MessagingUser).filter(
                MessagingUser.id == ci.user_id,
                MessagingUser.status == "active",
            ).first()

        # Strategy B: ContactIdentity phone lookup
        if not contact:
            pi = db.query(ContactIdentity).filter(
                ContactIdentity.project_id == ticket.project_id,
                ContactIdentity.identity_type == "phone",
                ContactIdentity.identity_value == clean_id,
            ).first()
            if pi:
                contact = db.query(MessagingUser).filter(
                    MessagingUser.id == pi.user_id,
                    MessagingUser.status == "active",
                ).first()

        # Strategy C: direct field match (external_id, phone, phone_e164, email)
        if not contact:
            contact = db.query(MessagingUser).filter(
                MessagingUser.project_id == ticket.project_id,
                MessagingUser.status == "active",
                or_(
                    MessagingUser.external_id == identifier,
                    MessagingUser.phone == identifier,
                    MessagingUser.phone_e164 == clean_id,
                    MessagingUser.email == identifier,
                    MessagingUser.external_id == clean_id,
                    MessagingUser.phone == clean_id,
                ),
            ).first()

        if contact:
            ticket.contact_id = contact.id
            changed = True

    # Step 2: enrich name/phone from linked MessagingUser
    if ticket.contact_id and (not ticket.customer_name or not ticket.customer_phone):
        contact = ticket.contact  # lazy-loaded relationship
        if contact:
            if not ticket.customer_name and contact.name:
                ticket.customer_name = contact.name
                changed = True
            if not ticket.customer_phone and contact.phone:
                ticket.customer_phone = contact.phone
                changed = True

    if changed and db:
        db.commit()

    data = TicketResponse.model_validate(ticket)
    if ticket.assigned_to_user_id and ticket.assigned_to:
        data.assigned_to_user_name = ticket.assigned_to.name

    # Resolve channel instance name from session metadata
    if db:
        try:
            session = ticket.session
            instance_id = (session.session_metadata or {}).get("inbound_instance_id") if session else None
            if instance_id:
                if ticket.channel == "whatsapp":
                    inst = db.query(WhatsAppInstance.instance_name).filter(
                        WhatsAppInstance.id == instance_id
                    ).first()
                elif ticket.channel == "email":
                    from app.models import CustomerSMTPConfig
                    inst = db.query(CustomerSMTPConfig.instance_name).filter(
                        CustomerSMTPConfig.id == instance_id
                    ).first()
                elif ticket.channel in ("messenger", "instagram", "messenger_comment", "instagram_comment"):
                    from app.models import MetaPageConnection
                    conn = db.query(MetaPageConnection).filter(
                        MetaPageConnection.id == instance_id
                    ).first()
                    if conn:
                        name = conn.page_name
                        if ticket.channel.startswith("instagram") and conn.ig_username:
                            name = f"@{conn.ig_username}"
                        inst = (name,) if name else None
                    else:
                        inst = None
                else:
                    inst = None
                if inst:
                    data.channel_instance_name = inst[0]
        except Exception:
            pass

    return data


# ============================================================================
# Routes
# ============================================================================

@router.get("/projects/{project_id}/support/inbox", response_model=InboxResponse)
async def get_inbox(
    project_id: int,
    status: Optional[str] = Query(None, description="Filter by status (open,in_progress,waiting_customer,resolved,closed)"),
    priority: Optional[str] = Query(None, description="Filter by priority (low,medium,high,urgent)"),
    channel: Optional[str] = Query(None, description="Filter by channel (web,whatsapp,sms)"),
    assigned_to_user_id: Optional[int] = Query(None, description="Filter by assigned user"),
    unassigned: Optional[bool] = Query(None, description="Filter unassigned tickets only"),
    chatbot_id: Optional[int] = Query(None, description="Filter by chatbot"),
    search: Optional[str] = Query(None, description="Search in ticket number or customer"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Get support inbox tickets with filters."""
    # Verify project access
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Build filters
    filters = {}
    if status:
        filters["status"] = status.split(",") if "," in status else status
    if priority:
        filters["priority"] = priority
    if channel:
        filters["channel"] = channel
    if assigned_to_user_id:
        filters["assigned_to_user_id"] = assigned_to_user_id
    if unassigned:
        filters["unassigned"] = unassigned
    if chatbot_id:
        filters["chatbot_id"] = chatbot_id
    if search:
        filters["search"] = search

    service = get_support_inbox_service(db)
    result = service.get_inbox(
        project_id=project_id,
        filters=filters,
        page=page,
        page_size=page_size
    )

    return InboxResponse(
        tickets=[_serialize_ticket(t) for t in result["tickets"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
        total_pages=result["total_pages"]
    )


@router.get("/projects/{project_id}/support/stats", response_model=InboxStatsResponse)
async def get_inbox_stats(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Get support inbox statistics."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = get_support_inbox_service(db)
    stats = service.get_inbox_stats(project_id)

    return InboxStatsResponse(**stats)


@router.post("/projects/{project_id}/support/tickets", response_model=TicketResponse)
async def create_ticket(
    project_id: int,
    request: CreateTicketRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Create a new support ticket."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = get_support_inbox_service(db)
    try:
        ticket = service.create_ticket(
            session_id=request.session_id,
            reason=request.reason,
            priority=request.priority,
            tags=request.tags
        )
        return _serialize_ticket(ticket)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/support/tickets/{ticket_id}", response_model=TicketWithMessagesResponse)
async def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Get a ticket with its messages."""
    service = get_support_inbox_service(db)
    try:
        result = service.get_ticket_with_messages(ticket_id)
        ticket = result["ticket"]
        messages_raw = result["messages"]

        # Bulk resolve user names for agent messages
        user_ids = {m.sent_by_user_id for m in messages_raw if m.sent_by_user_id}
        user_names = {}
        if user_ids:
            users = db.query(UserModel.id, UserModel.name).filter(UserModel.id.in_(user_ids)).all()
            user_names = {u.id: u.name for u in users}

        msg_responses = []
        for m in messages_raw:
            resp = MessageResponse.model_validate(m)
            if m.sent_by_user_id:
                resp.sent_by_user_name = user_names.get(m.sent_by_user_id)
            msg_responses.append(resp)

        return TicketWithMessagesResponse(
            ticket=_serialize_ticket(ticket),
            messages=msg_responses,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/support/tickets/{ticket_id}", response_model=TicketResponse)
async def update_ticket(
    ticket_id: int,
    request: UpdateTicketRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Update a ticket."""
    service = get_support_inbox_service(db)
    try:
        ticket = service.update_ticket(
            ticket_id=ticket_id,
            status=request.status,
            priority=request.priority,
            tags=request.tags,
            internal_notes=request.internal_notes,
            bot_can_resume=request.bot_can_resume
        )
        return _serialize_ticket(ticket)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/support/tickets/{ticket_id}/assign", response_model=TicketResponse)
async def assign_ticket(
    ticket_id: int,
    request: AssignTicketRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Assign a ticket to a user."""
    service = get_support_inbox_service(db)
    try:
        ticket = service.assign_ticket(
            ticket_id=ticket_id,
            user_id=request.user_id
        )
        return _serialize_ticket(ticket)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/support/tickets/{ticket_id}/takeover", response_model=TicketResponse)
async def takeover_conversation(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Take over a conversation as a human agent."""
    # Get user ID from current_user
    user_id = current_user.id if hasattr(current_user, 'id') else current_user.get('id')

    service = get_support_inbox_service(db)
    try:
        ticket = service.take_over_conversation(
            ticket_id=ticket_id,
            user_id=user_id
        )
        return _serialize_ticket(ticket)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/support/tickets/{ticket_id}/release", response_model=TicketResponse)
async def release_to_bot(
    ticket_id: int,
    resolve: bool = Query(True, description="Mark ticket as resolved"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Release conversation back to the bot."""
    service = get_support_inbox_service(db)
    try:
        ticket = service.release_to_bot(
            ticket_id=ticket_id,
            resolve=resolve
        )
        return _serialize_ticket(ticket)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/support/tickets/{ticket_id}/messages", response_model=MessageResponse)
async def send_agent_message(
    ticket_id: int,
    request: SendMessageRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Send a message as a support agent."""
    # Get user ID from current_user
    user_id = current_user.id if hasattr(current_user, 'id') else current_user.get('id')

    # Comment tickets have a dedicated public/private reply endpoint
    _ticket_ch = db.query(SupportTicket.channel).filter(SupportTicket.id == ticket_id).scalar()
    if _ticket_ch in ("messenger_comment", "instagram_comment"):
        raise HTTPException(
            status_code=400,
            detail="This is a comment ticket — use the comment-reply endpoint with mode 'public' or 'private'",
        )

    service = get_support_inbox_service(db)
    try:
        message = service.send_agent_message(
            ticket_id=ticket_id,
            user_id=user_id,
            content=request.content,
            media_url=request.media_url,
            media_type=request.media_type,
            filename=request.filename,
            attachments=request.attachments,
        )

        # Send message to customer via provider (reuse existing message to avoid duplicate)
        ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
        if ticket and ticket.session:
            chat_service = get_unified_chat_service(db)
            result = await chat_service.send_outgoing_message(
                session_id=ticket.session_id,
                content=request.content,
                sender_type="human_agent",
                sent_by_user_id=user_id,
                existing_message=message,
                media_url=request.media_url,
                media_type=request.media_type,
                filename=request.filename,
                channel_override=request.channel,
                attachments=request.attachments,
            )

            # Refresh message to get updated delivery_status from send_outgoing_message
            db.refresh(message)

            # Publish delivery status update via WebSocket
            service.publish_delivery_status(
                project_id=ticket.project_id,
                ticket_id=ticket.id,
                message_id=message.id,
                delivery_status=message.delivery_status,
                error=result.get("error") if not result.get("success") else None,
            )

        msg_response = MessageResponse.model_validate(message)
        agent_name = db.query(UserModel.name).filter(UserModel.id == user_id).scalar()
        msg_response.sent_by_user_name = agent_name
        return msg_response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _send_comment_reply(
    ticket: SupportTicket,
    request: CommentReplyRequest,
    user_id: int,
    db: Session,
) -> MessageResponse:
    if ticket.channel not in ("messenger_comment", "instagram_comment"):
        raise HTTPException(status_code=400, detail="Not a comment ticket")

    from app.services.meta_comment_service import MetaCommentService
    result = await MetaCommentService(db).reply_to_comment_ticket(
        ticket=ticket,
        mode=request.mode,
        content=request.content,
        sent_by_user_id=user_id,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Reply failed"))

    message = db.query(ChatMessage).filter(ChatMessage.id == result["message_id"]).first()
    msg_response = MessageResponse.model_validate(message)
    agent_name = db.query(UserModel.name).filter(UserModel.id == user_id).scalar()
    msg_response.sent_by_user_name = agent_name
    return msg_response


@router.post(
    "/projects/{project_id}/support/tickets/{ticket_id}/comment-reply",
    response_model=MessageResponse,
)
async def send_comment_reply(
    project_id: int,
    ticket_id: int,
    request: CommentReplyRequest,
    db: Session = Depends(get_db),
    membership=Depends(require_project_role("support_agent")),
):
    """Reply to a Facebook/Instagram comment ticket — publicly (comment under
    the post) or privately (one DM per comment thread, 7-day window)."""
    ticket = (
        db.query(SupportTicket)
        .filter(
            SupportTicket.id == ticket_id,
            SupportTicket.project_id == project_id,
        )
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    return await _send_comment_reply(ticket, request, membership["user_id"], db)


@router.post(
    "/support/tickets/{ticket_id}/comment-reply",
    response_model=MessageResponse,
    deprecated=True,
)
async def send_comment_reply_compat(
    ticket_id: int,
    request: CommentReplyRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """DEPRECATED — use the project-scoped route. Kept temporarily for
    external consumers; enforces the same project RBAC via the ticket."""
    from app.services.authorization_service import check_project_access, is_workspace_admin

    user_id = current_user.id if hasattr(current_user, 'id') else current_user.get('id')

    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    if not is_workspace_admin(db, current_user) and not check_project_access(
        db, user_id, ticket.project_id, "support_agent"
    ):
        raise HTTPException(
            status_code=403,
            detail="Requires at least 'support_agent' role on this project",
        )

    logger.warning(
        "DEPRECATED route POST /support/tickets/%s/comment-reply used by user %s — "
        "use /projects/{project_id}/support/tickets/{ticket_id}/comment-reply",
        ticket_id,
        user_id,
    )
    return await _send_comment_reply(ticket, request, user_id, db)


@router.get(
    "/projects/{project_id}/support/tickets/{ticket_id}/contact-tickets",
    response_model=List[ContactTicketSummary],
)
async def get_contact_tickets(
    project_id: int,
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get other tickets for the same contact (cross-channel history)."""
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Cross-channel: query by contact_id when available
    if ticket.contact_id:
        siblings = (
            db.query(
                SupportTicket,
                sa_func.count(ChatMessage.id).label("message_count"),
            )
            .outerjoin(ChatMessage, ChatMessage.session_id == SupportTicket.session_id)
            .filter(
                SupportTicket.contact_id == ticket.contact_id,
                SupportTicket.id != ticket.id,
            )
            .group_by(SupportTicket.id)
            .order_by(SupportTicket.created_at.desc())
            .limit(20)
            .all()
        )
    else:
        # Fallback: same customer_identifier (same-channel, for old tickets)
        siblings = (
            db.query(
                SupportTicket,
                sa_func.count(ChatMessage.id).label("message_count"),
            )
            .outerjoin(ChatMessage, ChatMessage.session_id == SupportTicket.session_id)
            .filter(
                SupportTicket.project_id == project_id,
                SupportTicket.customer_identifier == ticket.customer_identifier,
                SupportTicket.id != ticket.id,
            )
            .group_by(SupportTicket.id)
            .order_by(SupportTicket.created_at.desc())
            .limit(20)
            .all()
        )

    return [
        ContactTicketSummary(
            id=t.id,
            ticket_number=t.ticket_number,
            status=t.status,
            channel=t.channel,
            created_at=t.created_at,
            updated_at=t.updated_at,
            customer_name=t.customer_name,
            message_count=count,
        )
        for t, count in siblings
    ]


@router.get(
    "/projects/{project_id}/support/tickets/{ticket_id}/available-channels",
    response_model=List[AvailableChannelResponse],
)
async def get_available_channels(
    project_id: int,
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get available channels for replying to a contact (omnichannel).

    Uses the channel registry to determine which channels are configured
    for the project, then layers on contact-level checks (identity,
    session window) for each configured channel.
    """
    from app.models.messaging import MessagingUser, ContactIdentity
    from app.models import WhatsAppInstance, HandlerChannelLink
    from app.services.channels.channel_registry_service import ChannelRegistryService

    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # 1. Get project-level channel registry (source of truth for configured channels)
    registry_svc = ChannelRegistryService(db)
    registry = registry_svc.get_registry(project_id)
    configured_channels = {
        entry["channel"] for entry in registry if entry["available"]
    }

    channels = []

    # 2. Resolve contact identities
    contact = None
    if ticket.contact_id:
        contact = db.query(MessagingUser).filter(MessagingUser.id == ticket.contact_id).first()

    identities = {}
    if contact:
        if contact.phone:
            identities["whatsapp"] = contact.phone
            identities["sms"] = contact.phone
        if contact.email:
            identities["email"] = contact.email
        try:
            ci_records = db.query(ContactIdentity).filter(
                ContactIdentity.user_id == contact.id,
            ).all()
            for ci in ci_records:
                if ci.identity_type == "phone" and ci.identity_value:
                    identities.setdefault("whatsapp", ci.identity_value)
                    identities.setdefault("sms", ci.identity_value)
                elif ci.identity_type == "email" and ci.identity_value:
                    identities.setdefault("email", ci.identity_value)
                elif ci.identity_type == "channel" and ci.identity_value:
                    # Stored as "{channel}:{identifier}" (e.g. "messenger:PSID")
                    ch, _, ident = ci.identity_value.partition(":")
                    if ch in ("messenger", "instagram") and ident:
                        identities.setdefault(ch, ident)
        except Exception:
            pass
    else:
        if ticket.customer_phone:
            identities["whatsapp"] = ticket.customer_phone
            identities["sms"] = ticket.customer_phone
        elif ticket.channel == "whatsapp" and ticket.customer_identifier:
            identities["whatsapp"] = ticket.customer_identifier
        elif ticket.channel in ("messenger", "instagram") and ticket.customer_identifier:
            identities[ticket.channel] = ticket.customer_identifier

    # 3. Check each configured channel against contact-level availability

    # WhatsApp
    if "whatsapp" in configured_channels:
        wa_recipient = identities.get("whatsapp")
        wa_instance = None
        if wa_recipient:
            wa_link = db.query(HandlerChannelLink).filter(
                HandlerChannelLink.channel == "whatsapp",
            ).first()
            if wa_link:
                wa_instance = db.query(WhatsAppInstance).filter(
                    WhatsAppInstance.id == wa_link.instance_id,
                ).first()
            if not wa_instance:
                wa_instance = db.query(WhatsAppInstance).first()

            # Check 24h session window
            session_window_open = True
            try:
                from app.models import WhatsAppContactWindow
                window = db.query(WhatsAppContactWindow).filter(
                    WhatsAppContactWindow.contact_phone == wa_recipient,
                    WhatsAppContactWindow.instance_id == wa_instance.id if wa_instance else None,
                ).first()
                if window:
                    from datetime import datetime, timezone
                    session_window_open = window.window_end > datetime.now(timezone.utc) if window.window_end else False
                else:
                    session_window_open = False
            except Exception:
                pass

        channels.append(AvailableChannelResponse(
            channel="whatsapp",
            available=wa_instance is not None and wa_recipient is not None,
            instance_id=wa_instance.id if wa_instance else None,
            session_window_open=session_window_open if wa_recipient else None,
            recipient=wa_recipient,
            reason="no_recipient" if not wa_recipient else ("no_instance" if not wa_instance else None),
        ))

    # SMS
    if "sms" in configured_channels:
        sms_recipient = identities.get("sms")
        channels.append(AvailableChannelResponse(
            channel="sms",
            available=sms_recipient is not None,
            recipient=sms_recipient,
            reason="no_recipient" if not sms_recipient else None,
        ))

    # Email
    if "email" in configured_channels:
        email_recipient = identities.get("email")
        channels.append(AvailableChannelResponse(
            channel="email",
            available=email_recipient is not None,
            recipient=email_recipient,
            reason="no_recipient" if not email_recipient else None,
        ))

    # Web
    if "web" in configured_channels:
        channels.append(AvailableChannelResponse(
            channel="web",
            available=True,
        ))

    # Messenger / Instagram (PSID/IGSID identity + 24h window per connection)
    for meta_channel in ("messenger", "instagram"):
        if meta_channel not in configured_channels:
            continue
        recipient = identities.get(meta_channel)
        connection = None
        session_window_open = None
        if recipient:
            from app.models import MetaPageConnection
            from app.services.meta_window_service import MetaWindowService

            # Prefer the connection the ticket's session came in on
            session = ticket.session
            inbound_instance_id = (
                (session.session_metadata or {}).get("inbound_instance_id")
                if session and ticket.channel == meta_channel else None
            )
            platform_filter = (
                MetaPageConnection.messenger_enabled == True
                if meta_channel == "messenger"
                else MetaPageConnection.instagram_enabled == True
            )
            q = db.query(MetaPageConnection).filter(
                MetaPageConnection.project_id == project_id,
                MetaPageConnection.is_active == True,
                platform_filter,
            )
            if inbound_instance_id:
                connection = q.filter(MetaPageConnection.id == inbound_instance_id).first()
            if not connection:
                connection = q.first()
            if connection:
                session_window_open = MetaWindowService(db).is_window_open(
                    connection.id, meta_channel, recipient
                )

        channels.append(AvailableChannelResponse(
            channel=meta_channel,
            available=connection is not None and recipient is not None,
            instance_id=connection.id if connection else None,
            session_window_open=session_window_open,
            recipient=recipient,
            reason="no_recipient" if not recipient else ("no_instance" if not connection else None),
        ))

    return channels


# ============================================================================
# Bulk Operations
# ============================================================================

@router.post("/projects/{project_id}/support/tickets/bulk-update")
async def bulk_update_tickets(
    project_id: int,
    body: BulkUpdateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Bulk update status for multiple tickets."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    valid_statuses = {"open", "in_progress", "waiting_customer", "resolved", "closed"}
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status: {body.status}")

    service = get_support_inbox_service(db)
    count = service.bulk_update_status(project_id, body.ticket_ids, body.status)
    return {"updated": count}


# ============================================================================
# Block / Unblock Contacts
# ============================================================================

@router.post("/projects/{project_id}/support/contacts/block")
async def block_contact(
    project_id: int,
    body: BlockContactRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Block a contact — silently drops all future inbound messages."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = get_support_inbox_service(db)
    try:
        result = service.block_contact(project_id, body.identifier, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result


@router.post("/projects/{project_id}/support/contacts/unblock")
async def unblock_contact(
    project_id: int,
    body: UnblockContactRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Unblock a previously blocked contact."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = get_support_inbox_service(db)
    try:
        result = service.unblock_contact(project_id, body.identifier)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result


@router.get(
    "/projects/{project_id}/support/contacts/blocked",
    response_model=BlockedContactsListResponse,
)
async def get_blocked_contacts(
    project_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all blocked contacts for a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = get_support_inbox_service(db)
    return service.get_blocked_contacts(project_id, page, page_size, search)


# ── Start Conversation (agent-initiated outbound) ───────────────────────


class StartConversationRequest(BaseModel):
    contact_id: int
    channel: str  # whatsapp | sms | email
    mode: str  # template | text | html
    template_id: Optional[int] = None
    whatsapp_instance_id: Optional[int] = None
    email_instance_id: Optional[int] = None
    smtp_config_id: Optional[int] = None
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    reply_to: Optional[str] = None
    variable_mapping: Optional[dict] = None
    body: Optional[str] = None
    subject: Optional[str] = None
    priority: str = "medium"
    reason: Optional[str] = None
    attachments: Optional[List[dict]] = None  # email only: [{url, filename}]


class StartConversationResponse(BaseModel):
    ticket_id: int
    session_id: int
    send_log_id: Optional[int] = None
    status: str
    error: Optional[str] = None


@router.post(
    "/projects/{project_id}/support/tickets/start-conversation",
    response_model=StartConversationResponse,
)
async def start_conversation(
    project_id: int,
    request: StartConversationRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Agent-initiated outbound — creates ticket + sends first message in one call."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = get_support_inbox_service(db)
    try:
        result = await service.start_conversation(
            project_id=project_id,
            agent_user_id=current_user.id if hasattr(current_user, "id") else current_user["id"],
            contact_id=request.contact_id,
            channel=request.channel,
            mode=request.mode,
            template_id=request.template_id,
            body=request.body,
            subject=request.subject,
            whatsapp_instance_id=request.whatsapp_instance_id,
            email_instance_id=request.email_instance_id,
            smtp_config_id=request.smtp_config_id,
            from_email=request.from_email,
            from_name=request.from_name,
            reply_to=request.reply_to,
            variable_mapping=request.variable_mapping,
            priority=request.priority,
            reason=request.reason,
            attachments=request.attachments,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return StartConversationResponse(**result)


@router.get("/projects/{project_id}/support/contacts/{contact_id}/reachable-channels")
async def reachable_channels(
    project_id: int,
    contact_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Report which of whatsapp/sms/email can reach this contact right now."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = get_support_inbox_service(db)
    try:
        return service.reachable_channels(project_id, contact_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
