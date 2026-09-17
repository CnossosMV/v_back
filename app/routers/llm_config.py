"""
Router for Project LLM Configuration.

Per-provider CRUD: one key per provider per project (max 3).
"""
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.auth import get_current_user
from app.models import User, Project, ProjectLLMConfig
from app.schemas.llm_config import (
    LLMProviderConfigCreate,
    LLMProviderConfigUpdate,
    LLMProviderConfigResponse,
    LLMConfigListResponse,
    LLMTestResponse,
    AvailableModel,
    AvailableModelsResponse,
    SystemProviderStatus,
    SystemStatusResponse,
)
from app.services.encryption_service import encrypt_value, decrypt_value
from app.services.chatbot.llm_key_resolver import AVAILABLE_MODELS, EMBEDDING_MODELS, _resolve_system_key

logger = logging.getLogger(__name__)

VALID_PROVIDERS = {"openai", "anthropic", "google"}

router = APIRouter(
    prefix="/projects/{project_id}/llm-config",
    tags=["LLM Configuration"],
)


def _get_project(db: Session, project_id: int, user: User) -> Project:
    """Validate project access."""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == user.workspace_id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _validate_provider(provider: str) -> None:
    if provider not in VALID_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider '{provider}'. Must be one of: {', '.join(sorted(VALID_PROVIDERS))}",
        )


def _config_to_response(config: ProjectLLMConfig) -> LLMProviderConfigResponse:
    return LLMProviderConfigResponse(
        id=config.id,
        project_id=config.project_id,
        provider=config.provider,
        preferred_model=config.preferred_model,
        temperature=config.temperature,
        is_active=config.is_active,
        has_key=bool(config.api_key_encrypted),
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.get("", response_model=LLMConfigListResponse)
def list_llm_configs(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all provider configs for the project."""
    _get_project(db, project_id, user)

    configs = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
    ).order_by(ProjectLLMConfig.provider).all()

    return LLMConfigListResponse(
        configs=[_config_to_response(c) for c in configs],
    )


@router.get("/{provider}", response_model=LLMProviderConfigResponse)
def get_provider_config(
    project_id: int,
    provider: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get config for a specific provider."""
    _get_project(db, project_id, user)
    _validate_provider(provider)

    config = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
        ProjectLLMConfig.provider == provider,
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail=f"No config for provider '{provider}'")

    return _config_to_response(config)


@router.put("/{provider}", response_model=LLMProviderConfigResponse)
def upsert_provider_config(
    project_id: int,
    provider: str,
    data: LLMProviderConfigCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create or update config for a specific provider."""
    _get_project(db, project_id, user)
    _validate_provider(provider)

    config = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
        ProjectLLMConfig.provider == provider,
    ).first()

    if not config:
        config = ProjectLLMConfig(
            project_id=project_id,
            provider=provider,
            api_key_encrypted=encrypt_value(data.api_key),
        )
        db.add(config)
    else:
        config.api_key_encrypted = encrypt_value(data.api_key)

    if data.preferred_model is not None:
        config.preferred_model = data.preferred_model
    if data.temperature is not None:
        config.temperature = data.temperature

    config.is_active = True
    db.commit()
    db.refresh(config)

    return _config_to_response(config)


@router.patch("/{provider}", response_model=LLMProviderConfigResponse)
def update_provider_config(
    project_id: int,
    provider: str,
    data: LLMProviderConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update an existing provider config (key is optional)."""
    _get_project(db, project_id, user)
    _validate_provider(provider)

    config = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
        ProjectLLMConfig.provider == provider,
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail=f"No config for provider '{provider}'")

    if data.api_key is not None:
        config.api_key_encrypted = encrypt_value(data.api_key)
    if data.preferred_model is not None:
        config.preferred_model = data.preferred_model
    if data.temperature is not None:
        config.temperature = data.temperature

    db.commit()
    db.refresh(config)

    return _config_to_response(config)


@router.delete("/{provider}", status_code=204)
def delete_provider_config(
    project_id: int,
    provider: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a provider config (reverts to system key for that provider)."""
    _get_project(db, project_id, user)
    _validate_provider(provider)

    config = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
        ProjectLLMConfig.provider == provider,
    ).first()

    if config:
        db.delete(config)
        db.commit()


@router.post("/{provider}/test", response_model=LLMTestResponse)
def test_provider_config(
    project_id: int,
    provider: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Test a provider's API key by making a tiny completion call."""
    _get_project(db, project_id, user)
    _validate_provider(provider)

    config = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
        ProjectLLMConfig.provider == provider,
    ).first()

    if not config or not config.api_key_encrypted:
        raise HTTPException(status_code=400, detail=f"No API key configured for provider '{provider}'")

    try:
        api_key = decrypt_value(config.api_key_encrypted)
    except Exception:
        return LLMTestResponse(success=False, message="Failed to decrypt stored key")

    # Determine a test model for the provider
    default_models = {"openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5-20251001", "google": "gemini-pro"}
    model = config.preferred_model or default_models.get(provider, "gpt-4o-mini")

    try:
        from app.services.chatbot.llm_key_resolver import LLMConfig, create_chat_llm
        cfg = LLMConfig(api_key=api_key, provider=provider, model=model, temperature=0.0)
        llm = create_chat_llm(cfg, max_tokens=5)
        llm.invoke([{"role": "user", "content": "Hi"}])
        return LLMTestResponse(
            success=True,
            message="API key is valid and working.",
            model_used=model,
        )
    except Exception as e:
        return LLMTestResponse(
            success=False,
            message=f"API key test failed: {str(e)}",
            model_used=model,
        )


@router.get("/models/available", response_model=AvailableModelsResponse)
def get_available_models(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List available models filtered to providers the project has access to."""
    _get_project(db, project_id, user)

    # Determine which providers are accessible (project key or system key)
    accessible_providers = set()
    for provider in ["openai", "anthropic", "google"]:
        has_project_key = db.query(ProjectLLMConfig).filter(
            ProjectLLMConfig.project_id == project_id,
            ProjectLLMConfig.provider == provider,
            ProjectLLMConfig.is_active == True,
        ).first() is not None
        has_system_key = bool(_resolve_system_key(db, provider))
        if has_project_key or has_system_key:
            accessible_providers.add(provider)

    all_models = []
    # Chat models — only accessible providers
    for provider, models_list in AVAILABLE_MODELS.items():
        if provider in accessible_providers:
            for m in models_list:
                all_models.append(AvailableModel(provider=provider, **m))

    # Embedding models — only accessible providers
    for model_id, info in EMBEDDING_MODELS.items():
        if info["provider"] in accessible_providers:
            all_models.append(AvailableModel(
                provider=info["provider"],
                model_id=model_id,
                display_name=info["display_name"],
                type="embedding",
                dimensions=info["dimensions"],
            ))

    return AvailableModelsResponse(models=all_models)


@router.get("/{provider}/models/discover")
def discover_provider_models(
    project_id: int,
    provider: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Discover available models from a provider's API using the stored key."""
    from app.schemas.llm_config import DiscoveredModel as DiscoveredModelSchema, DiscoverModelsResponse
    from app.services.chatbot.model_discovery import discover_models

    _get_project(db, project_id, user)

    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Invalid provider: {provider}")

    # Try project key first
    config = db.query(ProjectLLMConfig).filter(
        ProjectLLMConfig.project_id == project_id,
        ProjectLLMConfig.provider == provider,
    ).first()

    api_key = None
    if config and config.api_key_encrypted:
        try:
            api_key = decrypt_value(config.api_key_encrypted)
        except Exception:
            pass

    # Fall back to system key
    if not api_key:
        api_key = _resolve_system_key(db, provider)

    if not api_key:
        # No key available — return static list
        static_models = AVAILABLE_MODELS.get(provider, [])
        return DiscoverModelsResponse(
            provider=provider,
            models=[DiscoveredModelSchema(model_id=m["model_id"], display_name=m["display_name"], model_type=m.get("type", "chat")) for m in static_models],
            from_api=False,
        )

    discovered = discover_models(provider, api_key)
    if not discovered:
        # Discovery failed — return static list
        static_models = AVAILABLE_MODELS.get(provider, [])
        return DiscoverModelsResponse(
            provider=provider,
            models=[DiscoveredModelSchema(model_id=m["model_id"], display_name=m["display_name"], model_type=m.get("type", "chat")) for m in static_models],
            from_api=False,
        )

    return DiscoverModelsResponse(
        provider=provider,
        models=[DiscoveredModelSchema(model_id=m.model_id, display_name=m.display_name, model_type=m.model_type, owned_by=m.owned_by) for m in discovered],
        from_api=True,
    )


@router.get("/system-status", response_model=SystemStatusResponse)
def get_system_status(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Check which system LLM providers are available (have system keys)."""
    _get_project(db, project_id, user)

    providers = []
    for provider in ["openai", "anthropic", "google"]:
        key = _resolve_system_key(db, provider)
        has_limits = False
        if key:
            try:
                from app.models import SystemLLMProvider
                sys_prov = db.query(SystemLLMProvider).filter(
                    SystemLLMProvider.provider == provider,
                    SystemLLMProvider.is_active == True,
                ).first()
                if sys_prov and (sys_prov.rate_limit_rpm or sys_prov.rate_limit_tpm):
                    has_limits = True
            except Exception:
                pass
        providers.append(SystemProviderStatus(
            provider=provider,
            available=bool(key),
            has_rate_limits=has_limits,
        ))

    return SystemStatusResponse(providers=providers)
