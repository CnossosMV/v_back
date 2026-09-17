"""
Chatbot services package.

This package contains all services related to chatbot/agent functionality:
- knowledge_loader: Load and process documents (markdown, PDF, URLs)
- vector_store: ChromaDB vector storage for embeddings
- langchain_service: RAG pipeline with LangChain
- conversation_manager: Session and context management
- agent_team_service: Agent Team CRUD and migration
- specialist_service: Specialist agent CRUD and knowledge management
- router_service: Message routing (LLM / rules / hybrid)
- guardrail_service: Guardrail checks (max turns, blocked topics, etc.)
- orchestration_engine: Multi-agent orchestration runtime
"""

from .knowledge_loader import KnowledgeLoader
from .vector_store import VectorStoreService
from .langchain_service import LangChainService
from .conversation_manager import ConversationManager
from .agent_team_service import AgentTeamService
from .specialist_service import SpecialistService
from .router_service import RouterService
from .guardrail_service import GuardrailService
from .orchestration_engine import OrchestrationEngine
from .tool_executor import ToolExecutor
from .playbook_service import PlaybookService
from .team_analytics_service import TeamAnalyticsService

__all__ = [
    "KnowledgeLoader",
    "VectorStoreService",
    "LangChainService",
    "ConversationManager",
    "AgentTeamService",
    "SpecialistService",
    "RouterService",
    "GuardrailService",
    "OrchestrationEngine",
    "ToolExecutor",
    "PlaybookService",
    "TeamAnalyticsService",
]
