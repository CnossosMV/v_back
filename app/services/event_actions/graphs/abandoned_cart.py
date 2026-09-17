"""
Abandoned Cart Recovery Workflow

A LangGraph workflow for recovering abandoned carts through
a multi-step reminder sequence with intelligent stop conditions.

Flow:
1. Wait 1 hour, send first reminder
2. Check if purchased - if yes, stop
3. Wait 24 hours, send second reminder
4. Check if purchased - if yes, stop
5. Wait 24 hours, send discount offer
"""
import logging
from typing import TypedDict, Annotated, Literal, Optional
from datetime import datetime
from langgraph.graph import StateGraph, END

logger = logging.getLogger(__name__)


class CartRecoveryState(TypedDict):
    """State for abandoned cart recovery workflow"""
    # Input
    project_id: int
    user_id: int
    event_id: int
    cart_value: float
    cart_items: list

    # Tracking
    reminder_1_sent: bool
    reminder_2_sent: bool
    discount_sent: bool
    purchased: bool

    # Output
    messages: list
    status: str


def check_if_purchased(state: CartRecoveryState) -> CartRecoveryState:
    """
    Check if the user has completed a purchase.
    This would query the database for recent purchase events.
    """
    # In real implementation, check database for purchase events
    # For now, return current state
    state["status"] = "checking_purchase"
    return state


def send_first_reminder(state: CartRecoveryState) -> CartRecoveryState:
    """
    Send the first reminder email (1 hour after cart abandonment).
    """
    state["reminder_1_sent"] = True
    state["messages"].append({
        "type": "reminder_1",
        "timestamp": datetime.utcnow().isoformat(),
        "message": "First reminder sent"
    })
    state["status"] = "reminder_1_sent"
    logger.info(f"Sent first reminder for user {state['user_id']}")
    return state


def send_second_reminder(state: CartRecoveryState) -> CartRecoveryState:
    """
    Send the second reminder email (24 hours after first).
    """
    state["reminder_2_sent"] = True
    state["messages"].append({
        "type": "reminder_2",
        "timestamp": datetime.utcnow().isoformat(),
        "message": "Second reminder sent"
    })
    state["status"] = "reminder_2_sent"
    logger.info(f"Sent second reminder for user {state['user_id']}")
    return state


def send_discount_offer(state: CartRecoveryState) -> CartRecoveryState:
    """
    Send a discount offer as final attempt.
    """
    state["discount_sent"] = True
    state["messages"].append({
        "type": "discount",
        "timestamp": datetime.utcnow().isoformat(),
        "message": "Discount offer sent"
    })
    state["status"] = "discount_sent"
    logger.info(f"Sent discount offer for user {state['user_id']}")
    return state


def mark_completed(state: CartRecoveryState) -> CartRecoveryState:
    """Mark workflow as completed successfully (purchase detected)."""
    state["status"] = "completed_purchased"
    state["messages"].append({
        "type": "completed",
        "timestamp": datetime.utcnow().isoformat(),
        "message": "User purchased - workflow complete"
    })
    return state


def mark_finished(state: CartRecoveryState) -> CartRecoveryState:
    """Mark workflow as finished (all reminders sent)."""
    state["status"] = "completed_all_sent"
    state["messages"].append({
        "type": "completed",
        "timestamp": datetime.utcnow().isoformat(),
        "message": "All reminders sent - workflow complete"
    })
    return state


def should_continue_after_check(state: CartRecoveryState) -> Literal["mark_completed", "continue"]:
    """Routing function after purchase check."""
    if state.get("purchased"):
        return "mark_completed"
    return "continue"


def route_after_reminder_1(state: CartRecoveryState) -> Literal["check_2", "mark_completed"]:
    """Routing after first reminder."""
    if state.get("purchased"):
        return "mark_completed"
    return "check_2"


def route_after_reminder_2(state: CartRecoveryState) -> Literal["send_discount", "mark_completed"]:
    """Routing after second reminder."""
    if state.get("purchased"):
        return "mark_completed"
    return "send_discount"


def create_abandoned_cart_workflow() -> StateGraph:
    """
    Create the abandoned cart recovery workflow.

    Returns:
        Compiled StateGraph workflow
    """
    workflow = StateGraph(CartRecoveryState)

    # Add nodes
    workflow.add_node("check_purchase_1", check_if_purchased)
    workflow.add_node("send_reminder_1", send_first_reminder)
    workflow.add_node("check_purchase_2", check_if_purchased)
    workflow.add_node("send_reminder_2", send_second_reminder)
    workflow.add_node("check_purchase_3", check_if_purchased)
    workflow.add_node("send_discount", send_discount_offer)
    workflow.add_node("mark_completed", mark_completed)
    workflow.add_node("mark_finished", mark_finished)

    # Set entry point
    workflow.set_entry_point("check_purchase_1")

    # Add conditional edges
    workflow.add_conditional_edges(
        "check_purchase_1",
        should_continue_after_check,
        {
            "mark_completed": "mark_completed",
            "continue": "send_reminder_1"
        }
    )

    workflow.add_edge("send_reminder_1", "check_purchase_2")

    workflow.add_conditional_edges(
        "check_purchase_2",
        should_continue_after_check,
        {
            "mark_completed": "mark_completed",
            "continue": "send_reminder_2"
        }
    )

    workflow.add_edge("send_reminder_2", "check_purchase_3")

    workflow.add_conditional_edges(
        "check_purchase_3",
        should_continue_after_check,
        {
            "mark_completed": "mark_completed",
            "continue": "send_discount"
        }
    )

    workflow.add_edge("send_discount", "mark_finished")
    workflow.add_edge("mark_completed", END)
    workflow.add_edge("mark_finished", END)

    return workflow.compile()


# Compiled workflow instance
abandoned_cart_workflow = create_abandoned_cart_workflow()
