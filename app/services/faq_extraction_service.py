"""
FAQ Extraction Service

Extracts Q&A pairs from conversations for FAQ building and LLM training.
"""

import logging
import json
from typing import Dict, Any, Optional, List
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import (
    ChatSession, ChatMessage, ConversationFeedback, Chatbot, Project
)

logger = logging.getLogger(__name__)


class FAQExtractionService:
    """Service for extracting FAQ data from conversations."""

    def __init__(self, db: Session):
        """
        Initialize the FAQ extraction service.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    def extract_qa_pairs(
        self,
        session_id: int
    ) -> List[Dict[str, Any]]:
        """
        Extract question-answer pairs from a session.

        Args:
            session_id: Chat session ID

        Returns:
            List of Q&A pairs
        """
        messages = self.db.query(ChatMessage).filter(
            ChatMessage.session_id == session_id
        ).order_by(ChatMessage.timestamp.asc()).all()

        qa_pairs = []
        current_question = None

        for i, msg in enumerate(messages):
            if msg.role == "user" or msg.sender_type == "customer":
                # This is a question
                current_question = {
                    "question": msg.content,
                    "question_id": msg.id,
                    "timestamp": msg.timestamp.isoformat()
                }
            elif (msg.role == "assistant" or msg.sender_type in ["bot", "human_agent"]) and current_question:
                # This is an answer
                qa_pairs.append({
                    "question": current_question["question"],
                    "question_id": current_question["question_id"],
                    "answer": msg.content,
                    "answer_id": msg.id,
                    "answered_by": msg.sender_type,
                    "timestamp": current_question["timestamp"]
                })
                current_question = None

        return qa_pairs

    def mark_as_faq(
        self,
        message_id: int,
        question: str,
        answer: str,
        category: Optional[str] = None,
        user_id: Optional[int] = None
    ) -> ConversationFeedback:
        """
        Mark a Q&A pair for FAQ extraction.

        Args:
            message_id: Message ID (usually the answer)
            question: The extracted question
            answer: The extracted answer
            category: Optional FAQ category
            user_id: User who marked this as FAQ

        Returns:
            Created/updated ConversationFeedback
        """
        message = self.db.query(ChatMessage).filter(
            ChatMessage.id == message_id
        ).first()

        if not message:
            raise ValueError(f"Message {message_id} not found")

        # Check if feedback already exists
        feedback = self.db.query(ConversationFeedback).filter(
            ConversationFeedback.message_id == message_id
        ).first()

        if feedback:
            # Update existing
            feedback.should_be_faq = True
            feedback.extracted_question = question
            feedback.extracted_answer = answer
            feedback.faq_category = category
        else:
            # Create new
            feedback = ConversationFeedback(
                session_id=message.session_id,
                message_id=message_id,
                should_be_faq=True,
                extracted_question=question,
                extracted_answer=answer,
                faq_category=category,
                submitted_by_user_id=user_id
            )
            self.db.add(feedback)

        self.db.commit()
        self.db.refresh(feedback)

        return feedback

    def get_faq_candidates(
        self,
        project_id: int,
        reviewed: Optional[bool] = None,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 50
    ) -> Dict[str, Any]:
        """
        Get FAQ candidates for review.

        Args:
            project_id: Project ID
            reviewed: Filter by reviewed status
            category: Filter by category
            page: Page number
            page_size: Page size

        Returns:
            Dict with FAQ candidates and pagination
        """
        # Get sessions for this project
        session_ids = self.db.query(ChatSession.id).join(Chatbot).filter(
            Chatbot.project_id == project_id
        ).subquery()

        query = self.db.query(ConversationFeedback).filter(
            ConversationFeedback.session_id.in_(session_ids),
            ConversationFeedback.should_be_faq == True
        )

        if reviewed is not None:
            query = query.filter(ConversationFeedback.reviewed == reviewed)

        if category:
            query = query.filter(ConversationFeedback.faq_category == category)

        total = query.count()

        offset = (page - 1) * page_size
        candidates = query.order_by(
            ConversationFeedback.created_at.desc()
        ).offset(offset).limit(page_size).all()

        return {
            "candidates": candidates,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }

    def review_faq(
        self,
        feedback_id: int,
        approved: bool,
        user_id: int,
        notes: Optional[str] = None,
        updated_question: Optional[str] = None,
        updated_answer: Optional[str] = None,
        category: Optional[str] = None
    ) -> ConversationFeedback:
        """
        Review a FAQ candidate.

        Args:
            feedback_id: Feedback ID
            approved: Whether to approve as FAQ
            user_id: Reviewer user ID
            notes: Review notes
            updated_question: Updated question text
            updated_answer: Updated answer text
            category: Updated category

        Returns:
            Updated ConversationFeedback
        """
        feedback = self.db.query(ConversationFeedback).filter(
            ConversationFeedback.id == feedback_id
        ).first()

        if not feedback:
            raise ValueError(f"Feedback {feedback_id} not found")

        feedback.reviewed = True
        feedback.reviewed_by = user_id
        feedback.reviewed_at = datetime.utcnow()
        feedback.review_notes = notes
        feedback.should_be_faq = approved

        if updated_question:
            feedback.extracted_question = updated_question
        if updated_answer:
            feedback.extracted_answer = updated_answer
        if category:
            feedback.faq_category = category

        self.db.commit()
        self.db.refresh(feedback)

        return feedback

    def export_training_data(
        self,
        project_id: int,
        format: str = "json",
        include_metadata: bool = True,
        only_approved: bool = True
    ) -> Dict[str, Any]:
        """
        Export conversation data for FAQ/LLM training.

        Args:
            project_id: Project ID
            format: Export format (json, csv)
            include_metadata: Whether to include metadata
            only_approved: Only include approved FAQs

        Returns:
            Exported data
        """
        # Get all chatbots for this project
        chatbots = self.db.query(Chatbot).filter(
            Chatbot.project_id == project_id
        ).all()

        chatbot_ids = [c.id for c in chatbots]

        # Get sessions
        sessions = self.db.query(ChatSession).filter(
            ChatSession.chatbot_id.in_(chatbot_ids)
        ).all()

        conversations = []

        for session in sessions:
            messages = self.db.query(ChatMessage).filter(
                ChatMessage.session_id == session.id
            ).order_by(ChatMessage.timestamp.asc()).all()

            # Get feedback for this session
            feedback_list = self.db.query(ConversationFeedback).filter(
                ConversationFeedback.session_id == session.id
            ).all()

            feedback_map = {f.message_id: f for f in feedback_list}

            # Check if session has any helpful feedback
            is_helpful = any(f.is_helpful for f in feedback_list if f.is_helpful is not None)
            has_human = session.human_takeover

            conv_messages = []
            for msg in messages:
                role = "user" if msg.role == "user" or msg.sender_type == "customer" else "assistant"
                conv_messages.append({
                    "role": role,
                    "content": msg.content
                })

            conv_data = {
                "id": f"session_{session.id}",
                "channel": session.channel,
                "messages": conv_messages
            }

            if include_metadata:
                conv_data["metadata"] = {
                    "helpful": is_helpful,
                    "human_intervention": has_human,
                    "message_count": len(messages),
                    "created_at": session.started_at.isoformat()
                }

            conversations.append(conv_data)

        # Get approved FAQs
        faqs = []
        if only_approved:
            faq_feedback = self.db.query(ConversationFeedback).join(ChatSession).filter(
                ChatSession.chatbot_id.in_(chatbot_ids),
                ConversationFeedback.should_be_faq == True,
                ConversationFeedback.reviewed == True
            ).all()
        else:
            faq_feedback = self.db.query(ConversationFeedback).join(ChatSession).filter(
                ChatSession.chatbot_id.in_(chatbot_ids),
                ConversationFeedback.should_be_faq == True
            ).all()

        for f in faq_feedback:
            if f.extracted_question and f.extracted_answer:
                faqs.append({
                    "question": f.extracted_question,
                    "answer": f.extracted_answer,
                    "category": f.faq_category
                })

        result = {
            "project_id": project_id,
            "exported_at": datetime.utcnow().isoformat(),
            "conversations": conversations,
            "faqs": faqs,
            "stats": {
                "total_conversations": len(conversations),
                "total_faqs": len(faqs)
            }
        }

        if format == "csv":
            # Convert to CSV format
            return self._convert_to_csv(result)

        return result

    def _convert_to_csv(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Convert export data to CSV format."""
        import csv
        import io

        # Conversations CSV
        conv_output = io.StringIO()
        conv_writer = csv.writer(conv_output)
        conv_writer.writerow(["id", "channel", "messages", "helpful", "human_intervention"])

        for conv in data["conversations"]:
            conv_writer.writerow([
                conv["id"],
                conv["channel"],
                json.dumps(conv["messages"]),
                conv.get("metadata", {}).get("helpful", ""),
                conv.get("metadata", {}).get("human_intervention", "")
            ])

        # FAQs CSV
        faq_output = io.StringIO()
        faq_writer = csv.writer(faq_output)
        faq_writer.writerow(["question", "answer", "category"])

        for faq in data["faqs"]:
            faq_writer.writerow([
                faq["question"],
                faq["answer"],
                faq.get("category", "")
            ])

        return {
            "conversations_csv": conv_output.getvalue(),
            "faqs_csv": faq_output.getvalue(),
            "stats": data["stats"]
        }

    def get_categories(self, project_id: int) -> List[str]:
        """
        Get all FAQ categories for a project.

        Args:
            project_id: Project ID

        Returns:
            List of unique categories
        """
        chatbot_ids = self.db.query(Chatbot.id).filter(
            Chatbot.project_id == project_id
        ).subquery()

        session_ids = self.db.query(ChatSession.id).filter(
            ChatSession.chatbot_id.in_(chatbot_ids)
        ).subquery()

        categories = self.db.query(ConversationFeedback.faq_category).filter(
            ConversationFeedback.session_id.in_(session_ids),
            ConversationFeedback.faq_category.isnot(None),
            ConversationFeedback.faq_category != ""
        ).distinct().all()

        return [c[0] for c in categories]


def get_faq_extraction_service(db: Session) -> FAQExtractionService:
    """Factory function to get FAQExtractionService instance."""
    return FAQExtractionService(db)
