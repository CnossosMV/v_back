"""
Identity Resolver Service
Enhanced identity resolution with contact identities table, session isolation,
Tier 3 merge suggestions, account association, and inbound contact creation.
"""
import re
from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models.messaging import (
    MessagingUser, MessagingEvent, MessagingAnonymousProfile,
    ContactIdentity, Account, ContactAccountAssociation, MergeSuggestion
)
from app.models import WhatsAppInstance
from app.services.messaging.pii_hasher import pii_hasher
from app.services.messaging.phone_normalizer import PhoneNormalizer
from app.services.messaging.attribution_resolver import transfer_anonymous_touches


class IdentityResolver:
    """
    Resolves user identities with enhanced identity tracking:
    1. Creates/finds users by external_id, email_hash, or phone_hash
    2. Records all identities in contact_identities table
    3. Merges anonymous profiles with session isolation
    4. Creates merge suggestions for phone-only matches (Tier 3)
    5. Supports account association
    6. Supports inbound contact resolution
    """

    def resolve_identity(
        self,
        db: Session,
        project_id: int,
        external_id: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        anonymous_id: Optional[str] = None,
        name: Optional[str] = None,
        properties: Optional[dict] = None,
        account_id: Optional[str] = None,
        created_via: str = 'api',
        device_fingerprint: Optional[str] = None
    ) -> Optional[MessagingUser]:
        """
        Resolve identity and return the MessagingUser.

        Resolution order:
        1. If external_id provided, find/create user by external_id
        2. If email provided, try to find user by email_hash
        3. If phone provided, try to find user by phone_hash (creates suggestion, not auto-merge)
        4. If only anonymous_id, track anonymous profile (return None)
        """
        # Get PII hashes
        email_hash, phone_hash = None, None
        if email or phone:
            email_hash, phone_hash = pii_hasher.hash_user_pii(
                db, project_id, email, phone
            )

        user = None
        is_new = False

        # Priority 1: external_id lookup
        if external_id:
            user = db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.external_id == external_id,
                MessagingUser.status != 'deleted'
            ).first()

            if user:
                # If merged, resolve tombstone
                if user.status == 'merged' and user.merged_into:
                    from app.services.messaging.contact_merge_service import ContactMergeService
                    merge_svc = ContactMergeService(db)
                    resolved_id = merge_svc.resolve_tombstone(project_id, user.id)
                    if resolved_id != user.id:
                        user = db.query(MessagingUser).get(resolved_id)

                self._update_user(db, user, email, phone, email_hash, phone_hash, name, properties)
            else:
                user = self._create_user(
                    db, project_id, external_id, email, phone,
                    email_hash, phone_hash, name, properties, created_via
                )
                is_new = True

            # Record identities
            self._record_identity(db, project_id, user.id, 'contact_id', external_id, 'identify')
            if email:
                self._record_identity(db, project_id, user.id, 'email', email, 'identify')
            if phone:
                self._record_identity(db, project_id, user.id, 'phone', phone, 'identify')

            # Session isolation: handle anonymous_id
            if anonymous_id:
                self._merge_anonymous_with_isolation(db, project_id, anonymous_id, user.id)
                self._record_identity(db, project_id, user.id, 'anonymous_id', anonymous_id, 'identify')

            # Account association
            if account_id:
                self._associate_account(db, project_id, user.id, account_id)

            return user

        # Priority 2: email_hash lookup
        if email_hash:
            user = db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.email_hash == email_hash,
                MessagingUser.status == 'active'
            ).first()

            if user:
                self._update_user(db, user, email, phone, email_hash, phone_hash, name, properties)
                if anonymous_id:
                    self._merge_anonymous_with_isolation(db, project_id, anonymous_id, user.id)
                    self._record_identity(db, project_id, user.id, 'anonymous_id', anonymous_id, 'identify')
                if account_id:
                    self._associate_account(db, project_id, user.id, account_id)
                return user

        # Priority 3: phone_hash lookup — creates suggestion instead of auto-merging
        if phone_hash:
            matched_user = db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.phone_hash == phone_hash,
                MessagingUser.status == 'active'
            ).first()

            if matched_user:
                # If the matched user has no external_id, it's a safe merge (inbound stub)
                if not matched_user.external_id or matched_user.external_id == matched_user.phone:
                    self._update_user(db, matched_user, email, phone, email_hash, phone_hash, name, properties)
                    if anonymous_id:
                        self._merge_anonymous_with_isolation(db, project_id, anonymous_id, matched_user.id)
                    if account_id:
                        self._associate_account(db, project_id, matched_user.id, account_id)
                    return matched_user
                else:
                    # Phone-only match with existing external_id contact — create suggestion
                    # We still need a user for the current identify call — but since
                    # no external_id was provided, we can't create one. Return None.
                    if anonymous_id:
                        self._track_anonymous_profile(db, project_id, anonymous_id, properties, device_fingerprint)
                    return None

        # No match found — track anonymous profile if provided
        if anonymous_id:
            self._track_anonymous_profile(db, project_id, anonymous_id, properties, device_fingerprint)

        return None

    @staticmethod
    def _phone_variants(phone: str, instance_cc: str = None) -> List[str]:
        """Generate phone number variants for fuzzy matching.
        Given "5584991321815" with cc="55", returns:
          ["5584991321815", "84991321815"]
        Given "84991321815" with cc="55", returns:
          ["84991321815", "5584991321815"]
        """
        digits = re.sub(r'\D', '', phone)
        variants = [digits]
        if instance_cc:
            if digits.startswith(instance_cc):
                variants.append(digits[len(instance_cc):])
            else:
                variants.append(instance_cc + digits)
        return list(dict.fromkeys(variants))

    @staticmethod
    def _get_instance_cc(db: Session, channel_instance_id: int) -> Optional[str]:
        """Get default_country_code from a WhatsApp instance."""
        if not channel_instance_id:
            return None
        inst = db.query(WhatsAppInstance).filter(
            WhatsAppInstance.id == channel_instance_id
        ).first()
        return inst.default_country_code if inst else None

    def resolve_inbound_contact(
        self,
        db: Session,
        project_id: int,
        channel: str,
        sender_id: str,
        channel_instance_id: int = None,
        push_name: str = None,
    ) -> Optional[MessagingUser]:
        """
        Resolve a contact from an inbound message.
        Searches contact_identities for (channel, sender_id), then by phone.
        Creates a new MessagingUser if not found.
        """
        # Search contact_identities for channel identity
        identity = db.query(ContactIdentity).filter(
            ContactIdentity.project_id == project_id,
            ContactIdentity.identity_type == 'channel',
            ContactIdentity.identity_value == f"{channel}:{sender_id}"
        ).first()

        if identity:
            user = db.query(MessagingUser).filter(
                MessagingUser.id == identity.user_id,
                MessagingUser.status != 'deleted'
            ).first()
            if user:
                # Resolve tombstone if merged
                if user.status == 'merged' and user.merged_into:
                    from app.services.messaging.contact_merge_service import ContactMergeService
                    merge_svc = ContactMergeService(db)
                    resolved_id = merge_svc.resolve_tombstone(project_id, user.id)
                    user = db.query(MessagingUser).get(resolved_id)
                if push_name and not user.name:
                    user.name = push_name
                user.last_seen_at = datetime.utcnow()
                db.commit()
                return user

        # Search by phone identity (strip + for matching)
        clean_sender = sender_id.lstrip('+')
        phone_identity = db.query(ContactIdentity).filter(
            ContactIdentity.project_id == project_id,
            ContactIdentity.identity_type == 'phone',
            ContactIdentity.identity_value == clean_sender
        ).first()
        if phone_identity:
            user = db.query(MessagingUser).filter(
                MessagingUser.id == phone_identity.user_id,
                MessagingUser.status == 'active'
            ).first()
            if user:
                # Record channel identity for faster future lookups
                self._record_identity(
                    db, project_id, user.id, 'channel',
                    f"{channel}:{sender_id}", 'inbound_message',
                    channel_instance_id=channel_instance_id
                )
                if push_name and not user.name:
                    user.name = push_name
                user.last_seen_at = datetime.utcnow()
                db.commit()
                return user

        # Also try direct phone lookup on MessagingUser
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.phone_e164 == clean_sender,
            MessagingUser.status == 'active'
        ).first()
        if user:
            self._record_identity(
                db, project_id, user.id, 'channel',
                f"{channel}:{sender_id}", 'inbound_message',
                channel_instance_id=channel_instance_id
            )
            if push_name and not user.name:
                user.name = push_name
            user.last_seen_at = datetime.utcnow()
            db.commit()
            return user

        # Multi-variant phone lookup: try with/without country code
        instance_cc = self._get_instance_cc(db, channel_instance_id)
        if instance_cc:
            variants = self._phone_variants(clean_sender, instance_cc)
            for variant in variants:
                if variant == clean_sender:
                    continue  # Already tried exact match above
                # Try phone identity
                phone_id = db.query(ContactIdentity).filter(
                    ContactIdentity.project_id == project_id,
                    ContactIdentity.identity_type == 'phone',
                    ContactIdentity.identity_value == variant
                ).first()
                if phone_id:
                    user = db.query(MessagingUser).filter(
                        MessagingUser.id == phone_id.user_id,
                        MessagingUser.status == 'active'
                    ).first()
                if not user:
                    # Try phone_e164 direct
                    user = db.query(MessagingUser).filter(
                        MessagingUser.project_id == project_id,
                        MessagingUser.phone_e164 == variant,
                        MessagingUser.status == 'active'
                    ).first()
                if user:
                    # Match found via variant — record identities and update phone_e164
                    self._record_identity(
                        db, project_id, user.id, 'channel',
                        f"{channel}:{sender_id}", 'inbound_message',
                        channel_instance_id=channel_instance_id
                    )
                    self._record_identity(
                        db, project_id, user.id, 'phone',
                        clean_sender, 'inbound_message'
                    )
                    # Update phone_e164 to the full E.164 version (with CC)
                    full_e164 = clean_sender if clean_sender.startswith(instance_cc) else instance_cc + clean_sender
                    if user.phone_e164 != full_e164:
                        user.phone_e164 = full_e164
                        user.phone_norm_status = 'inferred'
                    if push_name and not user.name:
                        user.name = push_name
                    user.last_seen_at = datetime.utcnow()
                    db.commit()
                    return user

        # Email identity lookup: search by email identity or direct email field
        if channel == "email" and "@" in sender_id:
            email_identity = db.query(ContactIdentity).filter(
                ContactIdentity.project_id == project_id,
                ContactIdentity.identity_type == 'email',
                ContactIdentity.identity_value == sender_id.lower()
            ).first()
            if email_identity:
                user = db.query(MessagingUser).filter(
                    MessagingUser.id == email_identity.user_id,
                    MessagingUser.status == 'active'
                ).first()
                if user:
                    self._record_identity(
                        db, project_id, user.id, 'channel',
                        f"{channel}:{sender_id}", 'inbound_message',
                        channel_instance_id=channel_instance_id
                    )
                    if push_name and not user.name:
                        user.name = push_name
                    user.last_seen_at = datetime.utcnow()
                    db.commit()
                    return user

            # Direct email field lookup
            user = db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.email == sender_id.lower(),
                MessagingUser.status == 'active'
            ).first()
            if user:
                self._record_identity(
                    db, project_id, user.id, 'channel',
                    f"{channel}:{sender_id}", 'inbound_message',
                    channel_instance_id=channel_instance_id
                )
                self._record_identity(db, project_id, user.id, 'email', sender_id.lower(), 'inbound_message')
                if push_name and not user.name:
                    user.name = push_name
                user.last_seen_at = datetime.utcnow()
                db.commit()
                return user

        # Not found — create new MessagingUser
        is_email = channel == "email" and "@" in sender_id

        # Normalize phone for new inbound contacts
        phone_e164 = clean_sender
        phone_norm_status = None
        if not is_email and clean_sender:
            if not instance_cc:
                instance_cc = self._get_instance_cc(db, channel_instance_id)
            normalized, status = PhoneNormalizer.normalize(
                clean_sender, locale=None, fallback_country_code=instance_cc,
            )
            if normalized:
                phone_e164 = normalized
                phone_norm_status = status

        user = MessagingUser(
            project_id=project_id,
            external_id=sender_id,
            phone=None if is_email else sender_id,
            phone_e164=None if is_email else phone_e164,
            phone_norm_status=phone_norm_status,
            email=sender_id.lower() if is_email else None,
            name=push_name or None,
            created_via='inbound_message',
            primary_channel={"channel_type": channel, "channel_instance_id": channel_instance_id},
            status='active'
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        # Record identities
        self._record_identity(
            db, project_id, user.id, 'channel',
            f"{channel}:{sender_id}", 'inbound_message',
            channel_instance_id=channel_instance_id
        )
        if is_email:
            self._record_identity(db, project_id, user.id, 'email', sender_id.lower(), 'inbound_message')
        else:
            self._record_identity(db, project_id, user.id, 'phone', clean_sender, 'inbound_message')
            # Also record normalized variant if different
            if phone_e164 and phone_e164 != clean_sender:
                self._record_identity(db, project_id, user.id, 'phone', phone_e164, 'inbound_message')
        self._record_identity(db, project_id, user.id, 'contact_id', sender_id, 'inbound_message')

        # i18n: resolve locale/timezone from the inbound number's country (flag-gated)
        from app.services.messaging.locale_resolver import assign_locale
        assign_locale(db, user, country_code=instance_cc, phone_e164=phone_e164)
        from app.services.messaging.contact_profile_service import ContactProfileService
        ContactProfileService(db).sync_user(user, source="inbound_message")
        db.commit()

        return user

    def _create_user(
        self, db, project_id, external_id, email, phone,
        email_hash, phone_hash, name, properties, created_via='api'
    ) -> MessagingUser:
        user = MessagingUser(
            project_id=project_id,
            external_id=external_id,
            email=email,
            phone=phone,
            email_hash=email_hash,
            phone_hash=phone_hash,
            name=name,
            properties=properties,
            created_via=created_via,
            status='active'
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        # i18n: resolve locale/timezone from explicit traits in properties (flag-gated)
        from app.services.messaging.locale_resolver import assign_locale
        assign_locale(db, user)
        from app.services.messaging.contact_profile_service import ContactProfileService
        ContactProfileService(db).sync_user(user, source=created_via)
        db.commit()
        return user

    def _update_user(self, db, user, email, phone, email_hash, phone_hash, name, properties):
        if email and not user.email:
            user.email = email
            user.email_hash = email_hash
        if phone and not user.phone:
            user.phone = phone
            user.phone_hash = phone_hash
        if name and not user.name:
            user.name = name
        if properties:
            # dict() copy: in-place mutation of a plain JSON column is not
            # detected by SQLAlchemy, so reassigning the same object is a no-op
            existing = dict(user.properties or {})
            existing.update(properties)
            user.properties = existing
        user.last_seen_at = datetime.utcnow()
        db.commit()
        db.refresh(user)
        # i18n: re-resolve so an explicit locale/timezone trait takes effect (flag-gated)
        from app.services.messaging.locale_resolver import assign_locale
        assign_locale(db, user)
        from app.services.messaging.contact_profile_service import ContactProfileService
        ContactProfileService(db).sync_user(
            user, source=getattr(user, "created_via", None) or "api",
        )
        db.commit()

    def _track_anonymous_profile(self, db, project_id, anonymous_id, properties=None, device_fingerprint=None):
        profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == anonymous_id
        ).first()
        now = datetime.utcnow()
        if profile:
            profile.last_seen_at = now
            if properties:
                existing = dict(profile.properties or {})
                existing.update(properties)
                profile.properties = existing
            if device_fingerprint:
                profile.device_fingerprint = device_fingerprint
        else:
            profile = MessagingAnonymousProfile(
                project_id=project_id,
                anonymous_id=anonymous_id,
                properties=properties,
                device_fingerprint=device_fingerprint
            )
            db.add(profile)
        db.commit()
        return profile

    def _merge_anonymous_with_isolation(self, db, project_id, anonymous_id, user_id):
        """
        Session isolation: If anonymous_id is already merged to a different user,
        sever the old link and create a fresh link for the current user.
        Events under the old anonymous_id stay with the previous contact.
        """
        profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == anonymous_id
        ).first()

        if profile:
            if profile.merged_to_user_id and profile.merged_to_user_id != user_id:
                # Session isolation: sever old link, don't re-assign events
                profile.merged_to_user_id = user_id
                profile.merged_at = datetime.utcnow()
            elif not profile.merged_to_user_id:
                profile.merged_to_user_id = user_id
                profile.merged_at = datetime.utcnow()
                # Merge properties
                if profile.properties:
                    user = db.query(MessagingUser).filter(MessagingUser.id == user_id).first()
                    if user:
                        existing = dict(user.properties or {})
                        for key, value in profile.properties.items():
                            if key not in existing:
                                existing[key] = value
                        user.properties = existing
                # Reassign unlinked events
                self.reassign_anonymous_events(db, project_id, anonymous_id, user_id)
                transfer_anonymous_touches(
                    db, project_id=project_id, anonymous_id=anonymous_id, user_id=user_id,
                )
            profile.last_seen_at = datetime.utcnow()
        else:
            profile = MessagingAnonymousProfile(
                project_id=project_id,
                anonymous_id=anonymous_id,
                merged_to_user_id=user_id,
                merged_at=datetime.utcnow()
            )
            db.add(profile)
            transfer_anonymous_touches(
                db, project_id=project_id, anonymous_id=anonymous_id, user_id=user_id,
            )
        db.commit()

    def merge_anonymous_to_user(self, db, project_id, anonymous_id, user_id):
        """Legacy wrapper for backward compatibility."""
        self._merge_anonymous_with_isolation(db, project_id, anonymous_id, user_id)
        return True

    def reassign_anonymous_events(self, db, project_id, anonymous_id, user_id):
        result = db.query(MessagingEvent).filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.anonymous_id == anonymous_id,
            MessagingEvent.user_id == None
        ).update({"user_id": user_id}, synchronize_session=False)
        db.commit()
        return result

    def alias(self, db, project_id, previous_id, user_id):
        return self.merge_anonymous_to_user(db, project_id, previous_id, user_id)

    def _record_identity(self, db, project_id, user_id, identity_type, identity_value,
                          source, channel_instance_id=None):
        """Upsert identity into contact_identities table."""
        existing = db.query(ContactIdentity).filter(
            ContactIdentity.project_id == project_id,
            ContactIdentity.identity_type == identity_type,
            ContactIdentity.identity_value == identity_value
        ).first()
        if existing:
            if existing.user_id != user_id:
                existing.user_id = user_id
                db.commit()
            return existing
        identity = ContactIdentity(
            project_id=project_id,
            user_id=user_id,
            identity_type=identity_type,
            identity_value=identity_value,
            source=source,
            channel_instance_id=channel_instance_id
        )
        db.add(identity)
        try:
            db.commit()
        except Exception:
            db.rollback()
            # Unique constraint violation — already exists
            pass
        return identity

    def _associate_account(self, db, project_id, user_id, account_external_id):
        """Find or create Account, then create association."""
        account = db.query(Account).filter(
            Account.project_id == project_id,
            Account.external_id == account_external_id
        ).first()
        if not account:
            account = Account(
                project_id=project_id,
                external_id=account_external_id
            )
            db.add(account)
            db.commit()
            db.refresh(account)

        existing_assoc = db.query(ContactAccountAssociation).filter(
            ContactAccountAssociation.project_id == project_id,
            ContactAccountAssociation.user_id == user_id,
            ContactAccountAssociation.account_id == account.id
        ).first()
        if not existing_assoc:
            assoc = ContactAccountAssociation(
                project_id=project_id,
                user_id=user_id,
                account_id=account.id
            )
            db.add(assoc)
            db.commit()


# Singleton instance
identity_resolver = IdentityResolver()
