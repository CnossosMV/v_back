"""
Extension Auth Service — manages scoped tokens for the Chrome Extension.
"""
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import and_
from app.models import ExtensionToken, ExtensionAuditLog


class ExtensionAuthService:

    def __init__(self, db: Session):
        self.db = db

    def create_token(
        self, user_id: int, project_id: int, role: str = "editor"
    ) -> Tuple[str, ExtensionToken]:
        """Generate ext_live_{32chars}, store SHA-256 hash, 8h expiry."""
        raw = f"ext_live_{secrets.token_hex(16)}"
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[:12] + "..."

        token = ExtensionToken(
            project_id=project_id,
            user_id=user_id,
            token_hash=token_hash,
            token_prefix=prefix,
            role=role,
            is_active=True,
            expires_at=datetime.utcnow() + timedelta(hours=8),
        )
        self.db.add(token)

        # Audit
        self._audit(project_id, user_id, "extension.auth", "token", prefix, None)

        self.db.commit()
        self.db.refresh(token)
        return raw, token

    def verify_token(self, raw_token: str) -> Optional[Tuple[int, int, str]]:
        """Verify token → (project_id, user_id, role) or None."""
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        token = self.db.query(ExtensionToken).filter(
            and_(
                ExtensionToken.token_hash == token_hash,
                ExtensionToken.is_active == True,
                ExtensionToken.expires_at > datetime.utcnow(),
            )
        ).first()
        if not token:
            return None
        return (token.project_id, token.user_id, token.role)

    def revoke_token(self, token_id: int, user_id: int) -> bool:
        """Revoke a token by ID."""
        token = self.db.query(ExtensionToken).filter(
            ExtensionToken.id == token_id,
            ExtensionToken.user_id == user_id,
        ).first()
        if not token:
            return False
        token.is_active = False
        token.revoked_at = datetime.utcnow()

        self._audit(
            token.project_id, user_id,
            "extension.auth.revoke", "token", str(token_id), None,
        )
        self.db.commit()
        return True

    def list_active_tokens(self, user_id: int) -> list:
        """List active, non-expired tokens for a user."""
        return self.db.query(ExtensionToken).filter(
            ExtensionToken.user_id == user_id,
            ExtensionToken.is_active == True,
            ExtensionToken.expires_at > datetime.utcnow(),
        ).order_by(ExtensionToken.created_at.desc()).all()

    def cleanup_expired(self) -> int:
        """Delete tokens expired > 24h ago."""
        cutoff = datetime.utcnow() - timedelta(hours=24)
        count = self.db.query(ExtensionToken).filter(
            ExtensionToken.expires_at < cutoff,
        ).delete()
        self.db.commit()
        return count

    def _audit(
        self, project_id: int, user_id: int,
        action: str, target_type: str, target_id: str,
        ip_address: Optional[str], details: dict = None,
    ):
        log = ExtensionAuditLog(
            project_id=project_id,
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
            ip_address=ip_address,
        )
        self.db.add(log)
