"""
Milestone Classifier — evaluates inbound messages against step-level goals.

Supports three detection strategies (priority order):
1. Structured: exact match on button payloads / quick reply IDs
2. Pattern: regex / keyword conditions via ConditionEvaluator
3. AI: LLM classification via resolve_llm
"""

import logging
import re
import fnmatch
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    outcome: str  # "goal_met" | "on_topic" | "blocker" | "off_topic"
    goal_id: Optional[str] = None
    confidence: float = 0.0
    reasoning: Optional[str] = None
    compound: bool = False  # True if goal_met AND off-topic side request detected


class MilestoneClassifier:
    """Classify inbound messages against step-level milestone goals."""

    def __init__(self, db: Session):
        self.db = db

    def classify(
        self,
        message_text: str,
        goals: List[Dict[str, Any]],
        project_id: int,
        content_pieces: Optional[List[Dict[str, Any]]] = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
    ) -> ClassificationResult:
        """Classify message against goals using structured → pattern → AI pipeline.

        Args:
            message_text: The inbound message text
            goals: List of goal configs from step_config
            project_id: For LLM key resolution
            content_pieces: Raw message content pieces (for button/interactive payloads)
            conversation_history: Last N messages for AI context

        Returns:
            ClassificationResult with outcome, goal_id, confidence, reasoning
        """
        if not goals:
            # No goals defined — default to goal_met (backward compat: any reply advances)
            return ClassificationResult(outcome="goal_met", confidence=1.0)

        # 1. Try structured detection (button payloads / quick reply IDs)
        structured_result = self._try_structured(message_text, goals, content_pieces)
        if structured_result:
            return structured_result

        # 2. Try pattern detection (reuse ConditionEvaluator)
        pattern_result = self._try_pattern(message_text, goals)
        if pattern_result:
            return pattern_result

        # 3. Try AI classification for goals with detection "ai" or "auto"
        ai_goals = [
            g for g in goals
            if g.get("detection") in ("ai", "auto")
        ]
        if ai_goals:
            ai_result = self._try_ai(
                message_text, goals, project_id, conversation_history,
            )
            if ai_result:
                return ai_result

        # 4. Default: on_topic (goals exist but no match)
        return ClassificationResult(
            outcome="on_topic",
            confidence=0.3,
            reasoning="No goal matched via structured/pattern/AI detection",
        )

    def _try_structured(
        self,
        message_text: str,
        goals: List[Dict[str, Any]],
        content_pieces: Optional[List[Dict[str, Any]]],
    ) -> Optional[ClassificationResult]:
        """Check if any content piece payload matches a goal's structured_match."""
        if not content_pieces:
            return None

        # Extract payloads from interactive/button content pieces
        payloads: List[str] = []
        for piece in content_pieces:
            piece_type = piece.get("type", "")
            if piece_type in ("interactive", "button"):
                payload = piece.get("payload") or piece.get("id") or ""
                if payload:
                    payloads.append(str(payload))
            # Also check nested button_reply
            button_reply = piece.get("button_reply") or {}
            if button_reply.get("id"):
                payloads.append(str(button_reply["id"]))
            # Interactive list reply
            list_reply = piece.get("list_reply") or {}
            if list_reply.get("id"):
                payloads.append(str(list_reply["id"]))

        if not payloads:
            return None

        for goal in goals:
            structured_match = goal.get("structured_match")
            if not structured_match:
                continue
            for payload in payloads:
                # Support glob patterns (e.g. "plan_*")
                if fnmatch.fnmatch(payload, structured_match):
                    return ClassificationResult(
                        outcome="goal_met",
                        goal_id=goal.get("id"),
                        confidence=1.0,
                        reasoning=f"Structured match: payload '{payload}' matched '{structured_match}'",
                    )

        return None

    def _try_pattern(
        self,
        message_text: str,
        goals: List[Dict[str, Any]],
    ) -> Optional[ClassificationResult]:
        """Check if message matches any goal's pattern_conditions via ConditionEvaluator."""
        for goal in goals:
            conditions = goal.get("pattern_conditions")
            detection = goal.get("detection", "auto")
            if not conditions or detection == "ai":
                continue

            try:
                from app.services.event_actions.conditions import ConditionEvaluator
                evaluator = ConditionEvaluator()
                event_data = {"body": message_text, "message.body": message_text}
                matched = evaluator.evaluate_all(
                    conditions, event_data, match_mode="any",
                )
                if matched:
                    return ClassificationResult(
                        outcome="goal_met",
                        goal_id=goal.get("id"),
                        confidence=0.9,
                        reasoning=f"Pattern match on goal '{goal.get('id')}'",
                    )
            except Exception as e:
                logger.warning(f"Pattern evaluation error for goal {goal.get('id')}: {e}")
                continue

        return None

    def _try_ai(
        self,
        message_text: str,
        goals: List[Dict[str, Any]],
        project_id: int,
        conversation_history: Optional[List[Dict[str, Any]]],
    ) -> Optional[ClassificationResult]:
        """Use LLM to classify the message against goals."""
        try:
            from app.services.chatbot.llm_key_resolver import resolve_llm

            llm_config = resolve_llm(self.db, project_id, purpose="milestone")
            if not llm_config or not llm_config.api_key:
                logger.warning(f"No LLM key available for milestone classification (project {project_id})")
                return None

            # Build goal descriptions for the prompt
            goal_lines = []
            for g in goals:
                goal_lines.append(f"- {g.get('id', 'unnamed')}: {g.get('description', 'No description')}")
            goals_text = "\n".join(goal_lines)

            # Build conversation context
            history_text = ""
            if conversation_history:
                history_lines = []
                for msg in conversation_history[-10:]:
                    role = msg.get("role", "user")
                    text = msg.get("text", msg.get("body", ""))[:200]
                    history_lines.append(f"[{role}]: {text}")
                history_text = "\nRecent conversation:\n" + "\n".join(history_lines)

            system_prompt = (
                "You are a message classifier for a marketing funnel. "
                "Classify the user's latest message into EXACTLY ONE category:\n\n"
                "1. goal_met:{goal_id} — The message fulfills one of the defined goals\n"
                "2. on_topic — The message is related to the conversation but doesn't fulfill any goal\n"
                "3. blocker — The user has a problem or complaint that blocks progress\n"
                "4. off_topic — The message is completely unrelated to the conversation goals\n\n"
                "If the message fulfills a goal BUT also contains an unrelated request, "
                "respond with: goal_met:{goal_id} + off_topic\n\n"
                f"Goals:\n{goals_text}\n"
                f"{history_text}\n\n"
                "Respond with ONLY the classification label (e.g. 'goal_met:plan_selected' or 'on_topic'). "
                "No explanation."
            )

            user_prompt = f"Latest message: {message_text[:500]}"

            # Call LLM using multi-provider factory
            from app.services.chatbot.llm_key_resolver import create_chat_llm
            llm = create_chat_llm(llm_config, max_tokens=50)
            response = llm.invoke([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ])

            raw = (response.content or "").strip().lower()
            return self._parse_ai_response(raw, goals)

        except Exception as e:
            logger.error(f"AI milestone classification failed: {e}", exc_info=True)
            return None

    def _parse_ai_response(
        self,
        raw: str,
        goals: List[Dict[str, Any]],
    ) -> ClassificationResult:
        """Parse LLM response into ClassificationResult."""
        # Check for compound (goal_met + off_topic)
        compound = False
        if "+" in raw and "off_topic" in raw:
            compound = True
            raw = raw.split("+")[0].strip()

        # goal_met:{goal_id}
        goal_match = re.match(r"goal_met[:\s]+(\S+)", raw)
        if goal_match:
            goal_id = goal_match.group(1).strip()
            # Validate goal_id exists
            valid_ids = {g.get("id") for g in goals}
            if goal_id not in valid_ids:
                # Try partial match
                for vid in valid_ids:
                    if vid and goal_id in vid:
                        goal_id = vid
                        break
            return ClassificationResult(
                outcome="goal_met",
                goal_id=goal_id,
                confidence=0.8,
                reasoning=f"AI classified as goal_met:{goal_id}",
                compound=compound,
            )

        if "blocker" in raw:
            return ClassificationResult(
                outcome="blocker",
                confidence=0.8,
                reasoning="AI classified as blocker",
            )

        if "off_topic" in raw:
            return ClassificationResult(
                outcome="off_topic",
                confidence=0.8,
                reasoning="AI classified as off_topic",
            )

        if "on_topic" in raw:
            return ClassificationResult(
                outcome="on_topic",
                confidence=0.7,
                reasoning="AI classified as on_topic",
            )

        # Fallback: couldn't parse — treat as on_topic
        logger.warning(f"Unparseable AI classification response: {raw!r}")
        return ClassificationResult(
            outcome="on_topic",
            confidence=0.3,
            reasoning=f"AI response unparseable: {raw[:100]}",
        )
