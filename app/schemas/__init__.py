from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import Optional, List, Dict, Any

class CustomerBase(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    address: Optional[str] = None

class CustomerCreate(CustomerBase):
    pass

class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    address: Optional[str] = None

class Customer(CustomerBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

# SMTP Configuration Schemas
class SMTPConfigBase(BaseModel):
    smtp_server: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_email: EmailStr
    from_name: Optional[str] = None
    is_active: bool = True

class SMTPConfigCreate(SMTPConfigBase):
    pass  # project_id will be passed in the URL

class SMTPConfigUpdate(BaseModel):
    smtp_server: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    smtp_use_ssl: Optional[bool] = None
    from_email: Optional[EmailStr] = None
    from_name: Optional[str] = None
    is_active: Optional[bool] = None
    inbound_enabled: Optional[bool] = None
    inbound_provider: Optional[str] = None

class SMTPConfig(SMTPConfigBase):
    id: int
    project_id: int
    customer_id: Optional[int] = None  # Keep for backward compatibility
    inbound_enabled: bool = False
    inbound_provider: Optional[str] = None
    mx_record_status: Optional[str] = None
    is_bidirectional: bool = False
    feedback_webhook_url: Optional[str] = None
    feedback_configured: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class FunnelTestRequest(BaseModel):
    test_email: EmailStr

# WhatsApp Configuration Schemas
class WhatsAppConfigBase(BaseModel):
    instance_name: str
    is_active: bool = True

class WhatsAppConfigCreate(WhatsAppConfigBase):
    customer_id: int

class WhatsAppConfigUpdate(BaseModel):
    instance_name: Optional[str] = None
    is_active: Optional[bool] = None

class WhatsAppConfig(WhatsAppConfigBase):
    id: int
    customer_id: int
    evolution_instance_id: Optional[str] = None
    connection_status: str
    qr_code: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

# WhatsApp Instance Schemas (New Architecture)
class WhatsAppInstanceBase(BaseModel):
    is_active: bool = True

class WhatsAppInstanceCreate(WhatsAppInstanceBase):
    pass

class WhatsAppInstanceUpdate(BaseModel):
    is_active: Optional[bool] = None
    default_country_code: Optional[str] = None

class WhatsAppInstance(WhatsAppInstanceBase):
    id: int
    user_id: int
    workspace_id: int
    project_id: Optional[int] = None
    instance_name: str
    provider_type: str = "evolution_api"
    connection_status: str
    phone_number: Optional[str] = None
    qr_code_data: Optional[str] = None
    webhook_url: Optional[str] = None
    default_country_code: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_connected_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WhatsAppInstanceCreateRequest(BaseModel):
    instance_name: Optional[str] = None


class WhatsAppInstanceListItem(BaseModel):
    id: int
    instance_name: str
    provider_type: str = "evolution_api"
    connection_status: str
    phone_number: Optional[str] = None
    project_id: Optional[int] = None
    is_active: bool = True
    created_at: datetime
    last_connected_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class WhatsAppConnectionResponse(BaseModel):
    success: bool
    message: str
    qr_code: Optional[str] = None
    instance_name: str
    connection_status: str

class WhatsAppStatusResponse(BaseModel):
    instance_name: str
    connection_status: str
    phone_number: Optional[str] = None
    last_connected_at: Optional[datetime] = None
    is_active: bool

# QR Code Response Schema (Legacy - keeping for compatibility)
class QRCodeResponse(BaseModel):
    qr_code: str
    instance_id: str
    status: str

# Auth Schemas
class SocialLoginRequest(BaseModel):
    provider: str
    token: str
    invitation_token: Optional[str] = None

class KeycloakCallbackRequest(BaseModel):
    code: str
    redirect_uri: str
    state: Optional[str] = None
    code_verifier: Optional[str] = None

class RefreshTokenRequest(BaseModel):
    refresh_token: Optional[str] = None

class UserBase(BaseModel):
    email: EmailStr
    name: str
    role: Optional[str] = "user"
    is_verified: bool = False

class User(UserBase):
    id: int
    keycloak_id: Optional[str] = None
    social_provider: Optional[str] = None
    workspace_id: Optional[int] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True

class WorkspaceBase(BaseModel):
    name: str

class Workspace(WorkspaceBase):
    id: int
    owner_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Project Schemas
class ProjectBase(BaseModel):
    name: str
    description: Optional[str] = None

class ProjectCreate(ProjectBase):
    workspace_id: int

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class Project(ProjectBase):
    id: int
    workspace_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True




# ==========================================
# Post for Me Integration Schemas
# ==========================================

# Credentials
class PostForMeCredentialCreate(BaseModel):
    api_key: str

class PostForMeCredentialResponse(BaseModel):
    id: int
    project_id: int
    is_active: bool
    last_validated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

# Social Accounts
class PostForMeSocialAccountResponse(BaseModel):
    id: int
    postforme_account_id: str
    platform: str
    account_name: Optional[str] = None
    account_username: Optional[str] = None
    account_profile_url: Optional[str] = None
    is_connected: bool
    connection_status: str
    metadata: Dict[str, Any] = {}
    last_sync_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True

class AuthUrlRequest(BaseModel):
    platform: str  # facebook, instagram, twitter, etc.

class AuthUrlResponse(BaseModel):
    auth_url: str
    platform: str

# Posts
class PostContentItem(BaseModel):
    text: str
    mediaIds: Optional[List[str]] = None

class PostCreate(BaseModel):
    account_ids: List[str]  # List of postforme_account_ids
    content: Dict[str, PostContentItem]  # {"default": {...}, "instagram": {...}}
    scheduled_time: Optional[datetime] = None
    external_id: Optional[str] = None
    tags: Optional[List[str]] = None

class PostUpdate(BaseModel):
    content: Optional[Dict[str, PostContentItem]] = None
    scheduled_time: Optional[datetime] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None

class PostResultResponse(BaseModel):
    id: int
    platform: str
    status: str
    platform_post_id: Optional[str] = None
    platform_post_url: Optional[str] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True

class PostResponse(BaseModel):
    id: int
    project_id: int
    postforme_post_id: Optional[str] = None
    content: Dict[str, Any]
    target_account_ids: List[str]
    status: str
    scheduled_time: Optional[datetime] = None
    published_at: Optional[datetime] = None
    external_id: Optional[str] = None
    tags: List[str] = []
    created_by: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class PostWithResultsResponse(BaseModel):
    post: PostResponse
    results: List[PostResultResponse]

# Media
class MediaUploadUrlRequest(BaseModel):
    file_name: str

class MediaUploadUrlResponse(BaseModel):
    id: str
    upload_url: str
    expires_at: datetime

class MediaResponse(BaseModel):
    id: int
    postforme_media_id: Optional[str] = None
    file_name: str
    file_type: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    permanent_url: Optional[str] = None
    status: str
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True

# Webhooks
class WebhookCreate(BaseModel):
    url: str
    events: List[str]

class WebhookUpdate(BaseModel):
    url: Optional[str] = None
    events: Optional[List[str]] = None
    is_active: Optional[bool] = None

class WebhookResponse(BaseModel):
    id: int
    postforme_webhook_id: Optional[str] = None
    url: str
    events: List[str]
    is_active: bool
    last_triggered_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Chatbot Schemas
# ============================================================================

# Chatbot Schemas
class ChatbotBase(BaseModel):
    name: str
    description: Optional[str] = None
    intention: Optional[str] = None
    model_provider: str = "openai"
    model_name: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int = 2000
    system_prompt: Optional[str] = None
    whatsapp_instance_id: Optional[int] = None
    auto_respond_whatsapp: bool = False
    status: str = "active"
    is_public: bool = False

class ChatbotCreate(ChatbotBase):
    project_id: int

class ChatbotUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    intention: Optional[str] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    whatsapp_instance_id: Optional[int] = None
    auto_respond_whatsapp: Optional[bool] = None
    status: Optional[str] = None
    is_public: Optional[bool] = None

class ChatbotResponse(ChatbotBase):
    id: int
    project_id: int
    vector_store_path: Optional[str] = None
    knowledge_base_path: Optional[str] = None
    channel_links: Optional[List[dict]] = None
    created_at: datetime
    updated_at: datetime

    # Statistics (can be added later)
    total_sessions: int = 0
    total_messages: int = 0
    total_documents: int = 0

    class Config:
        from_attributes = True


# Chat Session Schemas
class ChatSessionBase(BaseModel):
    user_identifier: str
    channel: str = "web"
    context_data: Optional[dict] = {}
    session_metadata: Optional[dict] = {}

class ChatSessionCreate(ChatSessionBase):
    chatbot_id: int
    user_id: Optional[int] = None

class ChatSessionResponse(ChatSessionBase):
    id: int
    chatbot_id: int
    user_id: Optional[int] = None
    is_active: bool
    started_at: datetime
    last_interaction_at: datetime
    ended_at: Optional[datetime] = None
    message_count: int = 0

    class Config:
        from_attributes = True


# Chat Message Schemas
class ChatMessageBase(BaseModel):
    role: str  # user, assistant, system
    content: str
    images: Optional[List[str]] = []
    attachments: Optional[List[str]] = []

class ChatMessageCreate(ChatMessageBase):
    session_id: int

class ChatMessageResponse(ChatMessageBase):
    id: int
    session_id: int
    retrieved_documents: Optional[List[dict]] = []
    retrieval_metadata: Optional[dict] = {}
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    message_metadata: Optional[dict] = {}
    timestamp: datetime

    class Config:
        from_attributes = True


# Knowledge Document Schemas
class KnowledgeDocumentBase(BaseModel):
    source_type: str  # file, url, text
    source_url: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    content: Optional[str] = None
    document_metadata: Optional[dict] = {}

class KnowledgeDocumentCreate(KnowledgeDocumentBase):
    chatbot_id: int

class KnowledgeDocumentUpdate(BaseModel):
    source_url: Optional[str] = None
    content: Optional[str] = None
    document_metadata: Optional[dict] = None

class KnowledgeDocumentResponse(KnowledgeDocumentBase):
    id: int
    chatbot_id: int
    file_path: Optional[str] = None
    content_hash: Optional[str] = None
    chunk_count: int = 0
    embedding_model: Optional[str] = None
    processed: bool = False
    processing_error: Optional[str] = None
    uploaded_at: datetime
    processed_at: Optional[datetime] = None
    last_updated_at: datetime

    class Config:
        from_attributes = True


# Chat Request/Response Schemas
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[int] = None
    user_identifier: Optional[str] = None
    channel: str = "web"
    context: Optional[dict] = {}

class ChatResponse(BaseModel):
    message: str
    session_id: int
    images: Optional[List[str]] = []
    retrieved_sources: Optional[List[dict]] = []
    tokens_used: Optional[int] = None
    processing_time: Optional[float] = None

# URL Upload Schema
class URLUploadRequest(BaseModel):
    url: str
    scrape_depth: int = 1  # How deep to follow links
    metadata: Optional[dict] = {}

# Batch Upload Response
class BatchUploadResponse(BaseModel):
    success: int
    failed: int
    total: int
    documents: List[KnowledgeDocumentResponse]
    errors: Optional[List[dict]] = []


# ============================================================================
# Whitelist Signup Schemas (for static website)
# ============================================================================

class WhitelistSignupBase(BaseModel):
    email: EmailStr
    phone: Optional[str] = None
    country: Optional[str] = None
    name: Optional[str] = None
    company: Optional[str] = None
    suggestion: Optional[str] = None


class WhitelistSignupCreate(WhitelistSignupBase):
    # UTM tracking (optional, can be sent from frontend)
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None


class WhitelistSignupResponse(WhitelistSignupBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class WhitelistSignupFullResponse(WhitelistSignupBase):
    """Full response with all tracking data (for admin use)"""
    id: int
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    referrer: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    is_verified: bool
    verified_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WhitelistSignupSuccessResponse(BaseModel):
    success: bool
    message: str
    signup_id: int


# ============================================================================
# Chatbot Routing Schemas
# ============================================================================

class ChatbotRoutingBase(BaseModel):
    routing_mode: str = "bot_then_human"  # human_only, bot_only, bot_then_human, custom
    fallback_to_human: bool = True
    escalation_keywords: Optional[List[str]] = []
    confidence_threshold: Optional[float] = 0.6
    max_bot_turns: Optional[int] = 10


class ChatbotRoutingCreate(ChatbotRoutingBase):
    pass


class ChatbotRoutingUpdate(BaseModel):
    routing_mode: Optional[str] = None
    fallback_to_human: Optional[bool] = None
    escalation_keywords: Optional[List[str]] = None
    confidence_threshold: Optional[float] = None
    max_bot_turns: Optional[int] = None


class ChatbotRoutingResponse(ChatbotRoutingBase):
    id: int
    chatbot_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
