"""
LLM Usage Tracker

Records token usage and cost estimates for every LLM API call.
Called after each LLM invocation to build billing/analytics data.
"""
import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Cost per 1K tokens (USD) — updated as of 2026-03
COST_PER_1K_TOKENS = {
    # OpenAI chat
    "gpt-4o-mini":    {"input": 0.00015, "output": 0.0006},
    "gpt-4o":         {"input": 0.0025,  "output": 0.01},
    "gpt-4-turbo":    {"input": 0.01,    "output": 0.03},
    "gpt-3.5-turbo":  {"input": 0.0005,  "output": 0.0015},
    # Anthropic
    "claude-sonnet-4-5-20250929": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5-20251001":  {"input": 0.0008, "output": 0.004},
    # Google
    "gemini-pro":        {"input": 0.00025, "output": 0.0005},
    "gemini-pro-vision": {"input": 0.00025, "output": 0.0005},
    # Embeddings
    "text-embedding-3-small": {"input": 0.00002, "output": 0.0},
    "text-embedding-3-large": {"input": 0.00013, "output": 0.0},
    "text-embedding-ada-002": {"input": 0.0001,  "output": 0.0},
    "text-embedding-004":     {"input": 0.00001, "output": 0.0},
    # Audio
    "whisper-1": {"input": 0.006, "output": 0.0},  # per minute, approximate
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD based on model and token counts."""
    rates = COST_PER_1K_TOKENS.get(model)
    if not rates:
        return 0.0
    return (input_tokens / 1000.0 * rates["input"]) + (output_tokens / 1000.0 * rates["output"])


def record_llm_usage(
    db: Session,
    project_id: int,
    provider: str,
    model: str,
    purpose: str,
    key_source: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """
    Record a single LLM API call for usage tracking.

    Args:
        db: Database session
        project_id: Project that made the call
        provider: LLM provider (openai, anthropic, google)
        model: Model used (gpt-4o-mini, etc.)
        purpose: What the call was for (chat, routing, guardrails, embeddings, etc.)
        key_source: Whether project's own key or system key was used
        input_tokens: Number of input/prompt tokens
        output_tokens: Number of output/completion tokens
    """
    try:
        from app.models import LLMUsageRecord

        total = input_tokens + output_tokens
        cost = _estimate_cost(model, input_tokens, output_tokens)

        record = LLMUsageRecord(
            project_id=project_id,
            provider=provider,
            model=model,
            purpose=purpose,
            key_source=key_source,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            cost_estimate=cost,
        )
        db.add(record)
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to record LLM usage: {e}")
        try:
            db.rollback()
        except Exception:
            pass


def track_langchain_response(
    db: Session,
    project_id: int,
    provider: str,
    model: str,
    purpose: str,
    key_source: str,
    response,
) -> None:
    """
    Extract token usage from a LangChain response and record it.
    Works with ChatOpenAI, ChatAnthropic, ChatGoogleGenerativeAI responses.
    """
    input_tokens = 0
    output_tokens = 0

    try:
        metadata = getattr(response, "response_metadata", {}) or {}

        # OpenAI format
        token_usage = metadata.get("token_usage", {})
        if token_usage:
            input_tokens = token_usage.get("prompt_tokens", 0)
            output_tokens = token_usage.get("completion_tokens", 0)
        else:
            # Anthropic format
            usage = metadata.get("usage", {})
            if usage:
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
    except Exception as e:
        logger.debug(f"Could not extract token usage from response: {e}")

    record_llm_usage(db, project_id, provider, model, purpose, key_source, input_tokens, output_tokens)


def track_openai_response(
    db: Session,
    project_id: int,
    provider: str,
    model: str,
    purpose: str,
    key_source: str,
    response,
) -> None:
    """
    Extract token usage from a direct OpenAI SDK response and record it.
    """
    input_tokens = 0
    output_tokens = 0

    try:
        usage = getattr(response, "usage", None)
        if usage:
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0
    except Exception as e:
        logger.debug(f"Could not extract token usage from OpenAI response: {e}")

    record_llm_usage(db, project_id, provider, model, purpose, key_source, input_tokens, output_tokens)


def track_embedding_usage(
    db: Session,
    project_id: int,
    provider: str,
    model: str,
    key_source: str,
    token_count: int = 0,
) -> None:
    """
    Record embedding API usage. Embeddings only have input tokens.
    """
    record_llm_usage(db, project_id, provider, model, "embeddings", key_source, input_tokens=token_count, output_tokens=0)
