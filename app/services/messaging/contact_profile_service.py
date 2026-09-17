"""Canonical contact endpoints, locale evidence and verification enqueueing.

All ingestion paths call this service after changing a contact identity.  It
keeps the legacy MessagingUser columns as primary caches while materializing
provider-neutral endpoints for campaigns and verification.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.models import Project
from app.models.campaigns import (
    ContactEndpoint,
    ContactPermissionEvidence,
    ContactVerificationRequest,
)
from app.models.messaging import (
    ContactVerificationState,
    MessagingUser,
    ProjectVerificationSettings,
)
from app.services.messaging.phone_normalizer import PhoneNormalizer, country_for_e164
from app.services.messaging.pii_hasher import pii_hasher


_ACTIVE_FIRST_SEEN_REQUEST_STATUSES = {
    "queued",
    "claiming",
    "processing_claimed",
    "processing",
    "paused_no_credits",
}


class ContactProfileService:
    def __init__(self, db: Session):
        self.db = db

    def sync_user(
        self,
        user: MessagingUser,
        *,
        source: str = "ingestion",
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Optional[ContactEndpoint]]:
        """Upsert primary endpoints and enqueue first-seen verification.

        The caller owns the surrounding transaction.  No provider I/O occurs
        here, so ingestion remains fast and restart-safe.
        """
        if not user.id:
            self.db.flush()
        project = self.db.query(Project).filter(Project.id == user.project_id).first()
        if not project:
            raise ValueError(f"Project {user.project_id} not found")
        salt = project.pii_salt or pii_hasher.get_or_create_project_salt(self.db, project.id)
        result: dict[str, Optional[ContactEndpoint]] = {"email": None, "phone": None}

        if user.email:
            email = pii_hasher.normalize_email(user.email)
            if email:
                email_hash = pii_hasher.hash_email(email, salt)
                endpoint, created, reactivated = self._upsert_endpoint(
                    user=user,
                    endpoint_type="email",
                    value=email,
                    normalized_value=email,
                    value_hash=email_hash,
                    source=source,
                    metadata={
                        "purpose": "marketing",
                        "locale": user.locale,
                        "locale_source": (context or {}).get("locale_source"),
                    },
                )
                user.email = email
                user.email_hash = email_hash
                result["email"] = endpoint
                if created or reactivated:
                    self._enqueue_if_enabled(user, endpoint, "email")

        raw_phone = user.phone_e164 or user.phone
        if raw_phone:
            if user.phone_e164 and 7 <= len(pii_hasher.normalize_phone(user.phone_e164)) <= 15:
                normalized_phone = pii_hasher.normalize_phone(user.phone_e164)
                phone_status = user.phone_norm_status or "assumed_e164"
            else:
                normalized_phone, phone_status = PhoneNormalizer.normalize(
                    str(raw_phone), locale=user.locale, fallback_country_code=None,
                )
            user.phone_e164 = normalized_phone
            user.phone_norm_status = phone_status
            if normalized_phone:
                phone_hash = pii_hasher.hash_phone(normalized_phone, salt)
                country = country_for_e164(normalized_phone)
                endpoint, created, reactivated = self._upsert_endpoint(
                    user=user,
                    endpoint_type="phone",
                    value=normalized_phone,
                    normalized_value=normalized_phone,
                    value_hash=phone_hash,
                    source=source,
                    metadata={
                        "normalization_status": phone_status,
                        "country": country,
                        "country_source": "phone_e164",
                        "country_confidence": (
                            "high" if phone_status == "valid_e164"
                            else "medium" if phone_status in {"inferred", "fallback"}
                            else "low"
                        ),
                    },
                )
                user.phone_hash = phone_hash
                result["phone"] = endpoint
                if created or reactivated:
                    self._enqueue_if_enabled(user, endpoint, "whatsapp")
        return result

    def _upsert_endpoint(
        self,
        *,
        user: MessagingUser,
        endpoint_type: str,
        value: str,
        normalized_value: str,
        value_hash: str,
        source: str,
        metadata: dict[str, Any],
        make_primary: bool = True,
    ) -> tuple[ContactEndpoint, bool, bool]:
        lookup = (
            ContactEndpoint.project_id == user.project_id,
            ContactEndpoint.user_id == user.id,
            ContactEndpoint.endpoint_type == endpoint_type,
            ContactEndpoint.value_hash == value_hash,
        )
        dialect = self.db.get_bind().dialect.name
        if dialect == "postgresql":
            # Serialize primary selection for one contact/type and use the
            # database uniqueness constraint as the first-seen authority.
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"contact-endpoint:{user.project_id}:{user.id}:{endpoint_type}"},
            )
            if make_primary:
                self.db.query(ContactEndpoint).filter(
                    ContactEndpoint.project_id == user.project_id,
                    ContactEndpoint.user_id == user.id,
                    ContactEndpoint.endpoint_type == endpoint_type,
                    ContactEndpoint.is_primary == True,  # noqa: E712
                ).update({"is_primary": False}, synchronize_session=False)
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            table = ContactEndpoint.__table__
            statement = pg_insert(table).values({
                table.c.project_id: user.project_id,
                table.c.user_id: user.id,
                table.c.endpoint_type: endpoint_type,
                table.c.value: value,
                table.c.normalized_value: normalized_value,
                table.c.value_hash: value_hash,
                table.c.is_primary: make_primary,
                table.c.status: "active",
                table.c.source: source,
                table.c["metadata"]: metadata,
            }).on_conflict_do_nothing(
                constraint="uq_contact_endpoint_hash",
            ).returning(table.c.id)
            inserted_id = self.db.execute(statement).scalar_one_or_none()
            created = inserted_id is not None
            endpoint = self.db.query(ContactEndpoint).filter(*lookup).one()
        else:
            endpoint = self.db.query(ContactEndpoint).filter(*lookup).first()
            created = endpoint is None
            if endpoint is None:
                endpoint = ContactEndpoint(
                    project_id=user.project_id,
                    user_id=user.id,
                    endpoint_type=endpoint_type,
                    value=value,
                    normalized_value=normalized_value,
                    value_hash=value_hash,
                    is_primary=make_primary,
                    status="active",
                    source=source,
                    endpoint_metadata=metadata,
                )
                self.db.add(endpoint)
                self.db.flush()

        reactivated = not created and endpoint.status != "active"
        if not created:
            endpoint.value = value
            endpoint.normalized_value = normalized_value
            endpoint.status = "active"
            endpoint.is_primary = make_primary or endpoint.is_primary
            endpoint.last_seen_at = datetime.utcnow()
            endpoint.endpoint_metadata = {**(endpoint.endpoint_metadata or {}), **metadata}

        if make_primary:
            self.db.query(ContactEndpoint).filter(
                ContactEndpoint.project_id == user.project_id,
                ContactEndpoint.user_id == user.id,
                ContactEndpoint.endpoint_type == endpoint_type,
                ContactEndpoint.id != endpoint.id,
                ContactEndpoint.is_primary == True,  # noqa: E712
            ).update({"is_primary": False}, synchronize_session=False)
        # Persist the reactivation before enqueueing or returning.  The
        # PostgreSQL insert used by _enqueue_if_enabled is a Core statement
        # and must not rely on ORM autoflush to make the endpoint active.
        self.db.flush()
        return endpoint, created, reactivated

    def _enqueue_if_enabled(
        self,
        user: MessagingUser,
        endpoint: ContactEndpoint,
        verification_type: str,
    ) -> Optional[ContactVerificationRequest]:
        settings = self.db.query(ProjectVerificationSettings).filter(
            ProjectVerificationSettings.project_id == user.project_id,
        ).first()
        if not settings:
            return None
        field = (
            "email_verify_on_first_seen"
            if verification_type == "email"
            else "whatsapp_verify_on_first_seen"
        )
        enabled = bool(getattr(settings, field, False))
        if not enabled:
            return None

        provider = settings.email_provider if verification_type == "email" else settings.whatsapp_provider
        key = f"first_seen:{verification_type}:{endpoint.id}:{endpoint.value_hash}"
        lookup = (
            ContactVerificationRequest.project_id == user.project_id,
            ContactVerificationRequest.idempotency_key == key,
        )
        if self.db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            table = ContactVerificationRequest.__table__
            statement = pg_insert(table).values({
                table.c.project_id: user.project_id,
                table.c.user_id: user.id,
                table.c.endpoint_id: endpoint.id,
                table.c.verification_type: verification_type,
                table.c.provider: provider,
                table.c.trigger_type: "first_seen_or_change",
                table.c.idempotency_key: key,
                table.c.status: "queued",
                table.c.request_metadata: {
                    "priority": "immediate",
                    "identifier_hash": endpoint.value_hash,
                },
            }).on_conflict_do_nothing(
                constraint="uq_verify_request_idem",
            ).returning(table.c.id)
            self.db.execute(statement).scalar_one_or_none()
            request = self.db.query(ContactVerificationRequest).filter(*lookup).one()
        else:
            request = self.db.query(ContactVerificationRequest).filter(*lookup).first()
            if not request:
                request = ContactVerificationRequest(
                    project_id=user.project_id,
                    user_id=user.id,
                    endpoint_id=endpoint.id,
                    verification_type=verification_type,
                    provider=provider,
                    trigger_type="first_seen_or_change",
                    idempotency_key=key,
                    status="queued",
                    request_metadata={"priority": "immediate", "identifier_hash": endpoint.value_hash},
                )
                self.db.add(request)
                return request

        if request.status in _ACTIVE_FIRST_SEEN_REQUEST_STATUSES:
            return request

        # A replaced endpoint can later be reintroduced with the same hash.
        # Reuse its durable idempotency row, but requeue terminal work instead
        # of creating a second request or returning a cancelled row forever.
        request.user_id = user.id
        request.endpoint_id = endpoint.id
        request.verification_type = verification_type
        request.provider = provider
        request.trigger_type = "first_seen_or_change"
        request.status = "queued"
        request.job_id = None
        request.attempt_count = 0
        request.provider_status = None
        request.canonical_status = None
        request.error_code = None
        request.error_message = None
        request.request_metadata = {
            "priority": "immediate",
            "identifier_hash": endpoint.value_hash,
        }
        request.requested_at = datetime.utcnow()
        request.started_at = None
        request.finished_at = None
        # Persist the terminal-to-queued transition before the caller can
        # return or refresh the durable request row.
        self.db.flush()
        return request

    def rectify_identifiers(
        self,
        user: MessagingUser,
        updates: dict[str, Any],
        *,
        source: str = "dsar_rectification",
    ) -> list[str]:
        """Replace primary identifiers without editing endpoint values in place.

        A rectification deactivates the superseded endpoint, invalidates its
        current verification cache, and materializes a new primary endpoint.
        A genuinely first-seen or reintroduced endpoint is queued through the
        same project automation policy used by normal ingestion.
        """
        project = self.db.query(Project).filter(
            Project.id == user.project_id,
        ).first()
        if not project:
            raise ValueError(f"Project {user.project_id} not found")
        salt = project.pii_salt or pii_hasher.get_or_create_project_salt(
            self.db, project.id,
        )
        updated_fields: list[str] = []

        if "email" in updates:
            raw_email = updates.get("email")
            normalized_email = (
                pii_hasher.normalize_email(str(raw_email))
                if raw_email is not None and str(raw_email).strip()
                else None
            )
            if normalized_email and "@" not in normalized_email:
                raise ValueError("Invalid rectified email")
            new_hash = (
                pii_hasher.hash_email(normalized_email, salt)
                if normalized_email
                else None
            )
            old_hash = user.email_hash
            if new_hash != old_hash:
                replaced = self._deactivate_replaced_endpoints(
                    user, "email", old_hash,
                )
                self._invalidate_replaced_verification(
                    user,
                    verification_type="email",
                    endpoints=replaced,
                    previous_hash=old_hash,
                )
            user.email = normalized_email
            user.email_hash = new_hash
            if normalized_email:
                endpoint, created, reactivated = self._upsert_endpoint(
                    user=user,
                    endpoint_type="email",
                    value=normalized_email,
                    normalized_value=normalized_email,
                    value_hash=new_hash,
                    source=source,
                    metadata={
                        "purpose": "marketing",
                        "locale": user.locale,
                        "rectified": True,
                    },
                )
                if created or reactivated:
                    self._enqueue_if_enabled(user, endpoint, "email")
            updated_fields.append("email")

        if "phone" in updates:
            raw_phone_value = updates.get("phone")
            raw_phone = (
                str(raw_phone_value).strip()
                if raw_phone_value is not None
                else ""
            )
            if raw_phone:
                normalized_phone, phone_status = PhoneNormalizer.normalize(
                    raw_phone,
                    locale=user.locale,
                    fallback_country_code=None,
                )
                if not normalized_phone:
                    raise ValueError("Invalid rectified phone")
            else:
                normalized_phone, phone_status = None, "missing"
            new_hash = (
                pii_hasher.hash_phone(normalized_phone, salt)
                if normalized_phone
                else None
            )
            old_hash = user.phone_hash
            if new_hash != old_hash:
                replaced = self._deactivate_replaced_endpoints(
                    user, "phone", old_hash,
                )
                self._invalidate_replaced_verification(
                    user,
                    verification_type="whatsapp",
                    endpoints=replaced,
                    previous_hash=old_hash,
                )
                user.whatsapp_status = "unverified"
                user.whatsapp_checked_at = None
            user.phone = raw_phone or None
            user.phone_e164 = normalized_phone
            user.phone_norm_status = phone_status
            user.phone_hash = new_hash
            if normalized_phone:
                endpoint, created, reactivated = self._upsert_endpoint(
                    user=user,
                    endpoint_type="phone",
                    value=normalized_phone,
                    normalized_value=normalized_phone,
                    value_hash=new_hash,
                    source=source,
                    metadata={
                        "normalization_status": phone_status,
                        "country": country_for_e164(normalized_phone),
                        "country_source": "phone_e164",
                        "rectified": True,
                    },
                )
                if created or reactivated:
                    self._enqueue_if_enabled(user, endpoint, "whatsapp")
            updated_fields.append("phone")

        return updated_fields

    def _deactivate_replaced_endpoints(
        self,
        user: MessagingUser,
        endpoint_type: str,
        previous_hash: Optional[str],
    ) -> list[ContactEndpoint]:
        selector = ContactEndpoint.is_primary == True  # noqa: E712
        if previous_hash:
            selector = or_(
                ContactEndpoint.value_hash == previous_hash,
                ContactEndpoint.is_primary == True,  # noqa: E712
            )
        endpoints = self.db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == user.project_id,
            ContactEndpoint.user_id == user.id,
            ContactEndpoint.endpoint_type == endpoint_type,
            ContactEndpoint.status == "active",
            selector,
        ).all()
        for endpoint in endpoints:
            endpoint.status = "inactive"
            endpoint.is_primary = False
        return endpoints

    def _invalidate_replaced_verification(
        self,
        user: MessagingUser,
        *,
        verification_type: str,
        endpoints: list[ContactEndpoint],
        previous_hash: Optional[str],
    ) -> None:
        endpoint_ids = [endpoint.id for endpoint in endpoints]
        state_filters = []
        if endpoint_ids:
            state_filters.append(ContactVerificationState.endpoint_id.in_(endpoint_ids))
        if previous_hash:
            state_filters.append(ContactVerificationState.identifier_hash == previous_hash)
        if state_filters:
            self.db.query(ContactVerificationState).filter(
                ContactVerificationState.project_id == user.project_id,
                ContactVerificationState.user_id == user.id,
                ContactVerificationState.verification_type == verification_type,
                or_(*state_filters),
            ).delete(synchronize_session=False)

        if endpoint_ids:
            now = datetime.utcnow()
            self.db.query(ContactVerificationRequest).filter(
                ContactVerificationRequest.project_id == user.project_id,
                ContactVerificationRequest.user_id == user.id,
                ContactVerificationRequest.endpoint_id.in_(endpoint_ids),
                ContactVerificationRequest.status.in_([
                    "queued",
                    "claiming",
                    "processing_claimed",
                    "processing",
                    "paused_no_credits",
                ]),
            ).update({
                ContactVerificationRequest.status: "cancelled",
                ContactVerificationRequest.error_code: "identifier_rectified",
                ContactVerificationRequest.error_message: (
                    "Verification invalidated because the contact identifier was rectified"
                ),
                ContactVerificationRequest.finished_at: now,
            }, synchronize_session=False)

    def withdraw_permission_evidence(
        self,
        user: MessagingUser,
        *,
        dsar_request_id: int,
        source: str = "dsar",
    ) -> list[ContactPermissionEvidence]:
        """Append endpoint-scoped withdrawals and retire every prior grant.

        Expiring prior grants prevents a queued send (or a later legacy
        ``is_subscribed`` toggle) from reusing evidence captured before the
        withdrawal. A future opt-in must append fresh granted evidence.
        """
        now = datetime.utcnow()
        rows = self.db.query(ContactPermissionEvidence).filter(
            ContactPermissionEvidence.project_id == user.project_id,
            ContactPermissionEvidence.user_id == user.id,
        ).order_by(
            ContactPermissionEvidence.captured_at,
            ContactPermissionEvidence.id,
        ).all()
        grouped: dict[tuple[str, Optional[int], str], list[ContactPermissionEvidence]] = {}
        for row in rows:
            key = (row.channel, row.endpoint_id, row.permission_type)
            grouped.setdefault(key, []).append(row)
            if row.status == "granted" and (
                row.expires_at is None or row.expires_at > now
            ):
                row.expires_at = now

        # Even contacts ingested before permission evidence was introduced get
        # explicit endpoint-level withdrawal markers.
        endpoints = self.db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == user.project_id,
            ContactEndpoint.user_id == user.id,
            ContactEndpoint.status == "active",
        ).all()
        for endpoint in endpoints:
            channel = (
                "email" if endpoint.endpoint_type == "email"
                else "whatsapp" if endpoint.endpoint_type == "phone"
                else endpoint.endpoint_type
            )
            grouped.setdefault((channel, endpoint.id, "marketing"), [])

        withdrawals: list[ContactPermissionEvidence] = []
        for (channel, endpoint_id, permission_type), evidence_rows in grouped.items():
            evidence_ref = (
                f"dsar:{dsar_request_id}:withdraw:{channel}:"
                f"{endpoint_id if endpoint_id is not None else 'global'}:{permission_type}"
            )
            existing = next(
                (row for row in evidence_rows if row.evidence_ref == evidence_ref),
                None,
            )
            if existing:
                withdrawals.append(existing)
                continue
            latest = evidence_rows[-1] if evidence_rows else None
            withdrawal = ContactPermissionEvidence(
                project_id=user.project_id,
                user_id=user.id,
                endpoint_id=endpoint_id,
                channel=channel,
                permission_type=permission_type,
                status="withdrawn",
                source=source,
                policy_version=(latest.policy_version if latest else user.consent_version),
                evidence_ref=evidence_ref,
                captured_at=now,
                evidence_metadata={
                    "dsar_request_id": dsar_request_id,
                    "revoked_evidence_ids": [row.id for row in evidence_rows if row.id],
                },
            )
            self.db.add(withdrawal)
            withdrawals.append(withdrawal)

        known_channels = {channel for channel, _endpoint_id, _permission in grouped}
        opted_out = set(user.opted_out_channels or [])
        opted_out.update(known_channels)
        user.opted_out_channels = sorted(opted_out)
        channels = dict(user.consent_channels or {})
        for channel in known_channels:
            channels[channel] = {
                "granted": False,
                "status": "withdrawn",
                "source": source,
                "timestamp": now.isoformat(),
            }
        user.consent_channels = channels
        return withdrawals

    def list_endpoints(self, project_id: int, user_id: int) -> list[ContactEndpoint]:
        return self.db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == user_id,
        ).order_by(
            ContactEndpoint.endpoint_type,
            ContactEndpoint.is_primary.desc(),
            ContactEndpoint.last_seen_at.desc(),
        ).all()

    def set_primary(self, project_id: int, user_id: int, endpoint_id: int) -> ContactEndpoint:
        if self.db.get_bind().dialect.name == "postgresql":
            endpoint_type = self.db.query(ContactEndpoint.endpoint_type).filter(
                ContactEndpoint.id == endpoint_id,
                ContactEndpoint.project_id == project_id,
                ContactEndpoint.user_id == user_id,
            ).scalar()
            if not endpoint_type:
                raise ValueError("Contact endpoint not found")
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"contact-endpoint:{project_id}:{user_id}:{endpoint_type}"},
            )
        endpoint = self.db.query(ContactEndpoint).filter(
            ContactEndpoint.id == endpoint_id,
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == user_id,
            ContactEndpoint.status == "active",
        ).first()
        if not endpoint:
            raise ValueError("Contact endpoint not found")
        self.db.query(ContactEndpoint).filter(
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == user_id,
            ContactEndpoint.endpoint_type == endpoint.endpoint_type,
        ).update({"is_primary": False}, synchronize_session=False)
        endpoint.is_primary = True
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()
        if endpoint.endpoint_type == "email":
            user.email = endpoint.normalized_value or endpoint.value
            user.email_hash = endpoint.value_hash
        elif endpoint.endpoint_type == "phone":
            user.phone_e164 = endpoint.normalized_value or endpoint.value
            user.phone_hash = endpoint.value_hash
        self.db.commit()
        self.db.refresh(endpoint)
        return endpoint
