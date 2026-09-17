"""
Model Discovery Service

Queries provider APIs to discover available models using the stored API key.
Falls back to static lists when discovery fails.
"""
import logging
from typing import List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredModel:
    model_id: str
    display_name: str
    model_type: str  # "chat" or "embedding"
    owned_by: Optional[str] = None


def discover_models(provider: str, api_key: str) -> List[DiscoveredModel]:
    """
    Discover available models from a provider's API.

    Args:
        provider: "openai", "anthropic", or "google"
        api_key: Decrypted API key for the provider

    Returns:
        List of discovered models, or empty list on failure.
    """
    try:
        if provider == "openai":
            return _discover_openai(api_key)
        elif provider == "anthropic":
            return _discover_anthropic(api_key)
        elif provider == "google":
            return _discover_google(api_key)
        else:
            logger.warning(f"Unknown provider for discovery: {provider}")
            return []
    except Exception as e:
        logger.warning(f"Model discovery failed for provider '{provider}': {e}")
        return []


def _discover_openai(api_key: str) -> List[DiscoveredModel]:
    """Discover models from OpenAI API."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=10)
    response = client.models.list()

    models = []
    for m in response.data:
        mid = m.id
        # Chat models
        if mid.startswith("gpt-") or mid.startswith("o1") or mid.startswith("o3") or mid.startswith("o4"):
            # Skip snapshot/dated versions when base exists (keep latest)
            display = mid.replace("-", " ").title()
            models.append(DiscoveredModel(
                model_id=mid,
                display_name=display,
                model_type="chat",
                owned_by=m.owned_by,
            ))
        # Embedding models
        elif mid.startswith("text-embedding-"):
            display = mid.replace("-", " ").title()
            models.append(DiscoveredModel(
                model_id=mid,
                display_name=display,
                model_type="embedding",
                owned_by=m.owned_by,
            ))

    # Sort: chat first, then embedding; alphabetically within each
    models.sort(key=lambda x: (0 if x.model_type == "chat" else 1, x.model_id))
    return models


def _discover_anthropic(api_key: str) -> List[DiscoveredModel]:
    """Discover models from Anthropic API."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=10)
        response = client.models.list()

        models = []
        for m in response.data:
            mid = m.id
            if mid.startswith("claude-"):
                display = m.display_name if hasattr(m, 'display_name') and m.display_name else mid.replace("-", " ").title()
                models.append(DiscoveredModel(
                    model_id=mid,
                    display_name=display,
                    model_type="chat",
                ))
        models.sort(key=lambda x: x.model_id)
        return models
    except ImportError:
        logger.warning("anthropic SDK not available for model discovery")
        return []
    except Exception as e:
        logger.warning(f"Anthropic model discovery failed: {e}")
        return []


def _discover_google(api_key: str) -> List[DiscoveredModel]:
    """Discover models from Google Generative AI API."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)

        models = []
        for m in genai.list_models():
            name = m.name  # "models/gemini-pro" format
            mid = name.replace("models/", "")
            if "generateContent" in (m.supported_generation_methods or []):
                models.append(DiscoveredModel(
                    model_id=mid,
                    display_name=m.display_name or mid.replace("-", " ").title(),
                    model_type="chat",
                ))
            elif "embedContent" in (m.supported_generation_methods or []):
                models.append(DiscoveredModel(
                    model_id=mid,
                    display_name=m.display_name or mid.replace("-", " ").title(),
                    model_type="embedding",
                ))
        models.sort(key=lambda x: (0 if x.model_type == "chat" else 1, x.model_id))
        return models
    except ImportError:
        logger.warning("google-generativeai SDK not available for model discovery")
        return []
    except Exception as e:
        logger.warning(f"Google model discovery failed: {e}")
        return []
