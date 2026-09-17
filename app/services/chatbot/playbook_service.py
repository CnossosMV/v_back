"""
Playbook Service

Manages playbook templates and team creation from playbooks.
"""

import logging
from typing import List, Optional, Dict
from sqlalchemy.orm import Session

from app.models import (
    TeamPlaybook, AgentTeam, SpecialistAgent, RouterConfig, RoutingRule,
)

logger = logging.getLogger(__name__)


class PlaybookService:
    """Service for managing playbook templates."""

    def __init__(self, db: Session):
        self.db = db

    def list_playbooks(self, category: Optional[str] = None) -> List[TeamPlaybook]:
        """Return all active playbooks, optionally filtered by category."""
        query = self.db.query(TeamPlaybook).filter(TeamPlaybook.is_active == True)
        if category:
            query = query.filter(TeamPlaybook.category == category)
        return query.order_by(TeamPlaybook.display_order.asc()).all()

    def get_playbook(self, slug: str) -> Optional[TeamPlaybook]:
        """Get a playbook by its slug."""
        return self.db.query(TeamPlaybook).filter(
            TeamPlaybook.slug == slug,
            TeamPlaybook.is_active == True,
        ).first()

    def get_playbook_preview(self, slug: str) -> Optional[Dict]:
        """Return the template data with agent breakdown for preview."""
        playbook = self.get_playbook(slug)
        if not playbook:
            return None
        return {
            "id": playbook.id,
            "name": playbook.name,
            "slug": playbook.slug,
            "description": playbook.description,
            "category": playbook.category,
            "icon": playbook.icon,
            "agent_count": playbook.agent_count,
            "template_data": playbook.template_data,
        }

    def create_team_from_playbook(
        self,
        project_id: int,
        slug: str,
        customizations: Optional[Dict] = None,
        created_by: Optional[int] = None,
    ) -> AgentTeam:
        """
        Instantiate an AgentTeam from a playbook template.

        Creates the team, all specialists (with placeholder prompts),
        router config, and routing rules from the template data.
        """
        playbook = self.get_playbook(slug)
        if not playbook:
            raise ValueError(f"Playbook '{slug}' not found")

        template = playbook.template_data or {}
        custom = customizations or {}

        # Create team
        team_name = custom.get("name", f"{playbook.name} Team")
        team = AgentTeam(
            project_id=project_id,
            name=team_name,
            description=custom.get("description", playbook.description),
            deployment_channels=custom.get("deployment_channels", []),
            team_metadata={"from_playbook": playbook.slug},
            created_by=created_by,
        )
        self.db.add(team)
        self.db.flush()

        # Create specialists from template
        specialist_templates = template.get("specialists", [])
        specialists = []
        for idx, spec_tpl in enumerate(specialist_templates):
            specialist = SpecialistAgent(
                team_id=team.id,
                name=spec_tpl.get("name", f"Specialist {idx + 1}"),
                description=spec_tpl.get("description"),
                icon=spec_tpl.get("icon", "🤖"),
                is_default=spec_tpl.get("is_default", idx == 0),
                position_x=100 + idx * 250,
                position_y=300,
                system_prompt=spec_tpl.get("system_prompt", ""),
                model_provider=spec_tpl.get("model_provider", "openai"),
                model_name=spec_tpl.get("model_name", "gpt-4o-mini"),
                temperature=spec_tpl.get("temperature", 0.7),
                max_tokens=spec_tpl.get("max_tokens", 2000),
                tone=spec_tpl.get("tone", "friendly"),
                language=spec_tpl.get("language"),
                agent_order=idx,
            )
            self.db.add(specialist)
            specialists.append(specialist)

        self.db.flush()

        # Create router config
        router_tpl = template.get("router", {})
        default_agent_id = specialists[0].id if specialists else None

        router = RouterConfig(
            team_id=team.id,
            routing_mode=router_tpl.get("routing_mode", "auto"),
            model_provider=router_tpl.get("model_provider", "openai"),
            model_name=router_tpl.get("model_name", "gpt-4o-mini"),
            default_agent_id=default_agent_id,
            allow_mid_convo_switch=router_tpl.get("allow_mid_convo_switch", True),
            switch_notification=router_tpl.get("switch_notification", "seamless"),
            position_x=300,
            position_y=100,
        )
        self.db.add(router)
        self.db.flush()

        # Create routing rules from template (if any)
        rule_templates = router_tpl.get("routing_rules", [])
        for rule_tpl in rule_templates:
            # Find specialist by name
            target_name = rule_tpl.get("specialist_name", "")
            target = next(
                (s for s in specialists if s.name == target_name),
                None,
            )
            if target:
                rule = RoutingRule(
                    router_id=router.id,
                    specialist_id=target.id,
                    description=rule_tpl.get("description", ""),
                    priority=rule_tpl.get("priority", 0),
                    keyword_hints=rule_tpl.get("keyword_hints"),
                    is_active=True,
                )
                self.db.add(rule)

        self.db.commit()
        self.db.refresh(team)

        logger.info(
            "Created team %d from playbook '%s' with %d specialists",
            team.id, slug, len(specialists),
        )
        return team
