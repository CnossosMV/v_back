"""
Orchestration Engine

Core runtime for multi-agent teams. Processes incoming messages through:
1. Session management
2. Router (LLM / rules / hybrid)
3. Context handoff on agent switch
4. Guardrail checks
5. Specialist response generation (RAG)
6. Session state and context updates
"""

import os
import time
import logging
from typing import Dict, Optional, List, Tuple
from dataclasses import asdict

from sqlalchemy.orm import Session

from app.models import (
    AgentTeam, SpecialistAgent, RouterConfig, ChatSession,
    ChatMessage, TeamSessionEvent, SpecialistTool,
)
from .conversation_manager import ConversationManager
from .router_service import RouterService, RoutingDecision
from .guardrail_service import GuardrailService, GuardrailResult
from .langchain_service import LangChainService
from .vector_store import VectorStoreService
from .knowledge_loader import KnowledgeLoader
from .tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

# Summary interval: generate rolling summary every N user messages
SUMMARY_INTERVAL = 5


class OrchestrationEngine:
    """Core engine for processing messages through a multi-agent team."""

    def __init__(self, db: Session):
        self.db = db
        self.conversation_manager = ConversationManager(db)
        self.router_service = RouterService(db)
        self.guardrail_service = GuardrailService(db)

    def process_message(
        self,
        team_id: int,
        message: str,
        user_identifier: str,
        channel: str = "web",
        session_id: Optional[int] = None,
        user_id: Optional[int] = None,
        context: Optional[Dict] = None,
        content_pieces: Optional[List[Dict]] = None,
    ) -> Dict:
        """
        Process a user message through the orchestration pipeline.

        Args:
            team_id: Agent team ID
            message: User message text
            user_identifier: Unique user identifier
            channel: Communication channel (web, whatsapp, api)
            session_id: Existing session ID (optional)
            user_id: Authenticated user ID (optional)
            context: Additional context data (optional)
            content_pieces: Optional list of content piece dicts with media info

        Returns:
            Dict with: response, session_id, images, debug
        """
        start_time = time.time()

        # 1. Load team and validate
        team = self.db.query(AgentTeam).filter(AgentTeam.id == team_id).first()
        if not team:
            raise ValueError(f"Agent team {team_id} not found")
        if team.status != "active":
            raise ValueError(f"Agent team {team_id} is not active (status: {team.status})")

        # Load specialists and router config
        specialists = (
            self.db.query(SpecialistAgent)
            .filter(
                SpecialistAgent.team_id == team_id,
                SpecialistAgent.status == "active",
            )
            .order_by(SpecialistAgent.agent_order.asc())
            .all()
        )
        if not specialists:
            raise ValueError(f"Team {team_id} has no active specialists")

        router_config = (
            self.db.query(RouterConfig)
            .filter(RouterConfig.team_id == team_id)
            .first()
        )
        if not router_config:
            raise ValueError(f"Team {team_id} has no router configuration")

        # 2. Get or create session
        if session_id:
            session = self.conversation_manager.get_session_by_id(session_id)
            if not session or not session.is_active:
                session = None

        if not session_id or not locals().get("session"):
            session = self.conversation_manager.get_or_create_team_session(
                team_id=team_id,
                user_identifier=user_identifier,
                channel=channel,
                user_id=user_id,
                context_data=context,
            )

        # Update session state
        self.conversation_manager.update_session_state(session.id, "ACTIVE")

        # 2b. Enrich context from external APIs (if configured)
        self._enrich_context(team, session, user_identifier)

        # 3. Store user message
        self.conversation_manager.add_team_message(
            session_id=session.id,
            role="user",
            content=message,
            agent_id=session.current_agent_id,
        )

        # 4. Route message
        self.conversation_manager.update_session_state(session.id, "ROUTING")

        routing_decision = self.router_service.route_message(
            router_config=router_config,
            specialists=specialists,
            message=message,
            conversation_context=session.context_summary,
            current_agent_id=session.current_agent_id,
        )

        # 5. Handle agent switch
        previous_agent_id = session.current_agent_id
        agent_switched = (
            previous_agent_id is not None
            and previous_agent_id != routing_decision.agent_id
        )

        if agent_switched and not router_config.allow_mid_convo_switch:
            # Mid-conversation switching disabled — keep current agent
            specialist_map = {s.id: s for s in specialists}
            if previous_agent_id in specialist_map:
                current = specialist_map[previous_agent_id]
                routing_decision = RoutingDecision(
                    agent_id=current.id,
                    agent_name=current.name,
                    confidence=1.0,
                    reasoning="Mid-conversation switching disabled",
                    mode="override",
                )
                agent_switched = False

        target_specialist = None
        for s in specialists:
            if s.id == routing_decision.agent_id:
                target_specialist = s
                break

        if not target_specialist:
            raise ValueError(f"Routed specialist {routing_decision.agent_id} not found")

        if agent_switched:
            self._handle_agent_switch(
                session=session,
                previous_agent_id=previous_agent_id,
                new_agent=target_specialist,
                routing_decision=routing_decision,
            )

        # Update current agent
        self.conversation_manager.set_current_agent(session.id, target_specialist.id)

        # 6. Check guardrails
        guardrail_result = self.guardrail_service.check_all(
            specialist=target_specialist,
            session=session,
            message=message,
        )

        if not guardrail_result.passed:
            return self._handle_guardrail_triggered(
                session=session,
                specialist=target_specialist,
                guardrail_result=guardrail_result,
                routing_decision=routing_decision,
                start_time=start_time,
            )

        # 7. Generate specialist response
        self.conversation_manager.update_session_state(session.id, "ACTIVE")

        # Build media-enriched message for the LLM
        llm_message = message
        if content_pieces:
            media_parts = []
            for cp in content_pieces:
                rt = cp.get("resolved_text")
                ctype = cp.get("type", "text")
                if rt and ctype != "text":
                    media_parts.append(f"[{ctype.upper()} content: {rt}]")
            if media_parts:
                llm_message = "\n".join(media_parts) + "\n" + message

        response_text, images, response_metadata = self._generate_specialist_response(
            specialist=target_specialist,
            session=session,
            message=llm_message,
        )

        # 8. Store assistant message
        routing_decision_dict = asdict(routing_decision)
        token_usage = response_metadata.get("tokens_used", {})

        self.conversation_manager.add_team_message(
            session_id=session.id,
            role="assistant",
            content=response_text,
            agent_id=target_specialist.id,
            routing_decision=routing_decision_dict,
            images=images,
            retrieved_documents=response_metadata.get("retrieved_documents", []),
            retrieval_metadata=response_metadata,
            token_usage=token_usage,
        )

        # 9. Update rolling summary periodically
        self._maybe_update_summary(session)

        processing_time = time.time() - start_time

        # 10. Build response
        debug_info = {
            "router_decision": routing_decision_dict,
            "active_agent": {
                "id": target_specialist.id,
                "name": target_specialist.name,
                "icon": target_specialist.icon,
            },
            "agent_switched": agent_switched,
            "knowledge_used": response_metadata.get("retrieved_documents", []),
            "confidence": routing_decision.confidence,
            "tokens": token_usage,
            "context_summary": session.context_summary,
            "processing_time": processing_time,
        }

        return {
            "response": response_text,
            "session_id": session.id,
            "images": images,
            "debug": debug_info,
        }

    def _generate_specialist_response(
        self,
        specialist: SpecialistAgent,
        session: ChatSession,
        message: str,
    ) -> Tuple[str, List[str], Dict]:
        """
        Generate a response using the specialist's configuration and knowledge.
        If the specialist has active tools, uses LLM tool-calling.

        Returns:
            Tuple of (response_text, images, metadata)
        """
        from app.services.chatbot.llm_key_resolver import resolve_llm

        # Resolve LLM config: specialist override > project own_key > system
        team = self.db.query(AgentTeam).filter(AgentTeam.id == specialist.team_id).first()
        project_id = team.project_id if team else 0
        llm_cfg = resolve_llm(
            self.db, project_id, "chat",
            specialist_model_name=specialist.model_name,
            specialist_temperature=specialist.temperature,
        )
        openai_api_key = llm_cfg.api_key

        vector_store_path = os.getenv("VECTOR_STORE_PATH", "/app/data/vector_stores")

        # Get conversation history
        history = self.conversation_manager.get_conversation_history(
            session_id=session.id,
            limit=20,
        )

        # Build system prompt with specialist persona
        system_prompt = self._build_specialist_system_prompt(specialist, session)

        # Inject media asset catalog into system prompt
        try:
            from app.services.media_asset_service import MediaAssetService
            asset_catalog = MediaAssetService(self.db).build_asset_catalog(
                project_id=project_id,
                specialist_id=specialist.id,
            )
            if asset_catalog:
                system_prompt = (system_prompt + "\n\n" + asset_catalog).strip()
        except Exception as e:
            logger.warning(f"Failed to build asset catalog: {e}")

        # Inject knowledge library context if specialist/team has collection bindings
        try:
            from app.services.knowledge.retrieval_service import RetrievalService
            from app.services.chatbot.llm_key_resolver import resolve_llm as _rl
            import os

            _oai_key = openai_api_key
            retrieval = RetrievalService(self.db, _oai_key)

            # Check specialist-level bindings first, then team-level
            lib_results = retrieval.retrieve_for_rag(
                project_id=project_id,
                query=message,
                consumer_type="specialist",
                consumer_id=specialist.id,
                k=5,
                min_score=0.3,
            )
            if not lib_results.get("results"):
                lib_results = retrieval.retrieve_for_rag(
                    project_id=project_id,
                    query=message,
                    consumer_type="agent_team",
                    consumer_id=team.id,
                    k=5,
                    min_score=0.3,
                )

            if lib_results.get("results"):
                lib_context_parts = []
                for i, r in enumerate(lib_results["results"], 1):
                    lib_context_parts.append(
                        f"[Library Source {i}] ({r['asset_name']})\n{r['chunk_text']}"
                    )
                lib_context = "\n---\n".join(lib_context_parts)
                system_prompt = (
                    system_prompt + "\n\nAdditional knowledge from library:\n" + lib_context
                ).strip()
        except Exception as e:
            logger.warning(f"Failed to inject knowledge library context: {e}")

        # Set up RAG services with specialist config (multi-provider embeddings + chat)
        from app.services.chatbot.llm_key_resolver import resolve_embeddings, create_embeddings
        try:
            emb_cfg = resolve_embeddings(self.db, project_id)
            embeddings = create_embeddings(emb_cfg)
        except ValueError:
            from langchain_openai import OpenAIEmbeddings
            embeddings = OpenAIEmbeddings(openai_api_key=openai_api_key, model="text-embedding-3-small")

        vector_store = VectorStoreService(vector_store_path, embeddings)
        knowledge_loader = KnowledgeLoader(vector_store_path)

        langchain_service = LangChainService(
            openai_api_key=llm_cfg.api_key,
            vector_store_service=vector_store,
            knowledge_loader=knowledge_loader,
            model_name=llm_cfg.model,
            temperature=llm_cfg.temperature,
            max_tokens=specialist.max_tokens or 2000,
        )

        # Check for active tools
        active_tools = (
            self.db.query(SpecialistTool)
            .filter(
                SpecialistTool.specialist_id == specialist.id,
                SpecialistTool.is_active == True,
            )
            .all()
        )

        if active_tools:
            # Build tool definitions for LLM
            tool_defs = self._build_tool_definitions(active_tools)
            tool_executor = ToolExecutor(self.db)

            def execute_tool_callback(tool_name: str, arguments: dict) -> dict:
                """Callback for LangChain to execute tools."""
                # Find the matching tool
                for t in active_tools:
                    if t.name == tool_name:
                        # Record event
                        event = TeamSessionEvent(
                            session_id=session.id,
                            event_type="tool_called",
                            event_data={
                                "tool_id": t.id,
                                "tool_name": tool_name,
                                "arguments": arguments,
                            },
                            agent_id=specialist.id,
                        )
                        self.db.add(event)
                        self.db.commit()

                        return tool_executor.execute_tool(
                            tool=t,
                            parameters=arguments,
                            session_id=session.id,
                            session_context={
                                "user_message": message,
                                "context_summary": session.context_summary or "",
                                **(session.extracted_entities or {}),
                            },
                        )
                return {"status": "error", "result": None, "error": f"Tool '{tool_name}' not found"}

            response_text, images, metadata = langchain_service.generate_response_with_tools(
                chatbot_id=specialist.id,
                query=message,
                conversation_history=history,
                system_prompt=system_prompt,
                knowledge_base_path=specialist.knowledge_base_path,
                tools=tool_defs,
                tool_executor_fn=execute_tool_callback,
            )
        else:
            # No tools — use standard RAG response
            response_text, images, metadata = langchain_service.generate_response(
                chatbot_id=specialist.id,
                query=message,
                conversation_history=history,
                system_prompt=system_prompt,
                knowledge_base_path=specialist.knowledge_base_path,
            )

        return response_text, images, metadata

    def _build_tool_definitions(self, tools: List[SpecialistTool]) -> List[Dict]:
        """Convert SpecialistTool models to LLM tool definitions."""
        defs = []
        for tool in tools:
            tool_def = {
                "name": tool.name,
                "description": tool.description or "",
            }

            # Add when_to_use to description
            if tool.when_to_use:
                tool_def["description"] += f"\nUse this tool when: {tool.when_to_use}"

            # Build parameters schema from config
            if tool.tool_type == "webhook":
                config = tool.config or {}
                body_template = config.get("body_template", {})
                # Try to infer parameters from body template
                if isinstance(body_template, dict):
                    properties = {}
                    for key, val in body_template.items():
                        if isinstance(val, str) and "{{" in val:
                            properties[key] = {"type": "string", "description": f"Value for {key}"}
                        else:
                            properties[key] = {"type": "string", "description": key}
                    tool_def["parameters"] = {
                        "type": "object",
                        "properties": properties,
                    }
                else:
                    tool_def["parameters"] = {"type": "object", "properties": {}}

            elif tool.tool_type == "mcp_server":
                # MCP tools should have inputSchema from discovery
                config = tool.config or {}
                tool_def["parameters"] = config.get("input_schema", {
                    "type": "object", "properties": {},
                })

            elif tool.tool_type == "api_connection":
                # API connection tools have parameters_schema from endpoint
                config = tool.config or {}
                tool_def["parameters"] = config.get("parameters_schema", {
                    "type": "object", "properties": {},
                })

            else:
                tool_def["parameters"] = {"type": "object", "properties": {}}

            defs.append(tool_def)

        return defs

    def _build_specialist_system_prompt(
        self,
        specialist: SpecialistAgent,
        session: ChatSession,
    ) -> str:
        """Build the system prompt incorporating specialist persona and context."""
        parts = []

        # Base system prompt
        if specialist.system_prompt:
            parts.append(specialist.system_prompt)

        # Tone directive
        tone_map = {
            "formal": "Use a professional and formal tone.",
            "friendly": "Use a warm, friendly, and approachable tone.",
            "direct": "Be concise and direct. Get straight to the point.",
            "empathetic": "Be empathetic and understanding. Show care for the user's feelings.",
        }
        if specialist.tone and specialist.tone in tone_map:
            parts.append(tone_map[specialist.tone])

        # Language directive — i18n: prefer the contact's locale over the specialist's
        # configured language (flag-gated; off ⇒ specialist.language as before).
        contact_locale = None
        try:
            from app.models import AgentTeam
            _team = self.db.query(AgentTeam).filter(AgentTeam.id == specialist.team_id).first()
            from app.services.engine_rollout_service import effective_mode
            if _team and effective_mode(self.db, _team.project_id, "locale_resolution") == "enforce":
                from app.models.messaging import MessagingUser
                _ident = getattr(session, "user_identifier", None)
                if _ident:
                    _c = self.db.query(MessagingUser).filter(
                        MessagingUser.project_id == _team.project_id,
                    ).filter(
                        (MessagingUser.external_id == _ident)
                        | (MessagingUser.phone == _ident)
                        | (MessagingUser.phone_e164 == _ident)
                        | (MessagingUser.email == _ident)
                    ).first()
                    contact_locale = _c.locale if _c else None
        except Exception:
            contact_locale = None
        if contact_locale:
            parts.append(f"Respond in the contact's language (locale {contact_locale}).")
        elif specialist.language:
            parts.append(f"Respond in {specialist.language}.")

        # Context handoff
        if session.context_summary:
            parts.append(
                f"\nConversation context (from previous interactions):\n"
                f"{session.context_summary}"
            )

        if session.extracted_entities:
            entities_str = ", ".join(
                f"{k}: {v}" for k, v in session.extracted_entities.items()
            )
            parts.append(f"Known information about the user: {entities_str}")

        # Include enriched context from API connections
        if session.context_data:
            import json
            for key, value in session.context_data.items():
                if isinstance(value, (dict, list)):
                    parts.append(f"External data ({key}):\n{json.dumps(value, indent=2, default=str)[:2000]}")
                elif value:
                    parts.append(f"External data ({key}): {str(value)[:1000]}")

        return "\n\n".join(parts)

    def _enrich_context(
        self,
        team: AgentTeam,
        session: ChatSession,
        user_identifier: str,
    ) -> None:
        """
        Fetch external data from configured API connections before processing.
        Reads team.context_sources and calls each endpoint, injecting results
        into session.context_data for use in system prompts.
        """
        sources = team.context_sources
        if not sources:
            return

        from app.services.api_connector_service import ApiConnectorService
        connector = ApiConnectorService(self.db)

        context_data = dict(session.context_data or {}) if session.context_data else {}

        for source in sources:
            connection_id = source.get("connection_id")
            endpoint_slug = source.get("endpoint_slug")
            inject_as = source.get("inject_as", "api_data")
            on_error = source.get("on_error", "skip")

            if not connection_id or not endpoint_slug:
                continue

            # Build parameter context
            param_vars = {
                "user_identifier": user_identifier,
                "session_id": str(session.id),
            }
            if session.extracted_entities:
                param_vars.update(session.extracted_entities)

            # Interpolate parameters
            raw_params = source.get("parameters", {})
            params = {}
            for k, v in raw_params.items():
                if isinstance(v, str):
                    import re
                    def replacer(match, ctx=param_vars):
                        key = match.group(1).strip()
                        return str(ctx.get(key, match.group(0)))
                    params[k] = re.sub(r'\{\{(\s*\w+\s*)\}\}', replacer, v)
                else:
                    params[k] = v

            try:
                result = connector.call_endpoint(
                    connection_id=connection_id,
                    endpoint_id_or_slug=endpoint_slug,
                    parameters=params,
                    trigger_source="context_enrichment",
                    session_id=session.id,
                )

                if result.get("status") == "success" and result.get("result") is not None:
                    context_data[inject_as] = result["result"]
                    logger.info(f"Context enrichment '{inject_as}' succeeded for session {session.id}")
                elif on_error == "fail":
                    logger.error(f"Context enrichment '{inject_as}' failed: {result.get('error')}")
                    raise ValueError(f"Required context source '{inject_as}' failed: {result.get('error')}")
                else:
                    logger.warning(f"Context enrichment '{inject_as}' failed (skipped): {result.get('error')}")

            except ValueError:
                raise
            except Exception as e:
                if on_error == "fail":
                    raise ValueError(f"Required context source '{inject_as}' failed: {str(e)}")
                logger.warning(f"Context enrichment '{inject_as}' error (skipped): {e}")

        # Inject user scores into context_data
        try:
            from app.services.scoring.scoring_engine import ScoringEngine
            from app.models.messaging import MessagingUser

            # Try to find user by identifier
            mu = self.db.query(MessagingUser).filter(
                MessagingUser.project_id == team.project_id,
                MessagingUser.external_id == user_identifier,
            ).first()
            if mu:
                snapshots = ScoringEngine(self.db).get_user_scores(team.project_id, mu.id)
                if snapshots:
                    user_scores = {}
                    for s in snapshots:
                        user_scores[s.score_definition.slug] = {
                            "score": s.score,
                            "tier": s.tier,
                            "name": s.score_definition.name,
                        }
                    context_data["user_scores"] = user_scores

                # Inject segment classification
                if mu.segment_name:
                    context_data["user_segment"] = {
                        "id": mu.segment_rule_id,
                        "name": mu.segment_name,
                    }
        except Exception as e:
            logger.warning(f"Error injecting scores into context: {e}")

        # Update session context_data
        if context_data:
            session.context_data = context_data
            self.db.commit()

    def _handle_agent_switch(
        self,
        session: ChatSession,
        previous_agent_id: int,
        new_agent: SpecialistAgent,
        routing_decision: RoutingDecision,
    ) -> None:
        """Handle context handoff when switching specialists."""
        # Record event
        event = TeamSessionEvent(
            session_id=session.id,
            event_type="agent_switch",
            event_data={
                "previous_agent_id": previous_agent_id,
                "new_agent_id": new_agent.id,
                "new_agent_name": new_agent.name,
                "routing_decision": asdict(routing_decision),
            },
            agent_id=new_agent.id,
        )
        self.db.add(event)
        self.db.commit()

        # Generate context summary for handoff
        self._update_context_summary(session)

        logger.info(
            f"Agent switch in session {session.id}: "
            f"{previous_agent_id} → {new_agent.id} ({new_agent.name})"
        )

    def _handle_guardrail_triggered(
        self,
        session: ChatSession,
        specialist: SpecialistAgent,
        guardrail_result: GuardrailResult,
        routing_decision: RoutingDecision,
        start_time: float,
    ) -> Dict:
        """Handle a guardrail being triggered."""
        # Record event
        event = TeamSessionEvent(
            session_id=session.id,
            event_type="guardrail_triggered",
            event_data={
                "checks": guardrail_result.triggered_checks,
                "action": guardrail_result.action,
                "details": guardrail_result.details,
            },
            agent_id=specialist.id,
        )
        self.db.add(event)

        # Handle action
        response_text = guardrail_result.message or "I'm unable to help with that right now."

        if guardrail_result.action == "escalate":
            self.conversation_manager.update_session_state(session.id, "ESCALATED")

            # Record escalation event
            escalation_event = TeamSessionEvent(
                session_id=session.id,
                event_type="escalation",
                event_data={
                    "reason": guardrail_result.triggered_checks,
                    "details": guardrail_result.details,
                },
                agent_id=specialist.id,
            )
            self.db.add(escalation_event)

        self.db.commit()

        # Store assistant message
        self.conversation_manager.add_team_message(
            session_id=session.id,
            role="assistant",
            content=response_text,
            agent_id=specialist.id,
            routing_decision=asdict(routing_decision),
            message_metadata={"guardrail_triggered": True},
        )

        processing_time = time.time() - start_time

        return {
            "response": response_text,
            "session_id": session.id,
            "images": [],
            "debug": {
                "router_decision": asdict(routing_decision),
                "active_agent": {
                    "id": specialist.id,
                    "name": specialist.name,
                    "icon": specialist.icon,
                },
                "guardrail_triggered": {
                    "checks": guardrail_result.triggered_checks,
                    "action": guardrail_result.action,
                    "details": guardrail_result.details,
                },
                "processing_time": processing_time,
            },
        }

    def _maybe_update_summary(self, session: ChatSession) -> None:
        """Update rolling summary every SUMMARY_INTERVAL user messages."""
        from sqlalchemy import func

        user_message_count = (
            self.db.query(func.count(ChatMessage.id))
            .filter(
                ChatMessage.session_id == session.id,
                ChatMessage.role == "user",
            )
            .scalar()
        ) or 0

        if user_message_count > 0 and user_message_count % SUMMARY_INTERVAL == 0:
            self._update_context_summary(session)

    def _update_context_summary(self, session: ChatSession) -> None:
        """Generate a rolling summary of the conversation."""
        from app.services.chatbot.llm_key_resolver import resolve_llm

        # Derive project_id from session's team
        project_id = 0
        if session.team_id:
            team = self.db.query(AgentTeam).filter(AgentTeam.id == session.team_id).first()
            if team:
                project_id = team.project_id

        try:
            llm_cfg = resolve_llm(self.db, project_id, "summary")
            openai_api_key = llm_cfg.api_key
        except ValueError:
            return

        # Get recent messages
        messages = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.session_id == session.id)
            .order_by(ChatMessage.timestamp.desc())
            .limit(20)
            .all()
        )

        if len(messages) < 3:
            return

        convo_text = "\n".join(
            f"{m.role}: {m.content[:300]}" for m in reversed(messages)
        )

        system_prompt = (
            "Summarize this conversation in 2-3 sentences. Focus on:\n"
            "1. The main topic/request\n"
            "2. Key information provided by the user (names, IDs, preferences)\n"
            "3. Current status of the request\n\n"
            "Also extract key entities as a JSON object at the end, on a new line starting with ENTITIES:\n"
            'Example: ENTITIES: {"name": "John", "plan": "premium", "issue": "billing"}'
        )

        try:
            from app.services.chatbot.llm_key_resolver import create_chat_llm

            llm = create_chat_llm(llm_cfg, max_tokens=300)

            response = llm.invoke([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": convo_text},
            ])

            result_text = response.content.strip()

            # Parse entities if present
            summary = result_text
            entities = None

            if "ENTITIES:" in result_text:
                parts = result_text.split("ENTITIES:", 1)
                summary = parts[0].strip()
                try:
                    import json
                    entities_text = parts[1].strip()
                    entities = json.loads(entities_text)
                except (json.JSONDecodeError, IndexError):
                    pass

            self.conversation_manager.update_context_summary(
                session_id=session.id,
                summary=summary,
                entities=entities,
            )

            logger.info(f"Updated context summary for session {session.id}")

        except Exception as e:
            logger.error(f"Failed to update context summary: {e}")

    def end_session(self, session_id: int) -> bool:
        """End a team session and record the event."""
        session = self.conversation_manager.get_session_by_id(session_id)
        if not session:
            return False

        # Record event
        event = TeamSessionEvent(
            session_id=session_id,
            event_type="state_change",
            event_data={"new_state": "CLOSED", "previous_state": session.session_state},
            agent_id=session.current_agent_id,
        )
        self.db.add(event)
        self.db.commit()

        self.conversation_manager.update_session_state(session_id, "CLOSED")
        return self.conversation_manager.end_session(session_id)

    def get_session_events(
        self,
        session_id: int,
        event_type: Optional[str] = None,
    ) -> List[TeamSessionEvent]:
        """Get events for a session, optionally filtered by type."""
        query = self.db.query(TeamSessionEvent).filter(
            TeamSessionEvent.session_id == session_id
        )
        if event_type:
            query = query.filter(TeamSessionEvent.event_type == event_type)

        return query.order_by(TeamSessionEvent.created_at.asc()).all()
