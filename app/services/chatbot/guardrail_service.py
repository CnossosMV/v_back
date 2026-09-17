"""
Guardrail Service

Checks guardrails (max turns, blocked topics, operating hours, frustration detection)
before allowing a specialist to respond.
"""

import os
import json
import logging
from typing import Optional, List
from datetime import datetime, timezone
from dataclasses import dataclass, field

from app.services.chatbot.llm_key_resolver import create_chat_llm
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import SpecialistAgent, ChatSession, ChatMessage

logger = logging.getLogger(__name__)


@dataclass
class GuardrailResult:
    """Result of guardrail checks."""
    passed: bool
    triggered_checks: List[str] = field(default_factory=list)
    action: Optional[str] = None  # escalate, retry_once, log_only, None
    message: Optional[str] = None  # User-facing message if blocked
    details: Optional[dict] = field(default_factory=dict)


class GuardrailService:
    """Service for checking guardrails on specialist agents."""

    def __init__(self, db: Session):
        self.db = db

    def check_all(
        self,
        specialist: SpecialistAgent,
        session: ChatSession,
        message: str,
    ) -> GuardrailResult:
        """
        Run all guardrail checks for a specialist.

        Args:
            specialist: The target specialist agent
            session: Current chat session
            message: The user's message

        Returns:
            GuardrailResult with pass/fail and action
        """
        result = GuardrailResult(passed=True)

        # Check max turns
        max_turns_result = self._check_max_turns(specialist, session)
        if max_turns_result:
            result.passed = False
            result.triggered_checks.append("max_turns")
            result.action = specialist.frustration_action or "escalate"
            result.message = "This conversation has reached its maximum length."
            result.details["max_turns"] = max_turns_result
            return result

        # Check blocked topics
        blocked_result = self._check_blocked_topics(specialist, message)
        if blocked_result:
            result.passed = False
            result.triggered_checks.append("blocked_topics")
            result.action = "log_only"
            result.message = "I'm not able to help with that topic."
            result.details["blocked_topics"] = blocked_result
            return result

        # Check operating hours
        hours_result = self._check_operating_hours(specialist)
        if hours_result:
            result.passed = False
            result.triggered_checks.append("operating_hours")
            result.action = specialist.frustration_action or "escalate"
            result.message = hours_result.get("message", "This service is currently outside operating hours.")
            result.details["operating_hours"] = hours_result
            return result

        # Check frustration (async-safe, last check)
        frustration_result = self._check_frustration(specialist, session, message)
        if frustration_result:
            result.passed = False
            result.triggered_checks.append("frustration")
            result.action = specialist.frustration_action or "escalate"
            result.message = "Let me connect you with a human agent who can better assist you."
            result.details["frustration"] = frustration_result
            return result

        return result

    def _check_max_turns(
        self,
        specialist: SpecialistAgent,
        session: ChatSession,
    ) -> Optional[dict]:
        """Check if conversation has exceeded max turns for this specialist."""
        if not specialist.max_turns:
            return None

        # Count user messages in this session for this specialist
        turn_count = (
            self.db.query(func.count(ChatMessage.id))
            .filter(
                ChatMessage.session_id == session.id,
                ChatMessage.role == "user",
                ChatMessage.agent_id == specialist.id,
            )
            .scalar()
        ) or 0

        if turn_count >= specialist.max_turns:
            return {
                "current_turns": turn_count,
                "max_turns": specialist.max_turns,
            }

        return None

    def _check_blocked_topics(
        self,
        specialist: SpecialistAgent,
        message: str,
    ) -> Optional[dict]:
        """Check if the message contains blocked topics."""
        if not specialist.blocked_topics:
            return None

        message_lower = message.lower()
        matched = []

        for topic in specialist.blocked_topics:
            if topic.lower() in message_lower:
                matched.append(topic)

        if matched:
            return {"matched_topics": matched}

        return None

    def _check_operating_hours(
        self,
        specialist: SpecialistAgent,
    ) -> Optional[dict]:
        """
        Check if the current time is within operating hours.

        operating_hours format:
        {
            "timezone": "America/Sao_Paulo",
            "schedule": {
                "monday": {"start": "09:00", "end": "18:00"},
                "tuesday": {"start": "09:00", "end": "18:00"},
                ...
            },
            "outside_hours_message": "We're available Monday-Friday 9am-6pm."
        }
        """
        if not specialist.operating_hours:
            return None

        schedule = specialist.operating_hours.get("schedule")
        if not schedule:
            return None

        now = datetime.now(timezone.utc)

        # Try to use configured timezone
        tz_name = specialist.operating_hours.get("timezone")
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo(tz_name))
            except (ImportError, KeyError):
                pass

        day_name = now.strftime("%A").lower()
        day_schedule = schedule.get(day_name)

        if not day_schedule:
            outside_msg = specialist.operating_hours.get(
                "outside_hours_message",
                "This service is currently unavailable."
            )
            return {"message": outside_msg, "reason": f"No schedule for {day_name}"}

        current_time = now.strftime("%H:%M")
        start = day_schedule.get("start", "00:00")
        end = day_schedule.get("end", "23:59")

        if current_time < start or current_time > end:
            outside_msg = specialist.operating_hours.get(
                "outside_hours_message",
                f"This service is available {start}-{end}."
            )
            return {
                "message": outside_msg,
                "reason": f"Outside hours ({start}-{end}), current: {current_time}",
            }

        return None

    def _check_frustration(
        self,
        specialist: SpecialistAgent,
        session: ChatSession,
        message: str,
    ) -> Optional[dict]:
        """
        Detect user frustration using LLM sentiment analysis.
        Only runs if frustration_action is not 'log_only'.
        """
        if specialist.frustration_action == "log_only":
            return None

        from app.services.chatbot.llm_key_resolver import resolve_llm
        from app.models import AgentTeam

        # Resolve LLM config for guardrails purpose
        team = self.db.query(AgentTeam).filter(AgentTeam.id == specialist.team_id).first()
        project_id = team.project_id if team else 0
        try:
            llm_cfg = resolve_llm(self.db, project_id, "guardrails")
        except ValueError:
            return None

        # Get recent messages for context
        recent_messages = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.session_id == session.id)
            .order_by(ChatMessage.timestamp.desc())
            .limit(6)
            .all()
        )

        if len(recent_messages) < 3:
            return None

        # Build conversation snippet for analysis
        convo_snippet = []
        for msg in reversed(recent_messages):
            convo_snippet.append(f"{msg.role}: {msg.content[:200]}")
        convo_snippet.append(f"user: {message[:200]}")
        convo_text = "\n".join(convo_snippet)

        system_prompt = (
            "You are a sentiment analyzer. Analyze the conversation below and determine "
            "if the user is frustrated, angry, or clearly dissatisfied.\n\n"
            "Respond with ONLY a JSON object (no markdown):\n"
            '{"frustrated": true/false, "confidence": <float 0-1>, "reason": "<brief reason>"}\n\n'
            "Be conservative — only flag genuine frustration, not casual complaints."
        )

        try:
            llm = create_chat_llm(llm_cfg, max_tokens=100)

            response = llm.invoke([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": convo_text},
            ])

            text = response.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                text = text.rsplit("```", 1)[0].strip()

            result = json.loads(text)

            if result.get("frustrated") and result.get("confidence", 0) >= 0.7:
                return {
                    "confidence": result["confidence"],
                    "reason": result.get("reason", "User appears frustrated"),
                }

        except Exception as e:
            logger.error(f"Frustration detection failed: {e}")

        return None
