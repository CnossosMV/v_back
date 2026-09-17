"""
LangGraph Workflows for Event Actions

This module contains pre-built LangGraph workflows for complex
multi-step automation scenarios.

Workflows:
- abandoned_cart: Recovery sequence for abandoned carts
- onboarding: User onboarding drip campaign (TODO)
- re_engagement: Re-engage inactive users (TODO)

These workflows are executed via the `run_graph` action type.
"""
from .abandoned_cart import abandoned_cart_workflow, CartRecoveryState

# Registry of available workflows
WORKFLOW_REGISTRY = {
    "abandoned_cart": abandoned_cart_workflow,
}

__all__ = [
    "abandoned_cart_workflow",
    "CartRecoveryState",
    "WORKFLOW_REGISTRY",
]
