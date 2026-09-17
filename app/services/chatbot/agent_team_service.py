"""
Agent Team Service

Manages Agent Team CRUD operations and chatbot migration.
"""

import logging
from typing import List, Dict, Optional
from sqlalchemy.orm import Session

from app.models import (
    AgentTeam, SpecialistAgent, SpecialistKnowledgeSource,
    RouterConfig, RoutingRule, Chatbot, KnowledgeDocument, Project
)

logger = logging.getLogger(__name__)


class AgentTeamService:
    """Service for managing Agent Teams."""

    def __init__(self, db: Session):
        self.db = db

    def list_teams(self, project_id: int) -> List[AgentTeam]:
        """List all agent teams for a project."""
        return self.db.query(AgentTeam).filter(
            AgentTeam.project_id == project_id
        ).order_by(AgentTeam.created_at.desc()).all()

    def get_team(self, team_id: int) -> Optional[AgentTeam]:
        """Get an agent team by ID with all relationships."""
        return self.db.query(AgentTeam).filter(
            AgentTeam.id == team_id
        ).first()

    def create_team(
        self,
        project_id: int,
        data: Dict,
        created_by: Optional[int] = None
    ) -> AgentTeam:
        """Create a new agent team with a default router config."""
        team = AgentTeam(
            project_id=project_id,
            name=data["name"],
            description=data.get("description"),
            deployment_channels=data.get("deployment_channels", []),
            initial_message=data.get("initial_message"),
            whatsapp_instance_id=data.get("whatsapp_instance_id"),
            auto_respond_whatsapp=data.get("auto_respond_whatsapp", False),
            is_public=data.get("is_public", False),
            team_metadata=data.get("team_metadata", {}),
            created_by=created_by,
        )

        self.db.add(team)
        self.db.flush()  # Get the team ID

        # Create handler_channel_link record if whatsapp_instance_id provided
        if data.get("whatsapp_instance_id"):
            from app.services.handler_channel_link_service import HandlerChannelLinkService
            link_svc = HandlerChannelLinkService(self.db)
            link_svc.set_link("agent_team", team.id, "whatsapp", instance_id=data["whatsapp_instance_id"])

        # Auto-create router config
        router_config = RouterConfig(
            team_id=team.id,
            routing_mode="auto",
            model_provider="openai",
            model_name="gpt-4o-mini",
        )
        self.db.add(router_config)

        self.db.commit()
        self.db.refresh(team)

        logger.info(f"Created agent team {team.id} for project {project_id}")
        return team

    def update_team(self, team_id: int, data: Dict) -> Optional[AgentTeam]:
        """Update an agent team."""
        team = self.get_team(team_id)
        if not team:
            return None

        nullable_fields = {"whatsapp_instance_id"}
        for key, value in data.items():
            if not hasattr(team, key):
                continue
            if value is not None or key in nullable_fields:
                setattr(team, key, value)

        # Sync handler_channel_links when whatsapp_instance_id changes
        if "whatsapp_instance_id" in data:
            from app.services.handler_channel_link_service import HandlerChannelLinkService
            link_svc = HandlerChannelLinkService(self.db)
            if data["whatsapp_instance_id"]:
                link_svc.set_link("agent_team", team_id, "whatsapp", instance_id=data["whatsapp_instance_id"])
            else:
                link_svc.remove_link("agent_team", team_id, "whatsapp")

        self.db.commit()
        self.db.refresh(team)

        logger.info(f"Updated agent team {team_id}")
        return team

    def delete_team(self, team_id: int) -> bool:
        """Delete an agent team and all related data."""
        team = self.get_team(team_id)
        if not team:
            return False

        self.db.delete(team)
        self.db.commit()

        logger.info(f"Deleted agent team {team_id}")
        return True

    def deploy_team(self, team_id: int) -> Optional[AgentTeam]:
        """Set team status to active."""
        team = self.get_team(team_id)
        if not team:
            return None

        # Validate team has at least one specialist
        specialist_count = self.db.query(SpecialistAgent).filter(
            SpecialistAgent.team_id == team_id,
            SpecialistAgent.status == "active"
        ).count()

        if specialist_count == 0:
            raise ValueError("Team must have at least one active specialist to deploy")

        team.status = "active"
        self.db.commit()
        self.db.refresh(team)

        logger.info(f"Deployed agent team {team_id}")
        return team

    def pause_team(self, team_id: int) -> Optional[AgentTeam]:
        """Set team status to paused."""
        team = self.get_team(team_id)
        if not team:
            return None

        team.status = "paused"
        self.db.commit()
        self.db.refresh(team)

        logger.info(f"Paused agent team {team_id}")
        return team

    def migrate_from_chatbot(
        self,
        project_id: int,
        chatbot_id: int,
        team_name: Optional[str] = None,
        created_by: Optional[int] = None
    ) -> Dict:
        """
        Migrate an existing chatbot into an Agent Team with a single specialist.

        Copies: model config, system_prompt, knowledge documents.
        Returns: dict with team, specialist, and migration stats.
        """
        # Get chatbot
        chatbot = self.db.query(Chatbot).filter(
            Chatbot.id == chatbot_id,
            Chatbot.project_id == project_id
        ).first()

        if not chatbot:
            raise ValueError("Chatbot not found in this project")

        # Check if already migrated
        existing = self.db.query(AgentTeam).filter(
            AgentTeam.migrated_from_chatbot_id == chatbot_id
        ).first()
        if existing:
            raise ValueError("This chatbot has already been migrated to an agent team")

        # Create team
        team = AgentTeam(
            project_id=project_id,
            name=team_name or f"{chatbot.name} Team",
            description=f"Migrated from chatbot: {chatbot.name}",
            status="draft",
            deployment_channels=["web"],
            initial_message=None,
            whatsapp_instance_id=chatbot.whatsapp_instance_id,
            auto_respond_whatsapp=chatbot.auto_respond_whatsapp,
            is_public=chatbot.is_public,
            migrated_from_chatbot_id=chatbot_id,
            created_by=created_by,
        )
        self.db.add(team)
        self.db.flush()

        # Copy handler_channel_links from chatbot to new team
        from app.services.handler_channel_link_service import HandlerChannelLinkService
        link_svc = HandlerChannelLinkService(self.db)
        chatbot_links = link_svc.get_links("chatbot", chatbot_id)
        for cl in chatbot_links:
            link_svc.set_link("agent_team", team.id, cl.channel, instance_id=cl.instance_id, config=cl.config, is_primary=cl.is_primary)

        # Create router config
        router_config = RouterConfig(
            team_id=team.id,
            routing_mode="auto",
            model_provider="openai",
            model_name="gpt-4o-mini",
        )
        self.db.add(router_config)

        # Create specialist from chatbot config
        specialist = SpecialistAgent(
            team_id=team.id,
            name=chatbot.name,
            description=chatbot.description or chatbot.intention,
            icon="🤖",
            is_default=True,
            system_prompt=chatbot.system_prompt,
            model_provider=chatbot.model_provider,
            model_name=chatbot.model_name,
            temperature=chatbot.temperature,
            max_tokens=chatbot.max_tokens,
            vector_store_path=chatbot.vector_store_path,
            knowledge_base_path=chatbot.knowledge_base_path,
        )
        self.db.add(specialist)
        self.db.flush()

        # Set as default agent for router
        router_config.default_agent_id = specialist.id

        # Migrate knowledge documents
        knowledge_docs = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.chatbot_id == chatbot_id
        ).all()

        knowledge_count = 0
        for doc in knowledge_docs:
            knowledge_source = SpecialistKnowledgeSource(
                specialist_id=specialist.id,
                source_type=doc.source_type,
                source_url=doc.source_url,
                file_path=doc.file_path,
                file_name=doc.file_name,
                file_type=doc.file_type,
                content=doc.content,
                content_hash=doc.content_hash,
                chunk_count=doc.chunk_count,
                embedding_model=doc.embedding_model,
                processed=doc.processed,
                processing_error=doc.processing_error,
                source_metadata=doc.document_metadata or {},
            )
            self.db.add(knowledge_source)
            knowledge_count += 1

        self.db.commit()
        self.db.refresh(team)
        self.db.refresh(specialist)

        logger.info(
            f"Migrated chatbot {chatbot_id} to team {team.id} "
            f"with {knowledge_count} knowledge sources"
        )

        return {
            "team": team,
            "specialist": specialist,
            "knowledge_sources_migrated": knowledge_count,
        }
