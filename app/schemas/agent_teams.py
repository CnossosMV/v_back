from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any


# ============================================================================
# Agent Team Schemas
# ============================================================================

class AgentTeamCreate(BaseModel):
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    deployment_channels: Optional[List[str]] = []
    initial_message: Optional[str] = None
    whatsapp_instance_id: Optional[int] = None
    auto_respond_whatsapp: bool = False
    is_public: bool = False
    team_metadata: Optional[Dict[str, Any]] = {}


class AgentTeamUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    deployment_channels: Optional[List[str]] = None
    initial_message: Optional[str] = None
    whatsapp_instance_id: Optional[int] = None
    auto_respond_whatsapp: Optional[bool] = None
    is_public: Optional[bool] = None
    team_metadata: Optional[Dict[str, Any]] = None


class ChannelLinkInfo(BaseModel):
    channel: str
    instance_id: Optional[int] = None
    config: Optional[Dict[str, Any]] = None
    is_primary: bool = True


class AgentTeamResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    status: str
    deployment_channels: Optional[List[str]] = []
    initial_message: Optional[str] = None
    whatsapp_instance_id: Optional[int] = None  # deprecated, use channel_links
    auto_respond_whatsapp: bool
    is_public: bool
    migrated_from_chatbot_id: Optional[int] = None
    channel_links: Optional[List[ChannelLinkInfo]] = None
    team_metadata: Optional[Dict[str, Any]] = {}
    created_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    # Nested data (populated in detailed responses)
    specialists: Optional[List["SpecialistAgentResponse"]] = None
    router_config: Optional["RouterConfigResponse"] = None

    class Config:
        from_attributes = True


class AgentTeamListResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    status: str
    deployment_channels: Optional[List[str]] = []
    specialist_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Specialist Agent Schemas
# ============================================================================

class SpecialistAgentCreate(BaseModel):
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    icon: Optional[str] = Field(None, max_length=10)
    is_default: bool = False
    position_x: float = 0.0
    position_y: float = 0.0
    # Persona
    system_prompt: Optional[str] = None
    model_provider: str = "openai"
    model_name: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int = 2000
    tone: Optional[str] = "friendly"
    language: Optional[str] = None
    # Guardrails
    max_turns: Optional[int] = None
    frustration_action: Optional[str] = "escalate"
    blocked_topics: Optional[List[str]] = []
    operating_hours: Optional[Dict[str, Any]] = None
    escalation_config: Optional[Dict[str, Any]] = None


class SpecialistAgentUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    icon: Optional[str] = Field(None, max_length=10)
    is_default: Optional[bool] = None
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    # Persona
    system_prompt: Optional[str] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tone: Optional[str] = None
    language: Optional[str] = None
    # Guardrails
    max_turns: Optional[int] = None
    frustration_action: Optional[str] = None
    blocked_topics: Optional[List[str]] = None
    operating_hours: Optional[Dict[str, Any]] = None
    escalation_config: Optional[Dict[str, Any]] = None
    # Status
    status: Optional[str] = None
    agent_order: Optional[int] = None


class SpecialistAgentResponse(BaseModel):
    id: int
    team_id: int
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    is_default: bool
    position_x: float
    position_y: float
    # Persona
    system_prompt: Optional[str] = None
    model_provider: str
    model_name: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tone: Optional[str] = None
    language: Optional[str] = None
    # Guardrails
    max_turns: Optional[int] = None
    frustration_action: Optional[str] = None
    blocked_topics: Optional[List[str]] = []
    operating_hours: Optional[Dict[str, Any]] = None
    escalation_config: Optional[Dict[str, Any]] = None
    # Storage
    vector_store_path: Optional[str] = None
    knowledge_base_path: Optional[str] = None
    # Status
    status: str
    agent_order: int
    # Timestamps
    created_at: datetime
    updated_at: datetime
    # Nested
    knowledge_sources: Optional[List["SpecialistKnowledgeSourceResponse"]] = None

    class Config:
        from_attributes = True


# ============================================================================
# Specialist Knowledge Source Schemas
# ============================================================================

class SpecialistKnowledgeSourceCreate(BaseModel):
    source_type: str  # pdf, image, video_url, website_url, faq_url, internal_doc, text
    source_url: Optional[str] = None
    file_name: Optional[str] = None
    content: Optional[str] = None
    expose_to_user: bool = False
    label: Optional[str] = None
    source_metadata: Optional[Dict[str, Any]] = {}


class SpecialistKnowledgeSourceResponse(BaseModel):
    id: int
    specialist_id: int
    source_type: str
    source_url: Optional[str] = None
    file_path: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    content_hash: Optional[str] = None
    chunk_count: int = 0
    embedding_model: Optional[str] = None
    expose_to_user: bool
    label: Optional[str] = None
    processed: bool
    processing_error: Optional[str] = None
    source_metadata: Optional[Dict[str, Any]] = {}
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Router Config Schemas
# ============================================================================

class RouterConfigCreate(BaseModel):
    routing_mode: str = "auto"  # auto, rules, hybrid
    model_provider: str = "openai"
    model_name: str = "gpt-4o-mini"
    default_agent_id: Optional[int] = None
    allow_mid_convo_switch: bool = True
    switch_notification: str = "seamless"  # seamless, notify_user, disabled
    initial_message: Optional[str] = None
    position_x: float = 300.0
    position_y: float = 200.0
    router_metadata: Optional[Dict[str, Any]] = {}


class RouterConfigUpdate(BaseModel):
    routing_mode: Optional[str] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    default_agent_id: Optional[int] = None
    allow_mid_convo_switch: Optional[bool] = None
    switch_notification: Optional[str] = None
    initial_message: Optional[str] = None
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    router_metadata: Optional[Dict[str, Any]] = None


class RouterConfigResponse(BaseModel):
    id: int
    team_id: int
    routing_mode: str
    model_provider: str
    model_name: str
    default_agent_id: Optional[int] = None
    allow_mid_convo_switch: bool
    switch_notification: str
    initial_message: Optional[str] = None
    position_x: float
    position_y: float
    router_metadata: Optional[Dict[str, Any]] = {}
    created_at: datetime
    updated_at: datetime
    # Nested
    routing_rules: Optional[List["RoutingRuleResponse"]] = None

    class Config:
        from_attributes = True


# ============================================================================
# Routing Rule Schemas
# ============================================================================

class RoutingRuleCreate(BaseModel):
    specialist_id: int
    description: str
    priority: int = 0
    keyword_hints: Optional[List[str]] = None
    is_active: bool = True


class RoutingRuleUpdate(BaseModel):
    description: Optional[str] = None
    priority: Optional[int] = None
    keyword_hints: Optional[List[str]] = None
    is_active: Optional[bool] = None


class RoutingRuleResponse(BaseModel):
    id: int
    router_id: int
    specialist_id: int
    description: str
    priority: int
    keyword_hints: Optional[List[str]] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Migration Schemas
# ============================================================================

class MigrateChatbotRequest(BaseModel):
    """Request to migrate an existing chatbot to an agent team."""
    team_name: Optional[str] = None  # Override name, defaults to chatbot name


class MigrateChatbotResponse(BaseModel):
    team: AgentTeamResponse
    specialist: SpecialistAgentResponse
    knowledge_sources_migrated: int = 0
    message: str


# ============================================================================
# Chat Schemas (for Phase 2 orchestrated chat)
# ============================================================================

class TeamChatRequest(BaseModel):
    message: str
    user_identifier: Optional[str] = None
    channel: str = "web"
    session_id: Optional[int] = None
    context: Optional[Dict[str, Any]] = {}


class TeamChatResponse(BaseModel):
    response: str
    session_id: int
    images: Optional[List[str]] = []
    debug: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


# ============================================================================
# Session & Event Schemas (Phase 2)
# ============================================================================

class TeamSessionResponse(BaseModel):
    id: int
    team_id: Optional[int] = None
    user_identifier: str
    channel: str
    is_active: bool
    session_state: Optional[str] = None
    current_agent_id: Optional[int] = None
    context_summary: Optional[str] = None
    extracted_entities: Optional[Dict[str, Any]] = None
    started_at: datetime
    last_interaction_at: Optional[datetime] = None
    message_count: int = 0

    class Config:
        from_attributes = True


class TeamSessionEventResponse(BaseModel):
    id: int
    session_id: int
    event_type: str
    event_data: Optional[Dict[str, Any]] = {}
    agent_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TeamMessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    agent_id: Optional[int] = None
    routing_decision: Optional[Dict[str, Any]] = None
    images: Optional[List[str]] = []
    timestamp: datetime

    class Config:
        from_attributes = True


class GuardrailConfigUpdate(BaseModel):
    """Update guardrail settings on a specialist."""
    max_turns: Optional[int] = None
    frustration_action: Optional[str] = None  # escalate, retry_once, log_only
    blocked_topics: Optional[List[str]] = None
    operating_hours: Optional[Dict[str, Any]] = None
    escalation_config: Optional[Dict[str, Any]] = None


# ============================================================================
# Specialist Tool Schemas (Phase 3)
# ============================================================================

class SpecialistToolCreate(BaseModel):
    tool_type: str  # webhook, mcp_server, catalog_integration
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    when_to_use: Optional[str] = None
    config: Optional[Dict[str, Any]] = {}
    auth_config: Optional[Dict[str, Any]] = None  # Will be encrypted on save
    is_active: bool = True
    timeout_ms: int = 10000
    tool_metadata: Optional[Dict[str, Any]] = {}


class SpecialistToolUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    when_to_use: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    auth_config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    timeout_ms: Optional[int] = None
    tool_metadata: Optional[Dict[str, Any]] = None


class SpecialistToolResponse(BaseModel):
    id: int
    specialist_id: int
    tool_type: str
    name: str
    description: Optional[str] = None
    when_to_use: Optional[str] = None
    config: Optional[Dict[str, Any]] = {}
    has_auth: bool = False  # Whether auth_config_encrypted is set (don't expose the value)
    is_active: bool
    timeout_ms: int
    last_used_at: Optional[datetime] = None
    tool_metadata: Optional[Dict[str, Any]] = {}
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ToolTestRequest(BaseModel):
    parameters: Optional[Dict[str, Any]] = {}


class ToolTestResponse(BaseModel):
    execution_id: int
    status: str
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: int


class ToolExecutionResponse(BaseModel):
    id: int
    tool_id: Optional[int] = None
    session_id: Optional[int] = None
    message_id: Optional[int] = None
    status: str
    request_data: Optional[Dict[str, Any]] = {}
    response_data: Optional[Any] = None
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MCPDiscoverRequest(BaseModel):
    server_url: str


class MCPDiscoverResponse(BaseModel):
    tools: List[Dict[str, Any]]


# ============================================================================
# Playbook Schemas (Phase 4)
# ============================================================================

class PlaybookResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    category: str
    icon: Optional[str] = None
    agent_count: int
    template_data: Optional[Dict[str, Any]] = {}
    is_active: bool
    display_order: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PlaybookPreviewResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    category: str
    icon: Optional[str] = None
    agent_count: int
    template_data: Optional[Dict[str, Any]] = {}


class CreateFromPlaybookRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    deployment_channels: Optional[List[str]] = []


# ============================================================================
# Analytics Schemas (Phase 4)
# ============================================================================

class AnalyticsSnapshotResponse(BaseModel):
    id: int
    team_id: int
    period_start: datetime
    period_end: datetime
    total_sessions: int
    resolved_sessions: int
    escalated_sessions: int
    avg_turns_to_resolve: Optional[float] = None
    specialist_distribution: Optional[Dict[str, Any]] = {}
    routing_accuracy: Optional[float] = None
    total_tokens: int
    estimated_cost: float
    tool_call_count: int
    tool_success_rate: Optional[float] = None
    avg_response_time_ms: Optional[int] = None
    snapshot_metadata: Optional[Dict[str, Any]] = {}
    created_at: datetime

    class Config:
        from_attributes = True


class SpecialistBreakdownResponse(BaseModel):
    specialist_id: int
    name: str
    icon: Optional[str] = None
    total_sessions: int
    resolved_sessions: int
    escalated_sessions: int
    resolution_rate: Optional[float] = None
    message_count: int


class RoutingMetricsResponse(BaseModel):
    total_sessions: int
    single_agent_sessions: int
    accuracy: Optional[float] = None


class CostBreakdownResponse(BaseModel):
    total_tokens: int
    estimated_cost: float


# Rebuild forward refs for nested models
AgentTeamResponse.model_rebuild()
SpecialistAgentResponse.model_rebuild()
RouterConfigResponse.model_rebuild()
