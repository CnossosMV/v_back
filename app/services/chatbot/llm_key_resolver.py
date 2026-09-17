"""
LLM Key Resolver

Single point of truth for "which LLM to use" for any purpose.
Resolves based on: specialist override > project own_key config > system env > system DB.

One key per provider per project. If a project has a key for a provider,
it is used for all purposes that would normally use that provider.
"""
import os
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Internal system defaults — admin controls via env vars
SYSTEM_MODEL_DEFAULTS = {
    "chat":       {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.7},
    "routing":    {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.1},
    "guardrails": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.0},
    "embeddings": {"provider": "openai", "model": "text-embedding-3-small"},
    "summary":    {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.3},
    "milestone":  {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.0},
    "media":      {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.0},
    "spec_parse": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.1},
}

SYSTEM_API_KEYS = {
    "openai":    lambda: os.getenv("OPENAI_API_KEY"),
    "anthropic": lambda: os.getenv("ANTHROPIC_API_KEY"),
    "google":    lambda: os.getenv("GOOGLE_AI_API_KEY"),
}

# Available chat models per provider (for the /models endpoint)
AVAILABLE_MODELS = {
    "openai": [
        {"model_id": "gpt-4o-mini", "display_name": "GPT-4o Mini", "type": "chat"},
        {"model_id": "gpt-4o", "display_name": "GPT-4o", "type": "chat"},
        {"model_id": "gpt-4-turbo", "display_name": "GPT-4 Turbo", "type": "chat"},
        {"model_id": "gpt-3.5-turbo", "display_name": "GPT-3.5 Turbo", "type": "chat"},
    ],
    "anthropic": [
        {"model_id": "claude-sonnet-4-5-20250929", "display_name": "Claude Sonnet 4.5", "type": "chat"},
        {"model_id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5", "type": "chat"},
    ],
    "google": [
        {"model_id": "gemini-pro", "display_name": "Gemini Pro", "type": "chat"},
        {"model_id": "gemini-pro-vision", "display_name": "Gemini Pro Vision", "type": "chat"},
    ],
}

# Embedding models with dimensions and provider info
EMBEDDING_MODELS = {
    "text-embedding-3-small": {"provider": "openai", "dimensions": 1536, "display_name": "OpenAI Embedding 3 Small"},
    "text-embedding-3-large": {"provider": "openai", "dimensions": 3072, "display_name": "OpenAI Embedding 3 Large"},
    "text-embedding-ada-002": {"provider": "openai", "dimensions": 1536, "display_name": "OpenAI Ada 002"},
    "text-embedding-004":     {"provider": "google", "dimensions": 768, "display_name": "Google Text Embedding 004"},
}

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def infer_provider(model_name: str) -> str:
    """Infer provider from model name prefix."""
    name = model_name.lower()
    if name.startswith("gpt-") or name.startswith("o1") or name.startswith("text-embedding-3") or name.startswith("text-embedding-a"):
        return "openai"
    if name.startswith("claude-"):
        return "anthropic"
    if name.startswith("gemini") or name.startswith("text-embedding-004"):
        return "google"
    # Default to openai for unknown models
    return "openai"


@dataclass
class LLMConfig:
    """Resolved LLM configuration."""
    api_key: str
    provider: str
    model: str
    temperature: float = 0.7
    key_source: str = "system"  # "project" or "system"


@dataclass
class EmbeddingConfig:
    """Resolved embedding configuration."""
    api_key: str
    provider: str       # openai, google
    model: str          # text-embedding-3-small, text-embedding-004
    dimensions: int     # 1536, 768, etc.
    key_source: str = "system"  # "project" or "system"


def _resolve_system_key(db: Session, provider: str) -> Optional[str]:
    """
    Resolve system API key for a provider.
    Priority: env var first, then DB system_llm_providers table.
    """
    # 1. Env var
    env_fn = SYSTEM_API_KEYS.get(provider)
    api_key = env_fn() if env_fn else None
    if api_key:
        return api_key

    # 2. DB table
    try:
        from app.models import SystemLLMProvider
        from app.services.encryption_service import decrypt_value

        sys_provider = db.query(SystemLLMProvider).filter(
            SystemLLMProvider.provider == provider,
            SystemLLMProvider.is_active == True,
        ).first()
        if sys_provider:
            return decrypt_value(sys_provider.api_key_encrypted)
    except Exception as e:
        logger.warning(f"Failed to query system LLM provider '{provider}' from DB: {e}")

    return None


def resolve_llm(
    db: Session,
    project_id: int,
    purpose: str = "chat",
    specialist_model_name: Optional[str] = None,
    specialist_temperature: Optional[float] = None,
) -> LLMConfig:
    """
    Resolve which LLM configuration to use.

    Priority (highest first):
    1. Specialist/chatbot explicit model_name + temperature (if set per-agent)
    2. Project's own key for the target provider (if row exists)
    3. System key: env var first, then DB system_llm_providers table

    Args:
        db: Database session
        project_id: Project ID to resolve config for
        purpose: One of: chat, routing, guardrails, embeddings, summary, milestone, media, spec_parse
        specialist_model_name: Optional per-agent model override
        specialist_temperature: Optional per-agent temperature override

    Returns:
        LLMConfig with api_key, provider, model, temperature, key_source
    """
    from app.models import ProjectLLMConfig

    system_defaults = SYSTEM_MODEL_DEFAULTS.get(purpose, SYSTEM_MODEL_DEFAULTS["chat"])
    default_provider = system_defaults["provider"]
    default_model = system_defaults["model"]
    default_temp = system_defaults.get("temperature", 0.7)

    # Determine target provider: specialist model override takes priority
    if specialist_model_name:
        target_provider = infer_provider(specialist_model_name)
    else:
        target_provider = default_provider

    # Check if project has its own key for this provider
    project_config = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
        ProjectLLMConfig.provider == target_provider,
        ProjectLLMConfig.is_active == True,
    ).first()

    if project_config:
        from app.services.encryption_service import decrypt_value
        try:
            api_key = decrypt_value(project_config.api_key_encrypted)
        except Exception:
            logger.error(f"Failed to decrypt LLM key for project {project_id} provider {target_provider}, falling back to system key")
            api_key = None

        if api_key:
            # Use project's own key
            model = specialist_model_name or project_config.preferred_model or default_model
            temperature = default_temp
            if project_config.temperature is not None:
                temperature = project_config.temperature
            if specialist_temperature is not None:
                temperature = specialist_temperature

            return LLMConfig(
                api_key=api_key,
                provider=target_provider,
                model=model,
                temperature=temperature,
                key_source="project",
            )

    # No project key for this provider — use system key (env var → DB)
    api_key = _resolve_system_key(db, target_provider)

    if not api_key:
        raise ValueError(f"No API key available for provider '{target_provider}' (purpose: {purpose})")

    model = specialist_model_name or default_model
    temperature = default_temp
    if specialist_temperature is not None:
        temperature = specialist_temperature

    return LLMConfig(
        api_key=api_key,
        provider=target_provider,
        model=model,
        temperature=temperature,
        key_source="system",
    )


def resolve_embeddings(
    db: Session,
    project_id: int,
    preferred_model: Optional[str] = None,
) -> EmbeddingConfig:
    """
    Resolve embedding configuration for a project.

    Priority:
    1. preferred_model parameter (e.g. from chatbot.embedding_model)
    2. System default (text-embedding-3-small)

    Then resolves the API key for the inferred provider:
    1. Project's own key for the provider
    2. System key (env var → DB)
    """
    model = preferred_model or DEFAULT_EMBEDDING_MODEL
    model_info = EMBEDDING_MODELS.get(model)
    if not model_info:
        logger.warning(f"Unknown embedding model '{model}', falling back to default")
        model = DEFAULT_EMBEDDING_MODEL
        model_info = EMBEDDING_MODELS[model]

    provider = model_info["provider"]
    dimensions = model_info["dimensions"]

    # Resolve API key using the same chain as resolve_llm
    from app.models import ProjectLLMConfig
    from app.services.encryption_service import decrypt_value

    # 1. Project key
    project_config = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
        ProjectLLMConfig.provider == provider,
        ProjectLLMConfig.is_active == True,
    ).first()

    if project_config:
        try:
            api_key = decrypt_value(project_config.api_key_encrypted)
            if api_key:
                return EmbeddingConfig(
                    api_key=api_key,
                    provider=provider,
                    model=model,
                    dimensions=dimensions,
                    key_source="project",
                )
        except Exception:
            logger.error(f"Failed to decrypt embedding key for project {project_id} provider {provider}")

    # 2. System key (env → DB)
    api_key = _resolve_system_key(db, provider)
    if not api_key:
        raise ValueError(f"No API key available for embedding provider '{provider}' (model: {model})")

    return EmbeddingConfig(
        api_key=api_key,
        provider=provider,
        model=model,
        dimensions=dimensions,
        key_source="system",
    )


def create_chat_llm(config: LLMConfig, max_tokens: int = 2000):
    """
    Factory: create the right LangChain chat model based on provider.
    """
    if config.provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            openai_api_key=config.api_key,
            model_name=config.model,
            temperature=config.temperature,
            max_tokens=max_tokens,
        )
    elif config.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            anthropic_api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=max_tokens,
        )
    elif config.provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            google_api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_output_tokens=max_tokens,
        )
    raise ValueError(f"Unsupported chat provider: {config.provider}")


def create_embeddings(config: EmbeddingConfig):
    """
    Factory: create the right LangChain embeddings object based on provider.
    """
    if config.provider == "openai":
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(
            openai_api_key=config.api_key,
            model=config.model,
        )
    elif config.provider == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        return GoogleGenerativeAIEmbeddings(
            google_api_key=config.api_key,
            model=f"models/{config.model}",
        )
    raise ValueError(f"Unsupported embedding provider: {config.provider}")
