"""
Router Service

Routes incoming messages to the appropriate specialist agent using
LLM-based classification, rule-based matching, or a hybrid approach.
"""

import os
import json
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass

from app.services.chatbot.llm_key_resolver import create_chat_llm
from sqlalchemy.orm import Session

from app.models import RouterConfig, RoutingRule, SpecialistAgent

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """Result of a routing decision."""
    agent_id: int
    agent_name: str
    confidence: float
    reasoning: str
    mode: str  # auto, rules, hybrid
    matched_rule_id: Optional[int] = None


class RouterService:
    """Service for routing messages to specialist agents."""

    def __init__(self, db: Session):
        self.db = db

    def route_message(
        self,
        router_config: RouterConfig,
        specialists: List[SpecialistAgent],
        message: str,
        conversation_context: Optional[str] = None,
        current_agent_id: Optional[int] = None,
    ) -> RoutingDecision:
        """
        Route a message to the appropriate specialist.

        Args:
            router_config: Router configuration
            specialists: List of available specialists
            message: User message to route
            conversation_context: Rolling conversation summary
            current_agent_id: Currently active agent ID (for mid-convo switching)

        Returns:
            RoutingDecision with target agent and metadata
        """
        if not specialists:
            raise ValueError("No specialist agents available for routing")

        # Single specialist — no routing needed
        if len(specialists) == 1:
            agent = specialists[0]
            return RoutingDecision(
                agent_id=agent.id,
                agent_name=agent.name,
                confidence=1.0,
                reasoning="Single specialist — routed directly",
                mode="direct",
            )

        mode = router_config.routing_mode

        if mode == "rules":
            return self._route_by_rules(router_config, specialists, message)
        elif mode == "auto":
            return self._route_by_llm(
                router_config, specialists, message,
                conversation_context, current_agent_id,
            )
        elif mode == "hybrid":
            return self._route_hybrid(
                router_config, specialists, message,
                conversation_context, current_agent_id,
            )
        else:
            # Fallback to default agent or first specialist
            return self._fallback_decision(router_config, specialists)

    def _route_by_rules(
        self,
        router_config: RouterConfig,
        specialists: List[SpecialistAgent],
        message: str,
    ) -> RoutingDecision:
        """Route using keyword-based rules ordered by priority."""
        rules = (
            self.db.query(RoutingRule)
            .filter(
                RoutingRule.router_id == router_config.id,
                RoutingRule.is_active == True,
            )
            .order_by(RoutingRule.priority.asc())
            .all()
        )

        specialist_map = {s.id: s for s in specialists}
        message_lower = message.lower()

        for rule in rules:
            if rule.specialist_id not in specialist_map:
                continue

            # Check keyword hints
            if rule.keyword_hints:
                for keyword in rule.keyword_hints:
                    if keyword.lower() in message_lower:
                        agent = specialist_map[rule.specialist_id]
                        return RoutingDecision(
                            agent_id=agent.id,
                            agent_name=agent.name,
                            confidence=0.8,
                            reasoning=f"Keyword match: '{keyword}' → rule: {rule.description}",
                            mode="rules",
                            matched_rule_id=rule.id,
                        )

        # No rule matched — fall back to default
        return self._fallback_decision(router_config, specialists)

    def _route_by_llm(
        self,
        router_config: RouterConfig,
        specialists: List[SpecialistAgent],
        message: str,
        conversation_context: Optional[str] = None,
        current_agent_id: Optional[int] = None,
    ) -> RoutingDecision:
        """Route using LLM classification."""
        from app.services.chatbot.llm_key_resolver import resolve_llm
        from app.models import AgentTeam

        # Resolve LLM config for routing purpose
        team = self.db.query(AgentTeam).filter(AgentTeam.id == router_config.team_id).first()
        project_id = team.project_id if team else 0
        try:
            llm_cfg = resolve_llm(self.db, project_id, "routing")
        except ValueError:
            logger.warning("No LLM key available for routing, falling back to default agent")
            return self._fallback_decision(router_config, specialists)

        # Build agent descriptions for LLM
        agent_descriptions = []
        specialist_map = {}
        for agent in specialists:
            specialist_map[agent.id] = agent
            desc = f"- Agent ID: {agent.id} | Name: {agent.name}"
            if agent.description:
                desc += f" | Description: {agent.description}"

            # Include routing rules as additional context
            rules = (
                self.db.query(RoutingRule)
                .filter(
                    RoutingRule.router_id == router_config.id,
                    RoutingRule.specialist_id == agent.id,
                    RoutingRule.is_active == True,
                )
                .all()
            )
            if rules:
                rule_descs = [r.description for r in rules]
                desc += f" | Routing rules: {'; '.join(rule_descs)}"

            agent_descriptions.append(desc)

        agents_text = "\n".join(agent_descriptions)

        context_block = ""
        if conversation_context:
            context_block = f"\nConversation context so far:\n{conversation_context}\n"

        current_agent_block = ""
        if current_agent_id and current_agent_id in specialist_map:
            current_name = specialist_map[current_agent_id].name
            current_agent_block = (
                f"\nThe user is currently talking to: {current_name} (ID: {current_agent_id}). "
                "Only switch if the message clearly requires a different specialist.\n"
            )

        system_prompt = (
            "You are a message router for a multi-agent system. "
            "Your job is to classify the user's message and decide which specialist agent should handle it.\n\n"
            f"Available agents:\n{agents_text}\n"
            f"{context_block}"
            f"{current_agent_block}\n"
            "Respond with ONLY a JSON object (no markdown, no explanation):\n"
            '{"agent_id": <int>, "confidence": <float 0-1>, "reasoning": "<brief explanation>"}'
        )

        try:
            llm = create_chat_llm(llm_cfg, max_tokens=200)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ]

            response = llm.invoke(messages)
            result = self._parse_llm_routing_response(response.content, specialist_map)

            if result:
                return RoutingDecision(
                    agent_id=result["agent_id"],
                    agent_name=specialist_map[result["agent_id"]].name,
                    confidence=result["confidence"],
                    reasoning=result["reasoning"],
                    mode="auto",
                )

        except Exception as e:
            logger.error(f"LLM routing failed: {e}")

        return self._fallback_decision(router_config, specialists)

    def _route_hybrid(
        self,
        router_config: RouterConfig,
        specialists: List[SpecialistAgent],
        message: str,
        conversation_context: Optional[str] = None,
        current_agent_id: Optional[int] = None,
    ) -> RoutingDecision:
        """Route using rules first, then LLM fallback if no rule matches."""
        rules_result = self._route_by_rules(router_config, specialists, message)

        # If rules found a match (not a fallback), use it
        if rules_result.matched_rule_id is not None:
            rules_result.mode = "hybrid"
            return rules_result

        # Fall back to LLM
        llm_result = self._route_by_llm(
            router_config, specialists, message,
            conversation_context, current_agent_id,
        )
        llm_result.mode = "hybrid"
        return llm_result

    def _fallback_decision(
        self,
        router_config: RouterConfig,
        specialists: List[SpecialistAgent],
    ) -> RoutingDecision:
        """Return the default agent or first specialist as fallback."""
        specialist_map = {s.id: s for s in specialists}

        # Try default agent
        if router_config.default_agent_id and router_config.default_agent_id in specialist_map:
            agent = specialist_map[router_config.default_agent_id]
            return RoutingDecision(
                agent_id=agent.id,
                agent_name=agent.name,
                confidence=0.5,
                reasoning="Fallback to default agent",
                mode="fallback",
            )

        # Try is_default specialist
        for agent in specialists:
            if agent.is_default:
                return RoutingDecision(
                    agent_id=agent.id,
                    agent_name=agent.name,
                    confidence=0.5,
                    reasoning="Fallback to default specialist",
                    mode="fallback",
                )

        # Last resort: first specialist
        agent = specialists[0]
        return RoutingDecision(
            agent_id=agent.id,
            agent_name=agent.name,
            confidence=0.3,
            reasoning="Fallback to first specialist (no default configured)",
            mode="fallback",
        )

    def _parse_llm_routing_response(
        self,
        response_text: str,
        specialist_map: Dict[int, SpecialistAgent],
    ) -> Optional[Dict]:
        """Parse LLM JSON response for routing decision."""
        try:
            # Strip markdown code fences if present
            text = response_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                text = text.rsplit("```", 1)[0]
            text = text.strip()

            result = json.loads(text)
            agent_id = int(result.get("agent_id", 0))
            confidence = float(result.get("confidence", 0.5))
            reasoning = str(result.get("reasoning", ""))

            if agent_id in specialist_map:
                return {
                    "agent_id": agent_id,
                    "confidence": min(max(confidence, 0.0), 1.0),
                    "reasoning": reasoning,
                }

            logger.warning(f"LLM returned unknown agent_id: {agent_id}")
            return None

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error(f"Failed to parse LLM routing response: {e} — raw: {response_text[:200]}")
            return None
