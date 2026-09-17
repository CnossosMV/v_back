"""Funnel Graph Introspector — stateless analysis of funnel step graph."""

import logging
from typing import Dict, Any, List, Set

from sqlalchemy.orm import Session

from app.models import Funnel, FunnelStep

logger = logging.getLogger(__name__)


class FunnelIntrospector:
    def __init__(self, db: Session):
        self.db = db

    def introspect(self, funnel_id: int) -> Dict[str, Any]:
        """Traverse all FunnelStep records and return a manifest of referenced resources."""
        funnel = self.db.query(Funnel).filter(Funnel.id == funnel_id).first()
        if not funnel:
            return {}

        steps = self.db.query(FunnelStep).filter(
            FunnelStep.funnel_id == funnel_id,
        ).order_by(FunnelStep.position).all()

        event_names: Set[str] = set()
        condition_fields: Set[str] = set()
        webhook_urls: Set[str] = set()
        tags_assigned: Set[str] = set()
        tags_checked: Set[str] = set()
        template_ids: Set[int] = set()
        wait_steps: List[Dict[str, Any]] = []
        wait_until_events: Set[str] = set()
        api_connections: List[Dict[str, Any]] = []

        # Extract from trigger config
        trigger_cfg = funnel.trigger_config or {}
        if trigger_cfg.get("event_name"):
            event_names.add(trigger_cfg["event_name"])

        # Extract from global exit config
        exit_cfg = funnel.global_exit_config or {}
        for goal_evt in exit_cfg.get("goal_events", []):
            if isinstance(goal_evt, str):
                event_names.add(goal_evt)
            elif isinstance(goal_evt, dict) and goal_evt.get("event_name"):
                event_names.add(goal_evt["event_name"])

        for step in steps:
            config = step.step_config or {}
            step_type = step.step_type

            if step_type == "action":
                action_type = config.get("action_type", "")
                action_cfg = config.get("config", {})

                # Template references
                tpl_id = action_cfg.get("template_id")
                if tpl_id:
                    template_ids.add(int(tpl_id))

                # Tag operations
                tag = action_cfg.get("tag")
                if tag:
                    if action_type in ("add_tag", "assign_tag"):
                        tags_assigned.add(tag)
                    elif action_type in ("remove_tag",):
                        tags_checked.add(tag)

                # Webhook URLs
                webhook_url = action_cfg.get("webhook_url") or action_cfg.get("url")
                if webhook_url:
                    webhook_urls.add(webhook_url)

                # API connections
                conn_id = action_cfg.get("connection_id")
                endpoint_slug = action_cfg.get("endpoint_slug")
                if conn_id:
                    api_connections.append({
                        "connection_id": conn_id,
                        "endpoint_slug": endpoint_slug,
                        "step_id": step.id,
                        "step_position": step.position,
                    })

            elif step_type == "condition":
                conditions = config.get("conditions", [])
                for cond in conditions:
                    field = cond.get("field")
                    if field:
                        condition_fields.add(field)
                    # Check for tag conditions
                    if cond.get("field") == "tags" or "tag" in str(cond.get("field", "")):
                        val = cond.get("value")
                        if val:
                            tags_checked.add(str(val))

            elif step_type == "wait":
                wait_steps.append({
                    "step_id": step.id,
                    "position": step.position,
                    "branch": step.branch,
                    "duration": config.get("duration"),
                    "unit": config.get("unit", "hours"),
                })

            elif step_type == "wait_until":
                exit_conditions = config.get("exit_conditions", [])
                for ec in exit_conditions:
                    evt = ec.get("event_name")
                    if evt:
                        wait_until_events.add(evt)
                        event_names.add(evt)

            elif step_type == "send_message":
                tpl_name = config.get("template_name")
                if tpl_name:
                    # Store template name as string reference
                    pass
                instance_id = config.get("instance_id")
                # Also extract webhook/api call if present in send config
                webhook_url = config.get("webhook_url")
                if webhook_url:
                    webhook_urls.add(webhook_url)

            elif step_type == "wait_for_reply":
                # Check for milestone events
                milestones = config.get("milestones", [])
                for ms in milestones:
                    evt = ms.get("event_name")
                    if evt:
                        event_names.add(evt)

        return {
            "funnel_id": funnel_id,
            "funnel_name": funnel.name,
            "funnel_status": funnel.status,
            "step_count": len(steps),
            "event_names": sorted(event_names),
            "condition_fields": sorted(condition_fields),
            "webhook_urls": sorted(webhook_urls),
            "tags_assigned": sorted(tags_assigned),
            "tags_checked": sorted(tags_checked),
            "template_ids": sorted(template_ids),
            "wait_steps": wait_steps,
            "wait_until_events": sorted(wait_until_events),
            "api_connections": api_connections,
        }
