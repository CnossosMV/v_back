"""
Action Executor for Event Actions

Handles execution of individual action types.
Each action type has a dedicated handler method.
"""
import logging
from typing import Any, Dict, Optional
from datetime import datetime
from dataclasses import dataclass
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class ActionContext:
    """Context passed to action handlers."""
    db: Session
    project_id: int
    user_id: Optional[int]
    event_id: Optional[int]
    event_data: Dict[str, Any]
    user_data: Optional[Dict[str, Any]]
    variables: Dict[str, Any]
    event_action_id: Optional[int] = None


@dataclass
class ActionResult:
    """Result of action execution."""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def _render_meta_components(components: list, variables: Dict[str, Any]) -> list:
    """Render `{{var}}` placeholders inside Meta template parameter values.

    `components` mirrors Meta's Graph-API shape (list of {type, parameters:[...]})
    but may carry `{{variable}}` tokens in text values from template config.
    Missing variables are left as empty strings to avoid Meta rejecting the payload.
    """
    import re
    if not components:
        return []
    pattern = re.compile(r"\{\{\s*([\w\.]+)\s*\}\}")

    def resolve(path: str) -> str:
        cur: Any = variables
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return ""
        return "" if cur is None else str(cur)

    def render_str(s: str) -> str:
        return pattern.sub(lambda m: resolve(m.group(1)), s)

    rendered: list = []
    for comp in components:
        if not isinstance(comp, dict):
            rendered.append(comp)
            continue
        new_comp = dict(comp)
        params = new_comp.get("parameters")
        if isinstance(params, list):
            new_params = []
            for p in params:
                if isinstance(p, dict):
                    np = dict(p)
                    if isinstance(np.get("text"), str):
                        np["text"] = render_str(np["text"])
                    new_params.append(np)
                else:
                    new_params.append(p)
            new_comp["parameters"] = new_params
        rendered.append(new_comp)
    return rendered


class ActionExecutor:
    """
    Executes individual actions from Event Action rules.

    Supported action types:
    - send_template: Send message via MessagingTemplate (channel inferred from template.channel_type)
    - send_whatsapp_message: DEPRECATED — prefer send_template with a WhatsApp MessagingTemplate
    - assign_chatbot: Assign user to chatbot session
    - assign_agent: Create support ticket for human agent
    - assign_agent_team: Assign user to agent team session
    - update_user: Update MessagingUser properties
    - add_tag: Add tag to user properties
    - webhook: Call external webhook
    - add_to_funnel: Add user to email funnel
    - run_graph: Execute LangGraph workflow (future)
    """

    @staticmethod
    def _context_country(context: ActionContext) -> str:
        user_data = context.user_data or {}
        user_properties = user_data.get("properties") or {}
        event_properties = (context.event_data or {}).get("properties") or {}
        return str(
            user_data.get("country")
            or user_data.get("market_country")
            or user_properties.get("country")
            or user_properties.get("market_country")
            or event_properties.get("country")
            or event_properties.get("market_country")
            or ""
        ).upper()

    @staticmethod
    def _locale_routing_user_data(context: ActionContext) -> Dict[str, Any]:
        """Prefer the triggering event's market/locale traits for this send."""
        user_data = dict(context.user_data or {})
        properties = dict(user_data.get("properties") or {})
        event_properties = (context.event_data or {}).get("properties") or {}

        for key in (
            "country",
            "market_country",
            "ui_language",
            "communication_locale",
            "content_locale",
            "content_language",
            "locale",
            "timezone",
        ):
            if event_properties.get(key) is not None:
                properties[key] = event_properties[key]
                user_data[key] = event_properties[key]

        user_data["properties"] = properties
        return user_data


    async def execute(
        self,
        action_type: str,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Execute an action of the specified type.

        Args:
            action_type: The type of action to execute
            config: Action configuration
            context: Execution context

        Returns:
            ActionResult with success status and details
        """
        handler = self._get_handler(action_type)
        if not handler:
            return ActionResult(
                success=False,
                message=f"Unknown action type: {action_type}",
                error=f"No handler for action type '{action_type}'"
            )

        try:
            result = await handler(config, context)
            logger.info(f"Action {action_type} executed: {result.success}")
            return result
        except Exception as e:
            logger.error(f"Action {action_type} failed: {e}")
            return ActionResult(
                success=False,
                message=f"Action execution failed: {str(e)}",
                error=str(e)
            )

    def _get_handler(self, action_type: str):
        """Get handler method for action type."""
        handlers = {
            "send_template": self._send_template,
            "assign_chatbot": self._assign_chatbot,
            "assign_agent": self._assign_agent,
            "assign_agent_team": self._assign_agent_team,
            "update_user": self._update_user,
            "add_tag": self._add_tag,
            "webhook": self._call_webhook,
            "add_to_funnel": self._add_to_funnel,
            "run_graph": self._run_graph,
            "api_call": self._api_call,
            "send_whatsapp_message": self._send_whatsapp_message,
        }
        return handlers.get(action_type)

    async def _send_template(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Send message using MessagingTemplate.

        Config:
            template_id: int - Template ID to use
            channel_id: int (optional) - Override channel
            recipient_field: str (optional) - Field to get recipient from
        """
        if not config.get("template_id"):
            return ActionResult(success=False, message="Missing template_id", error="template_id required")
        return await self._send_template_via_send_layer(config, context)

    async def _assign_chatbot(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Assign user to a chatbot session.

        Config:
            chatbot_id: int - Chatbot to assign
            greeting_message: str (optional) - Initial greeting
        """
        from app.models import Chatbot, ChatSession

        chatbot_id = config.get("chatbot_id")
        if not chatbot_id:
            return ActionResult(success=False, message="Missing chatbot_id", error="chatbot_id required")

        # Get chatbot
        chatbot = context.db.query(Chatbot).filter(
            Chatbot.id == chatbot_id,
            Chatbot.project_id == context.project_id,
            Chatbot.status == "active"
        ).first()

        if not chatbot:
            return ActionResult(success=False, message="Chatbot not found", error=f"Chatbot {chatbot_id} not found")

        # Get user identifier
        user_identifier = "unknown"
        if context.user_data:
            user_identifier = context.user_data.get("email") or context.user_data.get("external_id") or "unknown"

        # Create session
        session = ChatSession(
            chatbot_id=chatbot_id,
            user_id=context.user_id,
            user_identifier=user_identifier,
            channel="event_action",
            context_data={
                "trigger_event": context.event_data.get("event_name"),
                "triggered_by": "event_action"
            }
        )
        context.db.add(session)
        context.db.commit()

        # Send greeting if configured
        greeting = config.get("greeting_message")
        if greeting:
            from app.models import ChatMessage
            message = ChatMessage(
                session_id=session.id,
                role="assistant",
                content=greeting
            )
            context.db.add(message)
            context.db.commit()

        return ActionResult(
            success=True,
            message=f"User assigned to chatbot {chatbot.name}",
            data={"session_id": session.id, "chatbot_id": chatbot_id}
        )

    async def _assign_agent(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Create support ticket for human agent.

        Config:
            priority: str (optional) - low, medium, high, urgent
            tags: list (optional) - Tags to apply
            reason: str (optional) - Escalation reason
        """
        from app.models import SupportTicket
        import secrets

        # Get user identifier
        customer_identifier = "unknown"
        customer_name = None
        customer_phone = None
        if context.user_data:
            customer_identifier = context.user_data.get("email") or context.user_data.get("external_id") or "unknown"
            customer_name = context.user_data.get("name")
            customer_phone = context.user_data.get("phone")

        # Generate ticket number
        ticket_number = f"EVT-{secrets.token_hex(4).upper()}"

        # Create ticket
        ticket = SupportTicket(
            project_id=context.project_id,
            session_id=None,  # No session associated
            ticket_number=ticket_number,
            status="open",
            priority=config.get("priority", "medium"),
            human_takeover=True,
            customer_identifier=customer_identifier,
            customer_name=customer_name,
            customer_phone=customer_phone,
            channel="event_action",
            tags=config.get("tags"),
            escalation_reason=config.get("reason", f"Event action triggered: {context.event_data.get('event_name')}"),
            contact_id=context.user_id,
        )
        context.db.add(ticket)
        context.db.commit()

        # Push notification to project agents
        try:
            from app.services.push_notification_service import get_push_service
            push_svc = get_push_service(context.db)
            push_svc.send_to_project_agents(
                context.project_id,
                "New Support Ticket",
                f"#{ticket_number} — {ticket.customer_name or ticket.customer_identifier}",
                {"url": f"/support/{ticket.id}", "ticket_id": str(ticket.id), "project_id": str(context.project_id)},
            )
        except Exception as e:
            logger.warning(f"Push notification failed for ticket {ticket_number}: {e}")

        return ActionResult(
            success=True,
            message=f"Support ticket created: {ticket_number}",
            data={"ticket_id": ticket.id, "ticket_number": ticket_number}
        )

    async def _assign_agent_team(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Assign user to an agent team session.

        Config:
            team_id: int - Agent team to assign
            greeting_message: str (optional) - Initial greeting
            initial_context: dict (optional) - Additional context for the team
        """
        from app.models import AgentTeam, ChatSession, ChatMessage

        team_id = config.get("team_id")
        if not team_id:
            return ActionResult(success=False, message="Missing team_id", error="team_id required")

        # Get agent team
        team = context.db.query(AgentTeam).filter(
            AgentTeam.id == team_id,
            AgentTeam.project_id == context.project_id,
            AgentTeam.status == "active"
        ).first()

        if not team:
            return ActionResult(success=False, message="Agent team not found", error=f"Agent team {team_id} not found or not active")

        # Get user identifier
        user_identifier = "unknown"
        if context.user_data:
            user_identifier = context.user_data.get("email") or context.user_data.get("external_id") or "unknown"

        # Create session linked to the agent team
        context_data = {
            "trigger_event": context.event_data.get("event_name"),
            "triggered_by": "event_action",
        }
        initial_context = config.get("initial_context")
        if initial_context and isinstance(initial_context, dict):
            context_data.update(initial_context)

        session = ChatSession(
            team_id=team_id,
            user_id=context.user_id,
            user_identifier=user_identifier,
            channel="event_action",
            context_data=context_data,
        )
        context.db.add(session)
        context.db.commit()

        # Send greeting if configured
        greeting = config.get("greeting_message")
        if greeting:
            message = ChatMessage(
                session_id=session.id,
                role="assistant",
                content=greeting,
            )
            context.db.add(message)
            context.db.commit()

        return ActionResult(
            success=True,
            message=f"User assigned to agent team {team.name}",
            data={"session_id": session.id, "team_id": team_id}
        )

    async def _update_user(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Update MessagingUser properties.

        Config:
            properties: dict - Properties to update/add
        """
        from app.models.messaging import MessagingUser

        if not context.user_id:
            return ActionResult(success=False, message="No user to update", error="user_id required")

        user = context.db.query(MessagingUser).filter(
            MessagingUser.id == context.user_id
        ).first()

        if not user:
            return ActionResult(success=False, message="User not found", error=f"User {context.user_id} not found")

        properties = config.get("properties", {})
        if not properties:
            return ActionResult(success=False, message="No properties to update", error="properties required")

        # Merge properties
        current_props = dict(user.properties or {})
        current_props.update(properties)
        user.properties = current_props
        user.updated_at = datetime.utcnow()
        context.db.commit()

        return ActionResult(
            success=True,
            message=f"User properties updated",
            data={"user_id": user.id, "properties": properties}
        )

    async def _add_tag(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Add tag to user properties.

        Config:
            tag: str - Tag to add
        """
        from app.models.messaging import MessagingUser

        if not context.user_id:
            return ActionResult(success=False, message="No user to tag", error="user_id required")

        tag = config.get("tag")
        if not tag:
            return ActionResult(success=False, message="No tag specified", error="tag required")

        user = context.db.query(MessagingUser).filter(
            MessagingUser.id == context.user_id
        ).first()

        if not user:
            return ActionResult(success=False, message="User not found", error=f"User {context.user_id} not found")

        # Add tag to dedicated user.tags column
        from sqlalchemy.orm.attributes import flag_modified
        current_tags = list(user.tags or [])
        if tag not in current_tags:
            current_tags.append(tag)
        user.tags = current_tags
        flag_modified(user, "tags")
        user.updated_at = datetime.utcnow()
        context.db.commit()

        return ActionResult(
            success=True,
            message=f"Tag '{tag}' added to user",
            data={"user_id": user.id, "tag": tag}
        )

    async def _call_webhook(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Call external webhook.

        Config:
            url: str - Webhook URL
            method: str (optional) - HTTP method (default: POST)
            headers: dict (optional) - Custom headers
            body_template: dict (optional) - Body template with variable substitution
        """
        import aiohttp
        import json

        url = config.get("url")
        if not url:
            return ActionResult(success=False, message="No URL specified", error="url required")

        method = config.get("method", "POST").upper()
        headers = config.get("headers", {})
        headers.setdefault("Content-Type", "application/json")

        # Build body from template or default
        body_template = config.get("body_template")
        if body_template:
            # Substitute variables
            body = self._substitute_variables(body_template, context.variables)
        else:
            # Default body with event and user data
            body = {
                "event": context.event_data,
                "user": context.user_data,
                "project_id": context.project_id,
                "timestamp": datetime.utcnow().isoformat()
            }

        try:
            async with aiohttp.ClientSession() as session:
                request_kwargs: Dict[str, Any] = {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "timeout": aiohttp.ClientTimeout(total=30),
                }
                if method == "GET":
                    if body_template:
                        request_kwargs["params"] = self._substitute_variables(body_template, context.variables)
                    headers.pop("Content-Type", None)
                else:
                    request_kwargs["json"] = body

                async with session.request(**request_kwargs) as response:
                    response_text = await response.text()
                    success = 200 <= response.status < 300

                    # Parse JSON response for structured access
                    try:
                        response_parsed = json.loads(response_text)
                    except (json.JSONDecodeError, ValueError):
                        response_parsed = response_text[:500]

                    return ActionResult(
                        success=success,
                        message=f"Webhook returned {response.status}",
                        data={
                            "status_code": response.status,
                            "response": response_text[:500],
                            "result": response_parsed,
                        },
                        error=None if success else response_text[:200]
                    )

        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Webhook call failed: {str(e)}",
                error=str(e)
            )

    async def _add_to_funnel(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Enroll a messaging user into a funnel via FunnelEngine.

        Config:
            funnel_id: int - Funnel ID to enroll user into
        """
        from app.services.funnel_engine import FunnelEngine

        funnel_id = config.get("funnel_id")
        if not funnel_id:
            return ActionResult(success=False, message="Missing funnel_id", error="funnel_id required")

        # Resolve messaging user id
        user_id = None
        if context.user_data:
            user_id = context.user_data.get("id")
        if not user_id:
            return ActionResult(success=False, message="No user for funnel enrollment", error="Could not determine messaging user id")

        engine = FunnelEngine(context.db)
        enrollment = engine.enroll_user(
            funnel_id=funnel_id,
            user_id=user_id,
            metadata=context.variables or {},
        )

        if not enrollment:
            return ActionResult(success=False, message="Funnel not active or not found", error=f"Funnel {funnel_id} not available")

        return ActionResult(
            success=True,
            message=f"User enrolled in funnel (enrollment {enrollment.id})",
            data={"funnel_id": funnel_id, "enrollment_id": enrollment.id}
        )

    async def _run_graph(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Execute LangGraph workflow.

        Config:
            graph_id: str - Graph identifier
            input_mapping: dict - Map context to graph inputs
        """
        from app.services.event_actions.graphs import WORKFLOW_REGISTRY

        graph_id = config.get("graph_id")
        if not graph_id:
            return ActionResult(success=False, message="Missing graph_id", error="graph_id required")

        # Get workflow from registry
        workflow = WORKFLOW_REGISTRY.get(graph_id)
        if not workflow:
            return ActionResult(
                success=False,
                message=f"Unknown graph: {graph_id}",
                error=f"Graph '{graph_id}' not found in registry"
            )

        try:
            # Build input state from context and input_mapping
            input_mapping = config.get("input_mapping", {})
            initial_state = {
                "project_id": context.project_id,
                "user_id": context.user_id,
                "event_id": context.event_id,
                "messages": [],
                "status": "started"
            }

            # Apply input mapping
            for target_key, source_key in input_mapping.items():
                value = context.variables.get(source_key)
                if value is not None:
                    initial_state[target_key] = value

            # Add event properties
            if context.event_data:
                props = context.event_data.get("properties", {})
                for key, value in props.items():
                    if key not in initial_state:
                        initial_state[key] = value

            # Set default values for workflow-specific fields
            if graph_id == "abandoned_cart":
                initial_state.setdefault("cart_value", 0)
                initial_state.setdefault("cart_items", [])
                initial_state.setdefault("reminder_1_sent", False)
                initial_state.setdefault("reminder_2_sent", False)
                initial_state.setdefault("discount_sent", False)
                initial_state.setdefault("purchased", False)

            # Execute workflow
            result_state = await workflow.ainvoke(initial_state)

            return ActionResult(
                success=True,
                message=f"Graph {graph_id} executed successfully",
                data={
                    "graph_id": graph_id,
                    "status": result_state.get("status", "completed"),
                    "messages": result_state.get("messages", [])
                }
            )

        except Exception as e:
            logger.error(f"Graph execution failed: {graph_id}, error: {e}")
            return ActionResult(
                success=False,
                message=f"Graph execution failed: {str(e)}",
                error=str(e)
            )

    async def _api_call(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Call an external API via a Project API Connection.

        Config:
            connection_id: int - API connection to use
            endpoint_slug: str - Endpoint slug to call
            parameters: dict (optional) - Parameters with {{variable}} support
            store_result_as: str (optional) - Store response in context.variables
        """
        from app.services.api_connector_service import ApiConnectorService

        connection_id = config.get("connection_id")
        endpoint_slug = config.get("endpoint_slug")
        if not connection_id or not endpoint_slug:
            return ActionResult(
                success=False,
                message="connection_id and endpoint_slug are required",
                error="Missing required config fields",
            )

        # Interpolate parameters
        raw_params = config.get("parameters", {})
        params = self._substitute_variables(raw_params, context.variables)

        try:
            service = ApiConnectorService(context.db)
            result = service.call_endpoint(
                connection_id=connection_id,
                endpoint_id_or_slug=endpoint_slug,
                parameters=params if isinstance(params, dict) else {},
                trigger_source="event_action",
                trigger_source_id=str(context.event_id) if context.event_id else None,
            )

            # Optionally store result for downstream actions
            store_as = config.get("store_result_as")
            if store_as and result.get("result") is not None:
                context.variables[store_as] = result["result"]

            success = result.get("status") == "success"
            return ActionResult(
                success=success,
                message=f"API call {'succeeded' if success else 'failed'}",
                data=result,
                error=result.get("error"),
            )
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"API call failed: {str(e)}",
                error=str(e),
            )

    async def _send_whatsapp_message(
        self,
        config: Dict[str, Any],
        context: ActionContext
    ) -> ActionResult:
        """
        Send a WhatsApp message (text or template) via WhatsAppSender.

        Config:
            instance_id: int - WhatsApp instance to use
            message_type: str - "text" or "template"
            message: str - Free-form text (when message_type=text)
            template_name: str - Template name (when message_type=template)
            template_language: str - Language code (when message_type=template)
            template_components: list - Template components (when message_type=template)
            recipient_field: str - User field for phone number (default: "phone")
        """
        if self._context_country(context) == "US":
            return ActionResult(
                success=True,
                message="WhatsApp skipped for US market",
                data={"skipped": True, "reason": "channel_not_enabled_for_market"},
            )

        # Check project-level channel toggle
        from app.services.channels.channel_registry_service import ChannelRegistryService
        reg = ChannelRegistryService(context.db)
        if not reg.is_channel_available(context.project_id, "whatsapp"):
            return ActionResult(success=False, message="WhatsApp channel is disabled for this project", error="channel_disabled")

        # All WhatsApp sends go through the unified SendService.
        return await self._send_whatsapp_via_send_layer(config, context)

    async def _send_template_via_send_layer(
        self,
        config: Dict[str, Any],
        context: ActionContext,
    ) -> ActionResult:
        """Route template send through the unified SendService."""
        from app.models.messaging import MessagingTemplate

        template_id = config.get("template_id")
        if not template_id:
            return ActionResult(success=False, message="Missing template_id", error="template_id required")

        template = context.db.query(MessagingTemplate).filter(
            MessagingTemplate.id == template_id,
            MessagingTemplate.project_id == context.project_id,
            MessagingTemplate.is_active == True,
        ).first()
        if not template:
            return ActionResult(success=False, message="Template not found", error=f"Template {template_id} not found")

        # i18n: resolve the contact's locale/timezone, then re-resolve this template to
        # the matching locale variant (gives the right body AND meta_language for free).
        from app.models import Project
        from app.services.messaging.locale_resolver import contact_locale_tz
        from app.services.messaging.template_selector import resolve_template
        _project = context.db.query(Project).filter(Project.id == context.project_id).first()
        routing_user_data = self._locale_routing_user_data(context)
        send_locale, send_tz = contact_locale_tz(_project, user_data=routing_user_data) if _project else (None, None)
        if _project and template.slug:
            variant = resolve_template(context.db, context.project_id, template.slug, send_locale)
            if variant is not None:
                template = variant

        # Determine channel from template channel_type
        from app.services.channels.registry import ChannelRegistry
        tpl_ct = template.channel_type.value if template.channel_type else "email"

        channel = tpl_ct if tpl_ct in set(ChannelRegistry.available_channels()) else "email"
        if channel == "whatsapp" and self._context_country(context) == "US":
            return ActionResult(
                success=True,
                message="WhatsApp skipped for US market",
                data={"skipped": True, "reason": "channel_not_enabled_for_market"},
            )


        # Channel-aware recipient resolution: WhatsApp/SMS prefer phone, others prefer email.
        default_field = "phone" if channel in ("whatsapp", "sms") else "email"
        recipient_field = config.get("recipient_field", default_field)
        recipient = None
        if context.user_data:
            if channel in ("whatsapp", "sms"):
                recipient = (
                    context.user_data.get("phone_e164")
                    or context.user_data.get(recipient_field)
                    or context.user_data.get("phone")
                )
            else:
                recipient = context.user_data.get(recipient_field)
        if not recipient and context.event_data:
            props = context.event_data.get("properties", {})
            recipient = (
                props.get(recipient_field)
                or (props.get("phone") if channel in ("whatsapp", "sms") else props.get("email"))
            )
        if not recipient:
            return ActionResult(success=False, message="No recipient found", error="Could not determine recipient")

        # Render template body/subject from variables
        variables = dict(context.variables)
        variable_mapping = config.get("variable_mapping", {})
        if variable_mapping:
            for template_var, source_path in variable_mapping.items():
                if isinstance(source_path, str):
                    if source_path.startswith("_static:"):
                        variables[template_var] = source_path[8:]
                    else:
                        resolved = self._resolve_path(context.variables, source_path)
                        if resolved is not None:
                            variables[template_var] = resolved

        from app.services.messaging.template_renderer import template_renderer
        rendered_body, rendered_subject, _, _ = template_renderer.render_template(
            template.body, variables, template.subject,
            locale=send_locale, timezone=send_tz,
        )
        from app.services.messaging.text_quality import find_mojibake
        mojibake_marker = find_mojibake((rendered_subject, rendered_body))
        if mojibake_marker:
            logger.error(
                "Blocked template %s/%s because rendered content contains mojibake marker %r",
                template.slug, template.locale, mojibake_marker,
            )
            return ActionResult(
                success=False, message="Rendered template failed content validation",
                error="mojibake_detected",
            )

        # WhatsApp + Meta-approved template → dispatch via SendService in 'template' mode.
        if channel == "whatsapp" and template.meta_template_name:
            instance_cfg: dict = {}
            wa_instance_id = config.get("instance_id") or template.whatsapp_instance_id
            if wa_instance_id:
                instance_cfg["instance_id"] = wa_instance_id
            rendered_components = _render_meta_components(
                template.meta_components or [],
                variables,
            )
            return await self._send_via_send_layer(
                context=context,
                channel="whatsapp",
                recipient=recipient,
                content_type="template",
                template_name=template.meta_template_name,
                template_language=template.meta_language or "en_US",
                template_components=rendered_components,
                instance_config=instance_cfg or None,
                template_id=template.id,
            )

        # Respect body_format for plain text email templates
        tpl_body_format = getattr(template, 'body_format', None) or "html"
        if channel == "email" and tpl_body_format == "plain":
            ct = "text"
            text = rendered_body
            html = None
        elif channel == "email":
            ct = "rich"
            text = None
            html = rendered_body
        else:
            ct = "text"
            text = rendered_body
            html = None

        # WhatsApp Evolution / free-form text: pass instance id if template owns one.
        instance_cfg = None
        if channel == "whatsapp":
            wa_instance_id = config.get("instance_id") or template.whatsapp_instance_id
            if wa_instance_id:
                instance_cfg = {"instance_id": wa_instance_id}

        # Email header overrides: config wins over template defaults.
        extra_metadata: Optional[Dict[str, Any]] = None
        if channel == "email":
            extra_metadata = {}
            for key in ("from_email", "from_name", "reply_to"):
                val = config.get(key) or getattr(template, key, None)
                if val:
                    extra_metadata[key] = val
            if config.get("use_direct_smtp"):
                extra_metadata["use_direct_smtp"] = True
            if config.get("bcc"):
                extra_metadata["bcc"] = config["bcc"]
            if not extra_metadata:
                extra_metadata = None

        # Email instance override (config wins; user may route via a specific email instance).
        if channel == "email" and config.get("email_instance_id"):
            instance_cfg = {"instance_id": config["email_instance_id"]}

        return await self._send_via_send_layer(
            context=context,
            channel=channel,
            recipient=recipient,
            content_type=ct,
            text=text,
            html=html,
            subject=rendered_subject,
            media_url=template.media_url,
            template_id=template.id,
            instance_config=instance_cfg,
            metadata=extra_metadata,
        )

    async def _send_whatsapp_via_send_layer(
        self,
        config: Dict[str, Any],
        context: ActionContext,
    ) -> ActionResult:
        """Route WhatsApp send through the unified SendService."""
        instance_id = config.get("instance_id")
        if not instance_id:
            return ActionResult(success=False, message="Missing instance_id", error="instance_id required")

        # Resolve recipient phone
        recipient_field = config.get("recipient_field", "phone")
        phone = None
        if context.user_data:
            phone = context.user_data.get("phone_e164") or context.user_data.get(recipient_field) or context.user_data.get("phone")
        if not phone and context.event_data:
            props = context.event_data.get("properties", {})
            phone = props.get(recipient_field) or props.get("phone")
        if not phone:
            return ActionResult(success=False, message="No phone number found", error="Could not determine recipient phone")

        message_type = config.get("message_type", "text")
        if message_type == "template":
            template_name = config.get("template_name")
            if not template_name:
                return ActionResult(success=False, message="Missing template_name", error="template_name required")
            return await self._send_via_send_layer(
                context=context,
                channel="whatsapp",
                recipient=phone,
                content_type="template",
                template_name=template_name,
                template_language=config.get("template_language", "en_US"),
                template_components=config.get("template_components"),
                instance_config={"instance_id": instance_id},
            )
        else:
            message_text = config.get("message", "")
            rendered = self._substitute_variables(message_text, context.variables) if message_text else ""
            if not rendered:
                return ActionResult(success=False, message="Empty message", error="message text is empty")
            return await self._send_via_send_layer(
                context=context,
                channel="whatsapp",
                recipient=phone,
                content_type="text",
                text=rendered,
                template_name=config.get("template_name"),
                template_language=config.get("template_language", "en_US"),
                template_components=config.get("template_components"),
                instance_config={"instance_id": instance_id},
            )

    async def _send_via_send_layer(
        self,
        context: ActionContext,
        channel: str,
        recipient: str,
        content_type: str = "text",
        text: Optional[str] = None,
        html: Optional[str] = None,
        subject: Optional[str] = None,
        media_url: Optional[str] = None,
        media_type: Optional[str] = None,
        template_name: Optional[str] = None,
        template_language: Optional[str] = None,
        template_components=None,
        instance_config: Optional[Dict[str, Any]] = None,
        template_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ActionResult:
        """Route a send through the unified SendService."""
        from app.services.channels.base import OutboundContent
        from app.services.channels.send_service import SendService

        content = OutboundContent(
            content_type=content_type,
            text=text,
            html=html,
            subject=subject,
            media_url=media_url,
            media_type=media_type,
            template_name=template_name,
            template_language=template_language,
            template_components=template_components,
            metadata=metadata,
        )

        svc = SendService(context.db)
        decision = await svc.send(
            project_id=context.project_id,
            user_id=context.user_id,
            recipient=recipient,
            content=content,
            channel=channel,
            source_type="event_action",
            source_id=context.event_action_id,
            instance_config=instance_config,
            template_id=template_id,
        )

        if decision.success:
            # Record contact ledger — skipped when send() is the writer (0D)
            from app.services.channels.consolidation import ledger_in_send_enabled
            if context.user_id and not ledger_in_send_enabled(context.db, context.project_id):
                try:
                    from app.services.scoring.policy_service import PolicyService
                    PolicyService(context.db).record_contact(
                        project_id=context.project_id,
                        user_id=context.user_id,
                        channel=decision.channel_used or channel,
                        source="event_action",
                        source_id=context.event_id,
                    )
                    context.db.commit()
                except Exception as e:
                    logger.warning(f"Error recording contact ledger: {e}")

        return ActionResult(
            success=decision.success,
            message=f"Sent via {decision.channel_used}" if decision.success else (decision.error or "Send failed"),
            data={"send_log_id": decision.send_log_id, "channel_used": decision.channel_used},
            error=decision.error if not decision.success else None,
        )

    def _resolve_path(self, data: Dict[str, Any], path: str) -> Any:
        """
        Resolve a dot-notation path from a nested dictionary.

        Examples:
            _resolve_path({"properties": {"name": "John"}}, "properties.name") -> "John"
            _resolve_path({"user": {"tags": ["a", "b"]}}, "user.tags.0") -> "a"
        """
        if not data or not path:
            return None

        keys = path.split('.')
        value = data

        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            elif isinstance(value, list):
                try:
                    idx = int(key)
                    value = value[idx] if 0 <= idx < len(value) else None
                except (ValueError, IndexError):
                    return None
            else:
                return None

            if value is None:
                return None

        return value

    def _substitute_variables(self, template: Any, variables: Dict[str, Any]) -> Any:
        """Recursively substitute variables in template."""
        if isinstance(template, str):
            import re
            def replace(match):
                key = match.group(1).strip()
                # Support dotted paths like project.login_url
                value = self._resolve_path(variables, key)
                if value is not None:
                    return str(value)
                return match.group(0)
            return re.sub(r'\{\{([\s\w.]+)\}\}', replace, template)
        elif isinstance(template, dict):
            return {k: self._substitute_variables(v, variables) for k, v in template.items()}
        elif isinstance(template, list):
            return [self._substitute_variables(item, variables) for item in template]
        return template


# Singleton instance
action_executor = ActionExecutor()
