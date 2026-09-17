"""
Router Config Router

Endpoints for managing router configuration and routing rules.
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AgentTeam, RouterConfig, RoutingRule, SpecialistAgent
from app.schemas.agent_teams import (
    RouterConfigUpdate,
    RouterConfigResponse,
    RoutingRuleCreate,
    RoutingRuleUpdate,
    RoutingRuleResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(tags=["router-config"])


# ============================================================================
# Router Config
# ============================================================================

@router.get("/agent-teams/{team_id}/router", response_model=RouterConfigResponse)
async def get_router_config(
    team_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get router configuration for a team, including routing rules."""
    team = db.query(AgentTeam).filter(AgentTeam.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")

    config = db.query(RouterConfig).filter(RouterConfig.team_id == team_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Router config not found")

    return config


@router.put("/agent-teams/{team_id}/router", response_model=RouterConfigResponse)
async def update_router_config(
    team_id: int,
    config_data: RouterConfigUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update router configuration for a team."""
    config = db.query(RouterConfig).filter(RouterConfig.team_id == team_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Router config not found")

    # Validate default_agent_id belongs to the team if provided
    update_data = config_data.model_dump(exclude_unset=True)
    if "default_agent_id" in update_data and update_data["default_agent_id"] is not None:
        agent = db.query(SpecialistAgent).filter(
            SpecialistAgent.id == update_data["default_agent_id"],
            SpecialistAgent.team_id == team_id,
        ).first()
        if not agent:
            raise HTTPException(
                status_code=400,
                detail="default_agent_id must belong to this team",
            )

    for key, value in update_data.items():
        if hasattr(config, key):
            setattr(config, key, value)

    db.commit()
    db.refresh(config)
    return config


# ============================================================================
# Routing Rules
# ============================================================================

@router.post(
    "/agent-teams/{team_id}/router/rules",
    response_model=RoutingRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_routing_rule(
    team_id: int,
    rule_data: RoutingRuleCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create or update a routing rule for a team's router."""
    config = db.query(RouterConfig).filter(RouterConfig.team_id == team_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Router config not found")

    # Validate specialist belongs to team
    specialist = db.query(SpecialistAgent).filter(
        SpecialistAgent.id == rule_data.specialist_id,
        SpecialistAgent.team_id == team_id,
    ).first()
    if not specialist:
        raise HTTPException(status_code=400, detail="Specialist does not belong to this team")

    # Check for existing rule (upsert)
    existing = db.query(RoutingRule).filter(
        RoutingRule.router_id == config.id,
        RoutingRule.specialist_id == rule_data.specialist_id,
    ).first()

    if existing:
        # Update existing rule
        existing.description = rule_data.description
        existing.priority = rule_data.priority
        existing.keyword_hints = rule_data.keyword_hints
        existing.is_active = rule_data.is_active
        db.commit()
        db.refresh(existing)
        return existing

    # Create new rule
    rule = RoutingRule(
        router_id=config.id,
        specialist_id=rule_data.specialist_id,
        description=rule_data.description,
        priority=rule_data.priority,
        keyword_hints=rule_data.keyword_hints,
        is_active=rule_data.is_active,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.put("/routing-rules/{rule_id}", response_model=RoutingRuleResponse)
async def update_routing_rule(
    rule_id: int,
    rule_data: RoutingRuleUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update a routing rule."""
    rule = db.query(RoutingRule).filter(RoutingRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Routing rule not found")

    update_data = rule_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(rule, key):
            setattr(rule, key, value)

    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/routing-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_routing_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete a routing rule."""
    rule = db.query(RoutingRule).filter(RoutingRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Routing rule not found")

    db.delete(rule)
    db.commit()
