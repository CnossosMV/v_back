"""
Chat Widget Router

Handles embeddable chat widget configuration and public widget API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel
import secrets
import time

from app.database import get_db
from app.routers.auth import get_current_user
from app.dependencies import require_project_role
from app.models import ChatWidgetConfig, Chatbot, ChatSession, ChatMessage, ConversationFeedback
from app.services.unified_chat_service import get_unified_chat_service
from app.services.chatbot.conversation_manager import ConversationManager
from app.services.handler_channel_link_service import HandlerChannelLinkService

router = APIRouter(tags=["chat-widget"])


# ============================================================================
# Rate Limiting (Simple in-memory)
# ============================================================================

_rate_limit_cache: Dict[str, List[float]] = {}


def check_rate_limit(key: str, limit: int, window: int = 60) -> bool:
    """
    Simple rate limiting check.

    Args:
        key: Rate limit key (widget_key + IP)
        limit: Max requests per window
        window: Time window in seconds

    Returns:
        True if within limit, False if exceeded
    """
    now = time.time()
    if key not in _rate_limit_cache:
        _rate_limit_cache[key] = []

    # Clean old entries
    _rate_limit_cache[key] = [t for t in _rate_limit_cache[key] if now - t < window]

    if len(_rate_limit_cache[key]) >= limit:
        return False

    _rate_limit_cache[key].append(now)
    return True


# ============================================================================
# Pydantic Schemas
# ============================================================================

class WidgetTheme(BaseModel):
    primary_color: str = "#007bff"
    secondary_color: str = "#6c757d"
    background_color: str = "#ffffff"
    text_color: str = "#212529"
    position: str = "bottom-right"  # bottom-right, bottom-left
    size: str = "medium"  # small, medium, large
    border_radius: int = 16


class WidgetConfigResponse(BaseModel):
    id: int
    project_id: int
    chatbot_id: Optional[int] = None
    handler_type: Optional[str] = None
    handler_id: Optional[int] = None
    widget_key: str
    theme: Optional[Dict[str, Any]]
    welcome_message: Optional[str]
    placeholder_text: Optional[str]
    bot_name: Optional[str]
    bot_avatar_url: Optional[str]
    enable_webchat: bool
    enable_whatsapp_button: bool
    whatsapp_number: Optional[str]
    enable_file_upload: bool
    enable_feedback: bool
    enable_sound: bool
    enable_typing_indicator: bool
    allowed_domains: Optional[List[str]]
    rate_limit_per_minute: int
    require_email: bool
    auto_open_delay: Optional[int]
    greeting_delay: Optional[int]
    offline_message: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WidgetConfigCreateRequest(BaseModel):
    theme: Optional[WidgetTheme] = None
    welcome_message: Optional[str] = "Hi! How can I help you today?"
    placeholder_text: Optional[str] = "Type a message..."
    bot_name: Optional[str] = None
    bot_avatar_url: Optional[str] = None
    enable_webchat: bool = True
    enable_whatsapp_button: bool = False
    whatsapp_number: Optional[str] = None
    enable_file_upload: bool = False
    enable_feedback: bool = True
    enable_sound: bool = True
    enable_typing_indicator: bool = True
    allowed_domains: Optional[List[str]] = None
    rate_limit_per_minute: int = 20
    require_email: bool = False
    auto_open_delay: Optional[int] = None
    greeting_delay: int = 1000
    offline_message: Optional[str] = None
    custom_css: Optional[str] = None


class WidgetConfigUpdateRequest(BaseModel):
    theme: Optional[Dict[str, Any]] = None
    welcome_message: Optional[str] = None
    placeholder_text: Optional[str] = None
    bot_name: Optional[str] = None
    bot_avatar_url: Optional[str] = None
    enable_webchat: Optional[bool] = None
    enable_whatsapp_button: Optional[bool] = None
    whatsapp_number: Optional[str] = None
    enable_file_upload: Optional[bool] = None
    enable_feedback: Optional[bool] = None
    enable_sound: Optional[bool] = None
    enable_typing_indicator: Optional[bool] = None
    allowed_domains: Optional[List[str]] = None
    rate_limit_per_minute: Optional[int] = None
    require_email: Optional[bool] = None
    auto_open_delay: Optional[int] = None
    greeting_delay: Optional[int] = None
    offline_message: Optional[str] = None
    custom_css: Optional[str] = None
    is_active: Optional[bool] = None


# Public API Schemas
class PublicWidgetConfigResponse(BaseModel):
    widget_key: str
    theme: Optional[Dict[str, Any]]
    welcome_message: Optional[str]
    placeholder_text: Optional[str]
    bot_name: Optional[str]
    bot_avatar_url: Optional[str]
    enable_webchat: bool
    enable_whatsapp_button: bool
    whatsapp_number: Optional[str]
    enable_file_upload: bool
    enable_feedback: bool
    enable_sound: bool
    enable_typing_indicator: bool
    require_email: bool
    auto_open_delay: Optional[int]
    greeting_delay: Optional[int]
    offline_message: Optional[str]


class PublicChatRequest(BaseModel):
    message: str
    session_id: Optional[int] = None
    user_identifier: Optional[str] = None
    email: Optional[str] = None


class PublicChatResponse(BaseModel):
    message: str
    session_id: int
    timestamp: datetime


class PublicSessionRequest(BaseModel):
    user_identifier: Optional[str] = None
    email: Optional[str] = None


class PublicSessionResponse(BaseModel):
    session_id: int
    welcome_message: Optional[str]


class PublicFeedbackRequest(BaseModel):
    session_id: int
    message_id: Optional[int] = None
    rating: Optional[str] = None  # thumbs_up, thumbs_down
    rating_score: Optional[int] = None  # 1-5
    feedback_text: Optional[str] = None


# ============================================================================
# Admin Routes (Authenticated)
# ============================================================================

@router.get("/chatbots/{chatbot_id}/widget", response_model=WidgetConfigResponse)
async def get_widget_config(
    chatbot_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Get widget configuration for a chatbot."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    config = db.query(ChatWidgetConfig).filter(
        ChatWidgetConfig.chatbot_id == chatbot_id
    ).first()

    if not config:
        # Create default config
        config = ChatWidgetConfig(
            project_id=chatbot.project_id,
            chatbot_id=chatbot_id,
            handler_type="chatbot",
            handler_id=chatbot_id,
            widget_key=f"wk_{secrets.token_urlsafe(16)}",
            theme={"primary_color": "#007bff", "position": "bottom-right"},
            welcome_message="Hi! How can I help you today?",
            placeholder_text="Type a message...",
            bot_name=chatbot.name,
            enable_webchat=True,
            enable_feedback=True,
            enable_sound=True,
            enable_typing_indicator=True,
            rate_limit_per_minute=20,
            is_active=True
        )
        db.add(config)
        db.commit()
        db.refresh(config)

    return WidgetConfigResponse.model_validate(config)


@router.put("/chatbots/{chatbot_id}/widget", response_model=WidgetConfigResponse)
async def update_widget_config(
    chatbot_id: int,
    request: WidgetConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Update widget configuration for a chatbot."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    config = db.query(ChatWidgetConfig).filter(
        ChatWidgetConfig.chatbot_id == chatbot_id
    ).first()

    if not config:
        # Create new config
        config = ChatWidgetConfig(
            project_id=chatbot.project_id,
            chatbot_id=chatbot_id,
            handler_type="chatbot",
            handler_id=chatbot_id,
            widget_key=f"wk_{secrets.token_urlsafe(16)}"
        )
        db.add(config)

    # Update fields
    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if value is not None:
            setattr(config, field, value)

    db.commit()
    db.refresh(config)

    return WidgetConfigResponse.model_validate(config)


@router.post("/chatbots/{chatbot_id}/widget/regenerate-key", response_model=WidgetConfigResponse)
async def regenerate_widget_key(
    chatbot_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Regenerate widget key for a chatbot."""
    config = db.query(ChatWidgetConfig).filter(
        ChatWidgetConfig.chatbot_id == chatbot_id
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Widget config not found")

    config.widget_key = f"wk_{secrets.token_urlsafe(16)}"
    db.commit()
    db.refresh(config)

    return WidgetConfigResponse.model_validate(config)


# ============================================================================
# Public Routes (Rate Limited, No Auth)
# ============================================================================

@router.get("/widget/{widget_key}/config", response_model=PublicWidgetConfigResponse)
async def get_public_widget_config(
    widget_key: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """Get public widget configuration (for embed script)."""
    config = db.query(ChatWidgetConfig).filter(
        ChatWidgetConfig.widget_key == widget_key,
        ChatWidgetConfig.is_active == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Widget not found")

    # Check domain whitelist
    origin = request.headers.get("origin", "")
    if config.allowed_domains and len(config.allowed_domains) > 0:
        allowed = False
        for domain in config.allowed_domains:
            if domain in origin or domain == "*":
                allowed = True
                break
        if not allowed:
            raise HTTPException(status_code=403, detail="Domain not allowed")

    return PublicWidgetConfigResponse(
        widget_key=config.widget_key,
        theme=config.theme,
        welcome_message=config.welcome_message,
        placeholder_text=config.placeholder_text,
        bot_name=config.bot_name,
        bot_avatar_url=config.bot_avatar_url,
        enable_webchat=config.enable_webchat,
        enable_whatsapp_button=config.enable_whatsapp_button,
        whatsapp_number=config.whatsapp_number,
        enable_file_upload=config.enable_file_upload,
        enable_feedback=config.enable_feedback,
        enable_sound=config.enable_sound,
        enable_typing_indicator=config.enable_typing_indicator,
        require_email=config.require_email,
        auto_open_delay=config.auto_open_delay,
        greeting_delay=config.greeting_delay,
        offline_message=config.offline_message
    )


@router.post("/widget/{widget_key}/session", response_model=PublicSessionResponse)
async def create_widget_session(
    widget_key: str,
    request_data: PublicSessionRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    """Create a new chat session for the widget."""
    config = db.query(ChatWidgetConfig).filter(
        ChatWidgetConfig.widget_key == widget_key,
        ChatWidgetConfig.is_active == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Widget not found")

    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{widget_key}:{client_ip}"
    if not check_rate_limit(rate_key, config.rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Generate user identifier if not provided
    user_identifier = request_data.user_identifier or request_data.email
    if not user_identifier:
        user_identifier = f"widget_{secrets.token_urlsafe(8)}"

    # Resolve the chatbot_id for session creation
    resolved_chatbot_id = config.chatbot_id
    if not resolved_chatbot_id and config.handler_type == "chatbot":
        resolved_chatbot_id = config.handler_id

    # Create session (chatbot-based sessions need a chatbot_id)
    conv_manager = ConversationManager(db)
    if resolved_chatbot_id:
        session = conv_manager.get_or_create_session(
            chatbot_id=resolved_chatbot_id,
            user_identifier=user_identifier,
            channel="web"
        )
    else:
        # Agent team or no handler — create a generic session
        session = conv_manager.get_or_create_session(
            chatbot_id=None,
            user_identifier=user_identifier,
            channel="web",
            project_id=config.project_id,
        )

    return PublicSessionResponse(
        session_id=session.id,
        welcome_message=config.welcome_message
    )


@router.post("/widget/{widget_key}/chat", response_model=PublicChatResponse)
async def widget_chat(
    widget_key: str,
    request_data: PublicChatRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    """Send a message and get a response from the widget chatbot."""
    config = db.query(ChatWidgetConfig).filter(
        ChatWidgetConfig.widget_key == widget_key,
        ChatWidgetConfig.is_active == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Widget not found")

    if not config.enable_webchat:
        raise HTTPException(status_code=400, detail="Web chat is disabled for this widget")

    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{widget_key}:{client_ip}"
    if not check_rate_limit(rate_key, config.rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Resolve handler from config
    resolved_chatbot_id = config.chatbot_id
    if not resolved_chatbot_id and config.handler_type == "chatbot":
        resolved_chatbot_id = config.handler_id

    # Get or create session
    conv_manager = ConversationManager(db)
    if request_data.session_id:
        session = conv_manager.get_session_by_id(request_data.session_id)
        if not session:
            raise HTTPException(status_code=400, detail="Invalid session")
    else:
        user_identifier = request_data.user_identifier or request_data.email or f"widget_{secrets.token_urlsafe(8)}"
        if resolved_chatbot_id:
            session = conv_manager.get_or_create_session(
                chatbot_id=resolved_chatbot_id,
                user_identifier=user_identifier,
                channel="web"
            )
        else:
            session = conv_manager.get_or_create_session(
                chatbot_id=None,
                user_identifier=user_identifier,
                channel="web",
                project_id=config.project_id,
            )

    # Process message via InboundRouter
    from app.services.inbound.adapters import normalize_web_chat
    from app.services.inbound.router import InboundRouter

    inbound_msg = normalize_web_chat(
        message=request_data.message,
        user_identifier=session.user_identifier,
        session_id=session.id,
        instance_id=config.id,
    )
    inbound_msg.project_id = config.project_id

    inbound_router = InboundRouter(db)
    result = await inbound_router.route(inbound_msg)

    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to process message"))

    return PublicChatResponse(
        message=result.get("response", ""),
        session_id=session.id,
        timestamp=datetime.utcnow()
    )


@router.post("/widget/{widget_key}/feedback")
async def submit_widget_feedback(
    widget_key: str,
    request_data: PublicFeedbackRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    """Submit feedback for a conversation."""
    config = db.query(ChatWidgetConfig).filter(
        ChatWidgetConfig.widget_key == widget_key,
        ChatWidgetConfig.is_active == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Widget not found")

    if not config.enable_feedback:
        raise HTTPException(status_code=400, detail="Feedback is disabled for this widget")

    # Verify session belongs to this chatbot
    session = db.query(ChatSession).filter(
        ChatSession.id == request_data.session_id,
        ChatSession.chatbot_id == config.chatbot_id
    ).first()

    if not session:
        raise HTTPException(status_code=400, detail="Invalid session")

    # Create feedback
    feedback = ConversationFeedback(
        session_id=request_data.session_id,
        message_id=request_data.message_id,
        rating=request_data.rating,
        rating_score=request_data.rating_score,
        feedback_text=request_data.feedback_text,
        submitter_identifier=session.user_identifier
    )

    db.add(feedback)
    db.commit()

    return {"success": True, "message": "Feedback submitted"}


@router.get("/widget/{widget_key}/messages")
async def get_widget_messages(
    widget_key: str,
    session_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """Get messages for a widget session."""
    config = db.query(ChatWidgetConfig).filter(
        ChatWidgetConfig.widget_key == widget_key,
        ChatWidgetConfig.is_active == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Widget not found")

    # Verify session
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.chatbot_id == config.chatbot_id
    ).first()

    if not session:
        raise HTTPException(status_code=400, detail="Invalid session")

    messages = db.query(ChatMessage).filter(
        ChatMessage.session_id == session_id
    ).order_by(ChatMessage.timestamp.asc()).limit(limit).all()

    return {
        "session_id": session_id,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "sender_type": m.sender_type,
                "timestamp": m.timestamp.isoformat()
            }
            for m in messages
        ]
    }


# ============================================================================
# Project-Scoped Web Widget CRUD (Authenticated)
# ============================================================================

class WebWidgetCreateRequest(BaseModel):
    handler_type: Optional[str] = None  # 'chatbot' | 'agent_team' | None
    handler_id: Optional[int] = None
    bot_name: Optional[str] = None
    welcome_message: Optional[str] = "Hi! How can I help you today?"
    placeholder_text: Optional[str] = "Type a message..."
    theme: Optional[Dict[str, Any]] = None
    allowed_domains: Optional[List[str]] = None


class WebWidgetUpdateRequest(BaseModel):
    handler_type: Optional[str] = None
    handler_id: Optional[int] = None
    bot_name: Optional[str] = None
    welcome_message: Optional[str] = None
    placeholder_text: Optional[str] = None
    bot_avatar_url: Optional[str] = None
    theme: Optional[Dict[str, Any]] = None
    enable_webchat: Optional[bool] = None
    enable_file_upload: Optional[bool] = None
    enable_feedback: Optional[bool] = None
    enable_sound: Optional[bool] = None
    enable_typing_indicator: Optional[bool] = None
    allowed_domains: Optional[List[str]] = None
    rate_limit_per_minute: Optional[int] = None
    require_email: Optional[bool] = None
    is_active: Optional[bool] = None


def _sync_web_widget_link(db: Session, config: ChatWidgetConfig) -> None:
    """Auto-sync handler_channel_links when widget handler changes."""
    link_svc = HandlerChannelLinkService(db)
    if config.handler_type and config.handler_id:
        link_svc.set_link(
            handler_type=config.handler_type,
            handler_id=config.handler_id,
            channel="web",
            instance_id=config.id,
            is_primary=True,
        )
    else:
        # Remove any stale links pointing to this widget
        link_svc.remove_by_instance("web", config.id)


@router.get(
    "/projects/{project_id}/web-widgets",
    response_model=List[WidgetConfigResponse],
)
async def list_web_widgets(
    project_id: int,
    db: Session = Depends(get_db),
    _user=Depends(require_project_role("viewer")),
):
    """List all web widgets for a project."""
    widgets = (
        db.query(ChatWidgetConfig)
        .filter(ChatWidgetConfig.project_id == project_id)
        .order_by(ChatWidgetConfig.id)
        .all()
    )
    return [WidgetConfigResponse.model_validate(w) for w in widgets]


@router.post(
    "/projects/{project_id}/web-widgets",
    response_model=WidgetConfigResponse,
    status_code=201,
)
async def create_web_widget(
    project_id: int,
    body: WebWidgetCreateRequest,
    db: Session = Depends(get_db),
    _user=Depends(require_project_role("editor")),
):
    """Create a new web widget for a project."""
    chatbot_id = None
    if body.handler_type == "chatbot" and body.handler_id:
        chatbot_id = body.handler_id

    config = ChatWidgetConfig(
        project_id=project_id,
        chatbot_id=chatbot_id,
        handler_type=body.handler_type,
        handler_id=body.handler_id,
        widget_key=f"wk_{secrets.token_urlsafe(16)}",
        bot_name=body.bot_name,
        welcome_message=body.welcome_message,
        placeholder_text=body.placeholder_text,
        theme=body.theme or {"primary_color": "#007bff", "position": "bottom-right"},
        allowed_domains=body.allowed_domains,
        enable_webchat=True,
        enable_feedback=True,
        enable_sound=True,
        enable_typing_indicator=True,
        rate_limit_per_minute=20,
        is_active=True,
    )
    db.add(config)
    db.commit()
    db.refresh(config)

    _sync_web_widget_link(db, config)

    return WidgetConfigResponse.model_validate(config)


@router.put(
    "/projects/{project_id}/web-widgets/{widget_id}",
    response_model=WidgetConfigResponse,
)
async def update_web_widget(
    project_id: int,
    widget_id: int,
    body: WebWidgetUpdateRequest,
    db: Session = Depends(get_db),
    _user=Depends(require_project_role("editor")),
):
    """Update a web widget."""
    config = (
        db.query(ChatWidgetConfig)
        .filter(
            ChatWidgetConfig.id == widget_id,
            ChatWidgetConfig.project_id == project_id,
        )
        .first()
    )
    if not config:
        raise HTTPException(404, "Widget not found")

    update_data = body.model_dump(exclude_unset=True)

    # If handler changes, also update chatbot_id for backward compat
    if "handler_type" in update_data or "handler_id" in update_data:
        new_type = update_data.get("handler_type", config.handler_type)
        new_id = update_data.get("handler_id", config.handler_id)
        config.handler_type = new_type
        config.handler_id = new_id
        config.chatbot_id = new_id if new_type == "chatbot" else None

    for field, value in update_data.items():
        if field in ("handler_type", "handler_id"):
            continue  # Already handled
        if hasattr(config, field):
            setattr(config, field, value)

    db.commit()
    db.refresh(config)

    _sync_web_widget_link(db, config)

    return WidgetConfigResponse.model_validate(config)


@router.delete("/projects/{project_id}/web-widgets/{widget_id}")
async def delete_web_widget(
    project_id: int,
    widget_id: int,
    db: Session = Depends(get_db),
    _user=Depends(require_project_role("admin")),
):
    """Delete a web widget."""
    config = (
        db.query(ChatWidgetConfig)
        .filter(
            ChatWidgetConfig.id == widget_id,
            ChatWidgetConfig.project_id == project_id,
        )
        .first()
    )
    if not config:
        raise HTTPException(404, "Widget not found")

    # Clean up handler_channel_links
    link_svc = HandlerChannelLinkService(db)
    link_svc.remove_by_instance("web", config.id)

    db.delete(config)
    db.commit()

    return {"success": True}
