"""
Visitor Data Service

Resolves anonymous visitors to user data for cross-subdomain personalization.
Uses MessagingAnonymousProfile.merged_to_user_id for identity resolution.
"""
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any

from app.models import (
    ProjectPersonalizationConfig,
    ScoreDefinition,
    UserScoreSnapshot,
)
from app.models.messaging import MessagingAnonymousProfile, MessagingUser


class VisitorDataService:
    def __init__(self, db: Session):
        self.db = db

    def get_config(self, project_id: int) -> Optional[ProjectPersonalizationConfig]:
        return self.db.query(ProjectPersonalizationConfig).filter(
            ProjectPersonalizationConfig.project_id == project_id
        ).first()

    def resolve_visitor(self, project_id: int, anonymous_id: str) -> Optional[Dict[str, Any]]:
        """
        Resolve anonymous_id → user data.

        1. Look up MessagingAnonymousProfile by anonymous_id
        2. If merged_to_user_id → get MessagingUser
        3. Filter traits by config.exposed_traits
        4. Include scores if config.expose_scores
        """
        config = self.get_config(project_id)
        if not config or not config.enabled:
            return None

        # Find anonymous profile
        profile = self.db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == anonymous_id
        ).first()

        if not profile:
            return {"anonymous": True, "traits": {}, "scores": []}

        if not profile.merged_to_user_id:
            return {"anonymous": True, "traits": {}, "scores": []}

        # Get the linked user
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == profile.merged_to_user_id
        ).first()

        if not user:
            return {"anonymous": True, "traits": {}, "scores": []}

        # Check consent if required
        if config.require_analytics_consent:
            if not getattr(user, 'consent_analytics', False):
                return None

        # Build traits from user properties
        traits: Dict[str, Any] = {}
        user_props = user.properties or {}

        if config.exposed_traits:
            for field_name in config.exposed_traits:
                if field_name in user_props:
                    traits[field_name] = user_props[field_name]

        if config.expose_name and user.name:
            traits["name"] = user.name

        if config.expose_email and user.email:
            traits["email"] = user.email

        # Build scores
        scores = []
        if config.expose_scores:
            snapshots = self.db.query(UserScoreSnapshot, ScoreDefinition).join(
                ScoreDefinition,
                UserScoreSnapshot.score_definition_id == ScoreDefinition.id
            ).filter(
                UserScoreSnapshot.project_id == project_id,
                UserScoreSnapshot.user_id == user.id,
                ScoreDefinition.status == "active"
            ).all()

            for snapshot, definition in snapshots:
                scores.append({
                    "slug": definition.slug,
                    "name": definition.name,
                    "score": snapshot.score,
                    "tier": snapshot.tier,
                })

        return {
            "anonymous": False,
            "traits": traits,
            "scores": scores,
        }
