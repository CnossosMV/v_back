"""
Specialist Service

Manages specialist agent CRUD and knowledge source management.
Reuses VectorStoreService and KnowledgeLoader for document processing.
"""

import os
import logging
import hashlib
from typing import List, Dict, Optional
from pathlib import Path
from sqlalchemy.orm import Session

from app.models import SpecialistAgent, SpecialistKnowledgeSource, AgentTeam

logger = logging.getLogger(__name__)


class SpecialistService:
    """Service for managing specialist agents and their knowledge bases."""

    def __init__(self, db: Session):
        self.db = db

    def get_specialist(self, specialist_id: int) -> Optional[SpecialistAgent]:
        """Get a specialist agent by ID."""
        return self.db.query(SpecialistAgent).filter(
            SpecialistAgent.id == specialist_id
        ).first()

    def create_specialist(self, team_id: int, data: Dict) -> SpecialistAgent:
        """Create a new specialist agent."""
        team = self.db.query(AgentTeam).filter(AgentTeam.id == team_id).first()
        if not team:
            raise ValueError("Agent team not found")

        specialist = SpecialistAgent(
            team_id=team_id,
            name=data["name"],
            description=data.get("description"),
            icon=data.get("icon"),
            is_default=data.get("is_default", False),
            position_x=data.get("position_x", 0.0),
            position_y=data.get("position_y", 0.0),
            system_prompt=data.get("system_prompt"),
            model_provider=data.get("model_provider", "openai"),
            model_name=data.get("model_name", "gpt-4o-mini"),
            temperature=data.get("temperature", 0.7),
            max_tokens=data.get("max_tokens", 2000),
            tone=data.get("tone", "friendly"),
            language=data.get("language"),
            max_turns=data.get("max_turns"),
            frustration_action=data.get("frustration_action", "escalate"),
            blocked_topics=data.get("blocked_topics", []),
            operating_hours=data.get("operating_hours"),
            escalation_config=data.get("escalation_config"),
        )

        # If this is the first specialist, make it default
        existing_count = self.db.query(SpecialistAgent).filter(
            SpecialistAgent.team_id == team_id
        ).count()
        if existing_count == 0:
            specialist.is_default = True

        self.db.add(specialist)
        self.db.commit()
        self.db.refresh(specialist)

        logger.info(f"Created specialist {specialist.id} for team {team_id}")
        return specialist

    def update_specialist(self, specialist_id: int, data: Dict) -> Optional[SpecialistAgent]:
        """Update a specialist agent."""
        specialist = self.get_specialist(specialist_id)
        if not specialist:
            return None

        for key, value in data.items():
            if value is not None and hasattr(specialist, key):
                setattr(specialist, key, value)

        self.db.commit()
        self.db.refresh(specialist)

        logger.info(f"Updated specialist {specialist_id}")
        return specialist

    def delete_specialist(self, specialist_id: int) -> bool:
        """Delete a specialist agent."""
        specialist = self.get_specialist(specialist_id)
        if not specialist:
            return False

        self.db.delete(specialist)
        self.db.commit()

        logger.info(f"Deleted specialist {specialist_id}")
        return True

    # ========================================================================
    # Knowledge Source Management
    # ========================================================================

    def list_knowledge_sources(self, specialist_id: int) -> List[SpecialistKnowledgeSource]:
        """List all knowledge sources for a specialist."""
        return self.db.query(SpecialistKnowledgeSource).filter(
            SpecialistKnowledgeSource.specialist_id == specialist_id
        ).order_by(SpecialistKnowledgeSource.created_at.desc()).all()

    def add_knowledge_from_file(
        self,
        specialist_id: int,
        file_path: str,
        file_name: str,
        file_type: str,
        content: Optional[str] = None,
        label: Optional[str] = None,
        expose_to_user: bool = False,
    ) -> SpecialistKnowledgeSource:
        """Add a file-based knowledge source to a specialist."""
        content_hash = None
        if content:
            content_hash = hashlib.sha256(content.encode()).hexdigest()

        source = SpecialistKnowledgeSource(
            specialist_id=specialist_id,
            source_type=file_type if file_type in ["pdf", "image"] else "internal_doc",
            file_path=file_path,
            file_name=file_name,
            file_type=file_type,
            content=content,
            content_hash=content_hash,
            label=label or file_name,
            expose_to_user=expose_to_user,
            processed=False,
        )

        self.db.add(source)
        self.db.commit()
        self.db.refresh(source)

        logger.info(f"Added file knowledge source {source.id} to specialist {specialist_id}")
        return source

    def add_knowledge_from_url(
        self,
        specialist_id: int,
        source_url: str,
        source_type: str = "website_url",
        label: Optional[str] = None,
        expose_to_user: bool = False,
    ) -> SpecialistKnowledgeSource:
        """Add a URL-based knowledge source to a specialist."""
        source = SpecialistKnowledgeSource(
            specialist_id=specialist_id,
            source_type=source_type,
            source_url=source_url,
            label=label or source_url,
            expose_to_user=expose_to_user,
            processed=False,
        )

        self.db.add(source)
        self.db.commit()
        self.db.refresh(source)

        logger.info(f"Added URL knowledge source {source.id} to specialist {specialist_id}")
        return source

    def delete_knowledge_source(self, source_id: int) -> bool:
        """Delete a knowledge source."""
        source = self.db.query(SpecialistKnowledgeSource).filter(
            SpecialistKnowledgeSource.id == source_id
        ).first()

        if not source:
            return False

        self.db.delete(source)
        self.db.commit()

        logger.info(f"Deleted knowledge source {source_id}")
        return True

    def process_knowledge_source(
        self,
        source_id: int,
        vector_store_service,
        knowledge_loader,
    ) -> SpecialistKnowledgeSource:
        """
        Process a knowledge source: load content, create embeddings, store in vector store.
        Reuses existing VectorStoreService and KnowledgeLoader.
        """
        source = self.db.query(SpecialistKnowledgeSource).filter(
            SpecialistKnowledgeSource.id == source_id
        ).first()

        if not source:
            raise ValueError("Knowledge source not found")

        specialist = self.get_specialist(source.specialist_id)
        if not specialist:
            raise ValueError("Specialist not found")

        try:
            documents = []

            if source.file_path and os.path.exists(source.file_path):
                # Load from file
                documents = knowledge_loader.load_file(source.file_path)
            elif source.source_url:
                # Load from URL
                documents = knowledge_loader.load_url(source.source_url)
            elif source.content:
                # Load from raw text content
                from langchain.schema import Document
                documents = [Document(
                    page_content=source.content,
                    metadata={"source": "text_input", "specialist_id": specialist.id}
                )]

            if documents:
                # Use specialist_id as the collection identifier (like chatbot_id)
                collection_id = f"specialist_{specialist.id}"
                chunk_count = vector_store_service.add_documents(
                    chatbot_id=specialist.id,  # Reuse existing method with specialist_id
                    documents=documents,
                    document_id=source.id,
                )

                source.chunk_count = chunk_count
                source.embedding_model = "text-embedding-3-small"
                source.processed = True
                source.processing_error = None

                if documents and documents[0].page_content:
                    content_hash = hashlib.sha256(
                        documents[0].page_content.encode()
                    ).hexdigest()
                    source.content_hash = content_hash
            else:
                source.processing_error = "No documents could be loaded from source"

        except Exception as e:
            logger.error(f"Error processing knowledge source {source_id}: {e}")
            source.processing_error = str(e)
            source.processed = False

        self.db.commit()
        self.db.refresh(source)
        return source
