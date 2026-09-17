"""
SDK Config Service
Provides runtime configuration for the Versya SDK.
"""
from datetime import datetime
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session

from app.models.messaging import (
    MessagingDomain, MessagingEventSchema, MessagingDestination, MessagingTrackingDomain
)
from app.models import ProjectPersonalizationConfig


class SDKConfigService:
    """
    Builds runtime configuration for the Versya SDK.

    This is fetched by the SDK on initialization and provides:
    - Event schemas
    - No-code mappings
    - DataLayer settings
    - Consent requirements
    """

    async def get_sdk_config(
        self,
        db: Session,
        project_id: int,
        domain_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Build complete SDK config for a project.
        """
        # Get domain settings
        domain = None
        if domain_id:
            domain = db.query(MessagingDomain).filter(
                MessagingDomain.id == domain_id
            ).first()

        # Get active event schemas
        event_schemas = db.query(MessagingEventSchema).filter(
            MessagingEventSchema.project_id == project_id,
            MessagingEventSchema.is_active == True
        ).all()

        # Get active destinations
        destinations = db.query(MessagingDestination).filter(
            MessagingDestination.project_id == project_id,
            MessagingDestination.is_active == True
        ).all()

        # Get personalization config for SPA tracking flag
        perso_config = db.query(ProjectPersonalizationConfig).filter(
            ProjectPersonalizationConfig.project_id == project_id
        ).first()
        auto_track_spa = perso_config.auto_track_spa_pages if perso_config else False

        # Build config
        config = {
            "version": datetime.utcnow().isoformat() + "Z",
            "projectId": project_id,

            # Event definitions
            "eventSchemas": [
                {
                    "name": schema.event_name,
                    "displayName": schema.display_name,
                    "category": schema.category,
                    "properties": schema.properties_schema.get("properties", {}) if schema.properties_schema else {},
                    "required": schema.required_properties or []
                }
                for schema in event_schemas
            ],

            # No-code mappings (to be populated from chrome extension data)
            "noCodeMappings": await self.get_no_code_mappings(db, project_id),

            # DataLayer settings
            "dataLayer": {
                "push": True,  # Push events to dataLayer for GTM
                "listen": False,  # Listen to existing dataLayer.push
                "format": "ga4"  # Event format
            },

            # Consent requirements
            "consent": {
                "required": self._has_consent_required_destinations(destinations),
                "waitForConsent": False,  # Don't block events while waiting
                "types": self._get_required_consent_types(destinations)
            },

            # First-party tracking proxy. Existing SDK installs can read this
            # and send future calls through the customer's tracking domain.
            "tracking": self._get_tracking_proxy_config(db, project_id, domain),

            # Settings
            "settings": {
                "autoTrack": {
                    "pageViews": True,
                    "spaPages": auto_track_spa,
                    "outboundLinks": False,
                    "fileDownloads": False,
                    "scrollDepth": False
                },
                "debug": False
            }
        }

        return config

    def _get_tracking_proxy_config(
        self,
        db: Session,
        project_id: int,
        domain: Optional[MessagingDomain],
    ) -> Optional[Dict[str, Any]]:
        if not domain:
            return None

        tracking_domains = db.query(MessagingTrackingDomain).filter(
            MessagingTrackingDomain.project_id == project_id,
            MessagingTrackingDomain.domain_id == domain.id,
            MessagingTrackingDomain.cookie_keeper_enabled == True,
        ).order_by(MessagingTrackingDomain.updated_at.desc()).all()

        if not tracking_domains:
            return None

        preferred = next(
            (
                td for td in tracking_domains
                if (td.proxy_status or "").lower() in {"seen", "active"}
            ),
            None,
        )

        if not preferred:
            return None

        return {
            "hostname": preferred.hostname,
            "proxyUrl": f"https://{preferred.hostname}",
            "cookieKeeperEnabled": preferred.cookie_keeper_enabled,
            "configVersion": preferred.config_version,
        }

    async def get_no_code_mappings(
        self,
        db: Session,
        project_id: int
    ) -> List[Dict[str, Any]]:
        """
        Get published no-code element mappings from latest config snapshot.
        """
        from app.services.nocode_mapping_service import NoCodeMappingService
        svc = NoCodeMappingService(db)
        return svc.get_published_mappings(project_id)

    def _has_consent_required_destinations(
        self,
        destinations: List[MessagingDestination]
    ) -> bool:
        """Check if any destination requires consent."""
        for dest in destinations:
            if dest.consent_required:
                return True
        return False

    def _get_required_consent_types(
        self,
        destinations: List[MessagingDestination]
    ) -> List[str]:
        """Get all unique consent types required by destinations."""
        consent_types = set()
        for dest in destinations:
            if dest.consent_required:
                consent_types.update(dest.consent_required)
        return list(consent_types)


# Singleton instance
sdk_config_service = SDKConfigService()
