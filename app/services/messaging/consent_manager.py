"""
Consent Manager Service
Handles GDPR/LGPD compliant consent tracking and verification.
"""
from datetime import datetime
from typing import Any, Optional, List, Dict
from dataclasses import dataclass
from sqlalchemy.orm import Session

from app.models.messaging import MessagingAdsConsentEvidence, MessagingUser


@dataclass
class ConsentState:
    """Represents the current consent state for a user."""
    marketing: bool = False
    analytics: bool = False
    given_at: Optional[datetime] = None
    ip: Optional[str] = None
    version: Optional[str] = None
    ad_user_data: bool = False
    ad_personalization: bool = False

    def to_dict(self) -> dict:
        return {
            "marketing": self.marketing,
            "analytics": self.analytics,
            "ad_user_data": self.ad_user_data,
            "ad_personalization": self.ad_personalization,
            "given_at": self.given_at.isoformat() if self.given_at else None,
            "ip": self.ip,
            "version": self.version
        }


class ConsentManager:
    """
    Manages user consent for GDPR/LGPD compliance.

    Consent types:
    - marketing: Allow marketing communications
    - analytics: Allow analytics/tracking
    - ad_user_data: Allow use of user data for ads/audience sync
    - ad_personalization: Allow ad personalization / remarketing use

    Consent is stored per user and can be updated/revoked at any time.
    """

    # Current consent version - increment when consent UI/terms change
    CURRENT_VERSION = "1.0"

    def update_consent(
        self,
        db: Session,
        user: MessagingUser,
        marketing: Optional[bool] = None,
        analytics: Optional[bool] = None,
        ip: Optional[str] = None,
        version: Optional[str] = None
    ) -> MessagingUser:
        """
        Update consent state for a user.

        Only updates fields that are explicitly provided (not None).
        Records the IP address and version for audit trail.
        """
        now = datetime.utcnow()
        consent_changed = False

        if marketing is not None and user.consent_marketing != marketing:
            user.consent_marketing = marketing
            consent_changed = True

        if analytics is not None and user.consent_analytics != analytics:
            user.consent_analytics = analytics
            consent_changed = True

        if consent_changed:
            user.consent_given_at = now
            user.consent_ip = ip
            user.consent_version = version or self.CURRENT_VERSION

        db.commit()
        db.refresh(user)

        return user

    def check_consent(
        self,
        user: MessagingUser,
        required: List[str]
    ) -> bool:
        """
        Check if user has given all required consent types.

        Args:
            user: The messaging user to check
            required: List of required consent types (e.g., ["marketing", "analytics"])

        Returns:
            True if all required consents are granted, False otherwise
        """
        if not required:
            return True

        consent_map = {
            "marketing": user.consent_marketing,
            "analytics": user.consent_analytics,
            "ad_user_data": self.get_ads_consent(user, "ad_user_data"),
            "ad_personalization": self.get_ads_consent(user, "ad_personalization"),
        }

        for consent_type in required:
            if not consent_map.get(consent_type, False):
                return False

        return True

    def get_consent_state(self, user: MessagingUser) -> ConsentState:
        """
        Get the current consent state for a user.
        """
        return ConsentState(
            marketing=user.consent_marketing or False,
            analytics=user.consent_analytics or False,
            ad_user_data=self.get_ads_consent(user, "ad_user_data"),
            ad_personalization=self.get_ads_consent(user, "ad_personalization"),
            given_at=user.consent_given_at,
            ip=user.consent_ip,
            version=user.consent_version
        )

    @staticmethod
    def _ads_entry_status(value: Any) -> Optional[str]:
        if isinstance(value, bool):
            return "granted" if value else "denied"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"granted", "grant", "true", "yes", "1"}:
                return "granted"
            if normalized in {"denied", "deny", "false", "no", "0"}:
                return "denied"
            if normalized in {"withdrawn", "revoked", "revoke"}:
                return "withdrawn"
        if isinstance(value, dict):
            if "status" in value:
                return ConsentManager._ads_entry_status(value.get("status"))
            if "granted" in value:
                return "granted" if bool(value.get("granted")) else "denied"
        return None

    def update_ads_consent(
        self,
        db: Session,
        user: MessagingUser,
        ads_consent: Optional[Dict[str, Any]],
        source: Optional[str] = None,
        policy_version: Optional[str] = None,
        evidence_id: Optional[str] = None,
        page_url: Optional[str] = None,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ) -> MessagingUser:
        """
        Store ads consent scopes and append immutable evidence rows.

        Accepts either:
        {"ad_user_data": true, "ad_personalization": true}
        or {"ads": {"ad_user_data": {"status": "granted"}}}
        """
        if not ads_consent:
            return user

        payload = ads_consent.get("ads") if isinstance(ads_consent.get("ads"), dict) else ads_consent
        channels = dict(user.consent_channels or {})
        ads_state = dict(channels.get("ads") or {})
        now = datetime.utcnow()

        for scope in ("ad_user_data", "ad_personalization"):
            if scope not in payload:
                continue
            raw_entry = payload.get(scope)
            status = self._ads_entry_status(raw_entry)
            if not status:
                continue

            entry_source = source
            entry_policy = policy_version
            entry_evidence_id = evidence_id
            entry_page_url = page_url
            entry_metadata = dict(metadata or {})
            if isinstance(raw_entry, dict):
                entry_source = raw_entry.get("source") or entry_source
                entry_policy = raw_entry.get("policy_version") or raw_entry.get("version") or entry_policy
                entry_evidence_id = raw_entry.get("evidence_id") or entry_evidence_id
                entry_page_url = raw_entry.get("page_url") or raw_entry.get("url") or entry_page_url
                if isinstance(raw_entry.get("metadata"), dict):
                    entry_metadata.update(raw_entry["metadata"])

            ads_state[scope] = {
                "granted": status == "granted",
                "status": status,
                "source": entry_source or "api",
                "policy_version": entry_policy or self.CURRENT_VERSION,
                "evidence_id": entry_evidence_id,
                "page_url": entry_page_url,
                "timestamp": now.isoformat(),
            }
            if ip:
                ads_state[scope]["ip"] = ip

            db.add(MessagingAdsConsentEvidence(
                project_id=user.project_id,
                user_id=user.id,
                scope=scope,
                status=status,
                source=entry_source or "api",
                policy_version=entry_policy or self.CURRENT_VERSION,
                evidence_id=entry_evidence_id,
                captured_at=now,
                ip_address=ip,
                user_agent=(user_agent or "")[:500] if user_agent else None,
                page_url=entry_page_url,
                evidence_metadata=entry_metadata or None,
            ))

        channels["ads"] = ads_state
        user.consent_channels = channels
        user.consent_given_at = now
        user.consent_ip = ip or user.consent_ip
        user.consent_version = policy_version or user.consent_version or self.CURRENT_VERSION

        if commit:
            db.commit()
            db.refresh(user)
        return user

    def get_ads_consent(self, user: MessagingUser, scope: str) -> bool:
        channels = user.consent_channels or {}
        ads = channels.get("ads") if isinstance(channels.get("ads"), dict) else {}
        entry = ads.get(scope) if isinstance(ads, dict) else None
        if isinstance(entry, dict):
            status = entry.get("status")
            if status:
                return status == "granted"
            return bool(entry.get("granted"))
        if scope in channels:
            direct = channels.get(scope)
            if isinstance(direct, dict):
                return bool(direct.get("granted")) or direct.get("status") == "granted"
            return bool(direct)
        return False

    def revoke_all_consent(
        self,
        db: Session,
        user: MessagingUser,
        ip: Optional[str] = None
    ) -> MessagingUser:
        """
        Revoke all consent for a user (used for DSAR requests).
        """
        user.consent_marketing = False
        user.consent_analytics = False
        channels = dict(user.consent_channels or {})
        ads = dict(channels.get("ads") or {})
        now_iso = datetime.utcnow().isoformat()
        for scope in ("ad_user_data", "ad_personalization"):
            ads[scope] = {
                "granted": False,
                "status": "withdrawn",
                "source": "dsar",
                "timestamp": now_iso,
            }
        channels["ads"] = ads
        user.consent_channels = channels
        user.consent_given_at = datetime.utcnow()
        user.consent_ip = ip
        user.consent_version = self.CURRENT_VERSION

        db.commit()
        db.refresh(user)

        return user

    def grant_all_consent(
        self,
        db: Session,
        user: MessagingUser,
        ip: Optional[str] = None,
        version: Optional[str] = None
    ) -> MessagingUser:
        """
        Grant all consent types for a user.
        """
        return self.update_consent(
            db=db,
            user=user,
            marketing=True,
            analytics=True,
            ip=ip,
            version=version
        )

    def update_channel_consent(
        self,
        db: Session,
        user: MessagingUser,
        channel: str,
        granted: bool,
        source: str,
        ip: Optional[str] = None
    ) -> MessagingUser:
        """
        Update per-channel consent in the consent_channels JSONB field.

        Writes to user.consent_channels for the specific channel and keeps
        legacy boolean fields synced when applicable:
        - channel "email" syncs consent_marketing
        - channels "whatsapp" / "sms" do NOT touch legacy fields

        consent_channels format:
        {
            "email": {"granted": true, "source": "sdk", "timestamp": "..."},
            "whatsapp": {"granted": false, "source": "api", "timestamp": "..."}
        }
        """
        now = datetime.utcnow()

        channels = dict(user.consent_channels or {})
        channels[channel] = {
            "granted": granted,
            "source": source,
            "timestamp": now.isoformat(),
        }
        if ip:
            channels[channel]["ip"] = ip

        user.consent_channels = channels

        # Sync legacy boolean for email channel
        if channel == "email":
            user.consent_marketing = granted
            user.consent_given_at = now
            user.consent_ip = ip
            user.consent_version = self.CURRENT_VERSION

        db.commit()
        db.refresh(user)
        return user

    def get_channel_consent(self, user: MessagingUser, channel: str) -> bool:
        """
        Check consent for a specific channel.

        Checks consent_channels JSONB first; falls back to legacy booleans
        for backward compatibility:
        - "email" falls back to consent_marketing
        - "whatsapp" / "sms" / others default to False
        """
        channels = user.consent_channels or {}
        if channel in channels:
            entry = channels[channel]
            if isinstance(entry, dict):
                return bool(entry.get("granted", False))
            return bool(entry)

        # Fallback to legacy booleans
        if channel == "email":
            return bool(user.consent_marketing)

        return False

    def merge_consent_restrictive(
        self,
        winner: MessagingUser,
        loser: MessagingUser
    ) -> Dict[str, dict]:
        """
        Restrictive union of per-channel consent: consent is granted only if
        BOTH contacts had it for that channel.

        Returns the merged consent_channels dict (does NOT persist — caller
        should assign to winner.consent_channels and commit).
        """
        winner_channels: dict = dict(winner.consent_channels or {})
        loser_channels: dict = dict(loser.consent_channels or {})

        all_channel_keys = set(winner_channels.keys()) | set(loser_channels.keys())

        merged: Dict[str, dict] = {}
        now_iso = datetime.utcnow().isoformat()

        for ch in all_channel_keys:
            w_entry = winner_channels.get(ch, {})
            l_entry = loser_channels.get(ch, {})

            w_granted = bool(w_entry.get("granted", False)) if isinstance(w_entry, dict) else bool(w_entry)
            l_granted = bool(l_entry.get("granted", False)) if isinstance(l_entry, dict) else bool(l_entry)

            granted = w_granted and l_granted

            # Prefer winner's source/metadata when both granted, otherwise note merge
            source = (w_entry.get("source") if isinstance(w_entry, dict) else None) or "merge"

            merged[ch] = {
                "granted": granted,
                "source": source,
                "timestamp": now_iso,
            }

        return merged


# Singleton instance
consent_manager = ConsentManager()
