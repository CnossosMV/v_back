"""
PII Hasher Service
Handles GDPR-compliant hashing of PII (email, phone) using project-specific salts.
"""
import hashlib
import secrets
import re
from typing import Optional
from sqlalchemy.orm import Session


class PIIHasher:
    """
    Handles PII hashing with project-specific salts for GDPR compliance.

    Each project has a unique salt, ensuring that:
    1. The same email/phone will have different hashes across projects
    2. PII cannot be reversed without the salt
    3. Identity resolution can still work within a project
    """

    @staticmethod
    def generate_salt() -> str:
        """Generate a new 64-character hex salt."""
        return secrets.token_hex(32)

    @staticmethod
    def normalize_email(email: str) -> str:
        """
        Normalize email for consistent hashing.
        - Lowercase
        - Strip whitespace
        - Handle Gmail plus addressing (optional)
        """
        if not email:
            return ""
        normalized = email.lower().strip()
        return normalized

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """
        Normalize phone number for consistent hashing.
        - Remove all non-digit characters
        - Keep country code if present
        """
        if not phone:
            return ""
        # Remove all non-digit characters except leading +
        digits = re.sub(r'[^\d+]', '', phone)
        # Remove leading + and normalize
        digits = digits.lstrip('+')
        return digits

    def hash_email(self, email: str, salt: str) -> Optional[str]:
        """
        Hash email with project-specific salt.
        Returns SHA-256 hash as 64-character hex string.
        """
        if not email or not salt:
            return None

        normalized = self.normalize_email(email)
        if not normalized:
            return None

        # Create salted hash: SHA256(salt + normalized_email)
        hash_input = f"{salt}{normalized}".encode('utf-8')
        return hashlib.sha256(hash_input).hexdigest()

    def hash_phone(self, phone: str, salt: str) -> Optional[str]:
        """
        Hash phone number with project-specific salt.
        Returns SHA-256 hash as 64-character hex string.
        """
        if not phone or not salt:
            return None

        normalized = self.normalize_phone(phone)
        if not normalized:
            return None

        # Create salted hash: SHA256(salt + normalized_phone)
        hash_input = f"{salt}{normalized}".encode('utf-8')
        return hashlib.sha256(hash_input).hexdigest()

    def get_or_create_project_salt(self, db: Session, project_id: int) -> str:
        """
        Get or create a PII salt for a project.

        This is called when a project first needs to hash PII.
        The salt is stored in the projects table.
        """
        from app.models import Project

        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project {project_id} not found")

        if not project.pii_salt:
            project.pii_salt = self.generate_salt()
            db.commit()
            db.refresh(project)

        return project.pii_salt

    def hash_user_pii(
        self,
        db: Session,
        project_id: int,
        email: Optional[str] = None,
        phone: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Hash email and phone for a user in a specific project.
        Returns (email_hash, phone_hash) tuple.
        """
        salt = self.get_or_create_project_salt(db, project_id)

        email_hash = self.hash_email(email, salt) if email else None
        phone_hash = self.hash_phone(phone, salt) if phone else None

        return (email_hash, phone_hash)


# Singleton instance
pii_hasher = PIIHasher()
