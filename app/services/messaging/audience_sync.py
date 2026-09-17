"""
Audience sync service.

Builds provider-ready audience batches from Versya users without storing raw
PII in sync logs. Provider upload identifiers are normalized, unsalted SHA-256
hashes generated at sync time.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models.messaging import (
    MessagingAudience,
    MessagingAudienceDestination,
    MessagingAudienceMembership,
    MessagingAudienceSyncItem,
    MessagingAudienceSyncJob,
    MessagingDestination,
    MessagingEvent,
    MessagingUser,
)
from app.services.messaging.consent_manager import consent_manager
from app.services.messaging.destination_config import destination_config

logger = logging.getLogger(__name__)


SUPPORTED_PROVIDERS = {"meta_pixel", "google_ads", "tiktok"}


def sha256_normalized(value: Optional[str], *, lowercase: bool = True) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip()
    if lowercase:
        normalized = normalized.lower()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def digits_only(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or None


def provider_identifiers_for_user(user: MessagingUser) -> Dict[str, Optional[str]]:
    phone = user.phone_e164 or user.phone
    return {
        "email_sha256": sha256_normalized(user.email),
        "phone_sha256": sha256_normalized(digits_only(phone), lowercase=False),
        "external_id_sha256": sha256_normalized(user.external_id),
    }


def identifiers_present(user: MessagingUser) -> Dict[str, bool]:
    identifiers = provider_identifiers_for_user(user)
    return {
        "email": bool(identifiers.get("email_sha256")),
        "phone": bool(identifiers.get("phone_sha256")),
        "external_id": bool(identifiers.get("external_id_sha256")),
    }


@dataclass
class EvaluationResult:
    matched: bool
    state: str
    reason: str
    identifiers_present: Dict[str, bool]
    details: Dict[str, Any]


@dataclass
class ProviderSyncResult:
    status: str
    response_summary: Dict[str, Any]
    failed_count: int = 0
    error_message: Optional[str] = None


class AudienceSyncService:
    STATIC_SOURCE_TYPES = {"csv_import", "project_copy"}

    def _is_static_audience(self, audience: MessagingAudience) -> bool:
        return (audience.source_type or "dynamic_rule") in self.STATIC_SOURCE_TYPES

    def _filters(self, rule_config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not rule_config:
            return []
        filters = rule_config.get("filters")
        if isinstance(filters, list):
            return [f for f in filters if isinstance(f, dict)]
        return []

    def _match_mode(self, rule_config: Optional[Dict[str, Any]]) -> str:
        mode = (rule_config or {}).get("match", "all")
        return "any" if mode == "any" else "all"

    def _value_list(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    def _property_value(self, user: MessagingUser, field: str) -> Any:
        props = user.properties or {}
        key = field.split(".", 1)[1] if field.startswith("property.") else field
        current: Any = props
        for part in key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _event_performed(self, db: Session, user: MessagingUser, event_name: str, within_days: Optional[int]) -> bool:
        query = db.query(MessagingEvent.id).filter(
            MessagingEvent.project_id == user.project_id,
            MessagingEvent.user_id == user.id,
            MessagingEvent.event_name == event_name,
        )
        if within_days:
            since = datetime.utcnow() - timedelta(days=int(within_days))
            query = query.filter(MessagingEvent.created_at >= since)
        return db.query(query.exists()).scalar()

    def _campaign_seen(self, db: Session, user: MessagingUser, origin: str, within_days: Optional[int]) -> bool:
        query = db.query(MessagingEvent.id).filter(
            MessagingEvent.project_id == user.project_id,
            MessagingEvent.user_id == user.id,
            or_(
                MessagingEvent.campaign_origin == origin,
                MessagingEvent.attribution["campaign_origin"].astext == origin,
            ),
        )
        if within_days:
            since = datetime.utcnow() - timedelta(days=int(within_days))
            query = query.filter(MessagingEvent.created_at >= since)
        return db.query(query.exists()).scalar()

    def _matches_filter(self, db: Session, user: MessagingUser, flt: Dict[str, Any]) -> bool:
        field = str(flt.get("field") or "")
        operator = str(flt.get("operator") or "equals")
        expected = flt.get("value")
        expected_list = self._value_list(expected)
        within_days = flt.get("within_days")

        if field in {"tag", "tags"}:
            tags = [str(t) for t in (user.tags or [])]
            if operator in {"contains", "in", "any"}:
                return any(value in tags for value in expected_list)
            if operator in {"not_contains", "not_in"}:
                return all(value not in tags for value in expected_list)
            return expected in tags

        if field in {"segment", "segment_name"}:
            actual = user.segment_name
        elif field == "segment_rule_id":
            actual = str(user.segment_rule_id) if user.segment_rule_id is not None else None
        elif field == "lifecycle_stage":
            actual = user.lifecycle_stage
        elif field.startswith("property."):
            actual = self._property_value(user, field)
        elif field == "event":
            return self._event_performed(db, user, str(expected or ""), within_days)
        elif field == "campaign_origin":
            return self._campaign_seen(db, user, str(expected or ""), within_days)
        else:
            actual = self._property_value(user, field)

        actual_str = "" if actual is None else str(actual)
        if operator in {"equals", "eq"}:
            return actual_str == str(expected)
        if operator in {"not_equals", "neq"}:
            return actual_str != str(expected)
        if operator == "contains":
            return str(expected or "") in actual_str
        if operator in {"in", "any"}:
            return actual_str in expected_list
        if operator in {"exists", "present"}:
            return actual is not None and actual != ""
        if operator in {"missing", "not_exists"}:
            return actual is None or actual == ""
        return False

    def user_matches_rules(self, db: Session, audience: MessagingAudience, user: MessagingUser) -> bool:
        if self._is_static_audience(audience):
            return True
        filters = self._filters(audience.rule_config)
        if not filters:
            return True
        results = [self._matches_filter(db, user, flt) for flt in filters]
        return any(results) if self._match_mode(audience.rule_config) == "any" else all(results)

    def evaluate_user(self, db: Session, audience: MessagingAudience, user: MessagingUser) -> EvaluationResult:
        present = identifiers_present(user)
        if not self.user_matches_rules(db, audience, user):
            return EvaluationResult(False, "excluded", "rule_mismatch", present, {})
        if user.status != "active" or user.is_sandbox or user.is_blocked:
            return EvaluationResult(True, "excluded", "inactive_or_blocked", present, {})
        if user.global_opt_out or user.is_subscribed is False:
            return EvaluationResult(True, "excluded", "opted_out", present, {})
        if not any(present.values()):
            return EvaluationResult(True, "excluded", "missing_identifier", present, {})
        if not consent_manager.get_ads_consent(user, "ad_user_data") or not consent_manager.get_ads_consent(user, "ad_personalization"):
            return EvaluationResult(True, "excluded", "missing_ads_consent", present, {
                "ad_user_data": consent_manager.get_ads_consent(user, "ad_user_data"),
                "ad_personalization": consent_manager.get_ads_consent(user, "ad_personalization"),
            })
        return EvaluationResult(True, "eligible", "eligible", present, {})

    def preview(
        self,
        db: Session,
        project_id: int,
        rule_config: Optional[Dict[str, Any]],
        limit: int = 20,
        audience: Optional[MessagingAudience] = None,
    ) -> Dict[str, Any]:
        temp = audience or MessagingAudience(project_id=project_id, name="preview", rule_config=rule_config)
        if audience and self._is_static_audience(audience):
            users = (
                db.query(MessagingUser)
                .join(MessagingAudienceMembership, MessagingAudienceMembership.user_id == MessagingUser.id)
                .filter(
                    MessagingAudienceMembership.project_id == project_id,
                    MessagingAudienceMembership.audience_id == audience.id,
                )
                .limit(1000)
                .all()
            )
        elif temp.source_type in self.STATIC_SOURCE_TYPES:
            users = []
        else:
            users = db.query(MessagingUser).filter(MessagingUser.project_id == project_id).limit(1000).all()
        summary = {
            "total_users": len(users),
            "matched": 0,
            "eligible": 0,
            "missing_consent": 0,
            "missing_identifier": 0,
            "opted_out": 0,
            "excluded": 0,
            "samples": [],
        }
        for user in users:
            result = self.evaluate_user(db, temp, user)
            if result.matched:
                summary["matched"] += 1
            if result.state == "eligible":
                summary["eligible"] += 1
            elif result.reason == "missing_ads_consent":
                summary["missing_consent"] += 1
            elif result.reason == "missing_identifier":
                summary["missing_identifier"] += 1
            elif result.reason == "opted_out":
                summary["opted_out"] += 1
            else:
                summary["excluded"] += 1

            if len(summary["samples"]) < limit and result.matched:
                summary["samples"].append({
                    "user_id": user.id,
                    "external_id": user.external_id,
                    "display": user.email or user.name or user.external_id,
                    "state": result.state,
                    "reason": result.reason,
                    "identifiers_present": result.identifiers_present,
                })
        return summary

    def evaluate_memberships(self, db: Session, audience: MessagingAudience) -> List[MessagingAudienceMembership]:
        now = datetime.utcnow()
        memberships: List[MessagingAudienceMembership] = []
        existing = {
            row.user_id: row
            for row in db.query(MessagingAudienceMembership).filter(
                MessagingAudienceMembership.project_id == audience.project_id,
                MessagingAudienceMembership.audience_id == audience.id,
            ).all()
        }
        if self._is_static_audience(audience):
            users = (
                db.query(MessagingUser)
                .filter(
                    MessagingUser.project_id == audience.project_id,
                    MessagingUser.id.in_(existing.keys() or [0]),
                )
                .all()
            )
        else:
            users = db.query(MessagingUser).filter(MessagingUser.project_id == audience.project_id).all()

        for user in users:
            result = self.evaluate_user(db, audience, user)
            membership = existing.get(user.id)
            if not membership:
                membership = MessagingAudienceMembership(
                    project_id=audience.project_id,
                    audience_id=audience.id,
                    user_id=user.id,
                )
                db.add(membership)
            membership.state = result.state
            membership.eligibility_reason = result.reason
            membership.eligibility_details = result.details or None
            membership.identifiers_present = result.identifiers_present
            membership.last_evaluated_at = now
            memberships.append(membership)

        audience.last_evaluated_at = now
        db.commit()
        return memberships

    def _eligible_users(self, db: Session, audience: MessagingAudience) -> List[MessagingUser]:
        self.evaluate_memberships(db, audience)
        return (
            db.query(MessagingUser)
            .join(MessagingAudienceMembership, MessagingAudienceMembership.user_id == MessagingUser.id)
            .filter(
                MessagingAudienceMembership.project_id == audience.project_id,
                MessagingAudienceMembership.audience_id == audience.id,
                MessagingAudienceMembership.state == "eligible",
            )
            .all()
        )

    def _memberships_for_audience(self, db: Session, audience: MessagingAudience) -> List[MessagingAudienceMembership]:
        self.evaluate_memberships(db, audience)
        return db.query(MessagingAudienceMembership).filter(
            MessagingAudienceMembership.project_id == audience.project_id,
            MessagingAudienceMembership.audience_id == audience.id,
        ).all()

    def _payloads_for_users(self, users: Iterable[MessagingUser]) -> List[Dict[str, Any]]:
        payloads = []
        for user in users:
            hashes = provider_identifiers_for_user(user)
            if any(hashes.values()):
                payloads.append({
                    "user_id": user.id,
                    "hashes": hashes,
                    "identifiers_present": identifiers_present(user),
                })
        return payloads

    def run_sync(
        self,
        db: Session,
        audience: MessagingAudience,
        audience_destination: MessagingAudienceDestination,
        dry_run: bool = False,
    ) -> MessagingAudienceSyncJob:
        if audience_destination.provider_type not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported audience provider: {audience_destination.provider_type}")

        job = MessagingAudienceSyncJob(
            project_id=audience.project_id,
            audience_id=audience.id,
            audience_destination_id=audience_destination.id,
            provider_type=audience_destination.provider_type,
            operation="sync",
            status="running",
            dry_run=dry_run,
            started_at=datetime.utcnow(),
        )
        db.add(job)
        db.flush()

        memberships = self._memberships_for_audience(db, audience)
        provider_key = str(audience_destination.id)
        currently_eligible_ids = {row.user_id for row in memberships if row.state == "eligible"}
        previously_synced_ids = {
            row.user_id
            for row in memberships
            if isinstance(row.provider_status, dict)
            and isinstance(row.provider_status.get(provider_key), dict)
            and row.provider_status[provider_key].get("synced_state") == "included"
        }
        add_ids = currently_eligible_ids - previously_synced_ids
        remove_ids = set()
        if audience_destination.sync_mode in {"add_remove", "replace"}:
            remove_ids = previously_synced_ids - currently_eligible_ids

        users_by_id = {
            user.id: user
            for user in db.query(MessagingUser).filter(
                MessagingUser.project_id == audience.project_id,
                MessagingUser.id.in_(list(add_ids | remove_ids) or [0]),
            ).all()
        }
        add_payloads = self._payloads_for_users(users_by_id[user_id] for user_id in add_ids if user_id in users_by_id)
        remove_payloads = self._payloads_for_users(users_by_id[user_id] for user_id in remove_ids if user_id in users_by_id)

        job.total_count = len(memberships)
        job.eligible_count = len(currently_eligible_ids)
        job.add_count = len(add_payloads)
        job.remove_count = len(remove_payloads)
        job.request_summary = {
            "external_audience_id": audience_destination.external_audience_id,
            "sync_mode": audience_destination.sync_mode,
            "would_add": len(add_payloads),
            "would_remove": len(remove_payloads),
            "identifiers": {
                "email": sum(1 for p in add_payloads + remove_payloads if p["identifiers_present"].get("email")),
                "phone": sum(1 for p in add_payloads + remove_payloads if p["identifiers_present"].get("phone")),
                "external_id": sum(1 for p in add_payloads + remove_payloads if p["identifiers_present"].get("external_id")),
            },
        }

        for operation, batch in (("add", add_payloads), ("remove", remove_payloads)):
            for payload in batch:
                db.add(MessagingAudienceSyncItem(
                    project_id=audience.project_id,
                    job_id=job.id,
                    user_id=payload["user_id"],
                    operation=operation,
                    status="queued",
                    identifiers_present=payload["identifiers_present"],
                ))
        db.flush()

        result = self._send_to_provider(db, audience_destination, add_payloads, remove_payloads, dry_run=dry_run)
        job.status = result.status
        job.failed_count = result.failed_count
        job.response_summary = result.response_summary
        job.error_message = result.error_message
        job.finished_at = datetime.utcnow()

        item_status = "sent" if result.status in {"succeeded", "dry_run"} else "failed"
        db.query(MessagingAudienceSyncItem).filter(MessagingAudienceSyncItem.job_id == job.id).update(
            {"status": item_status, "provider_response": result.response_summary},
            synchronize_session=False,
        )

        audience_destination.last_sync_status = job.status
        audience_destination.last_sync_at = job.finished_at
        audience_destination.last_error = job.error_message
        if result.status in {"succeeded", "dry_run"}:
            now = datetime.utcnow()
            membership_by_user_id = {row.user_id: row for row in memberships}
            for user_id in add_ids:
                membership = membership_by_user_id.get(user_id)
                if membership:
                    provider_status = dict(membership.provider_status or {})
                    provider_status[provider_key] = {
                        "synced_state": "included",
                        "last_operation": "add",
                        "last_job_id": job.id,
                        "last_synced_at": now.isoformat(),
                    }
                    membership.provider_status = provider_status
                    membership.last_synced_at = now
            for user_id in remove_ids:
                membership = membership_by_user_id.get(user_id)
                if membership:
                    provider_status = dict(membership.provider_status or {})
                    provider_status[provider_key] = {
                        "synced_state": "excluded",
                        "last_operation": "remove",
                        "last_job_id": job.id,
                        "last_synced_at": now.isoformat(),
                    }
                    membership.provider_status = provider_status
                    membership.last_synced_at = now
        db.commit()
        db.refresh(job)
        try:
            from app.services.messaging.audience_webhook_delivery import audience_webhook_delivery_service
            audience_webhook_delivery_service.queue_sync_job_events(db, job, audience_destination)
        except Exception:
            logger.exception("Failed to queue audience webhook events for sync job %s", job.id)
        return job

    def _send_to_provider(
        self,
        db: Session,
        audience_destination: MessagingAudienceDestination,
        add_payloads: List[Dict[str, Any]],
        remove_payloads: List[Dict[str, Any]],
        dry_run: bool,
    ) -> ProviderSyncResult:
        if dry_run:
            return ProviderSyncResult("dry_run", {
                "provider": audience_destination.provider_type,
                "would_add": len(add_payloads),
                "would_remove": len(remove_payloads),
            })
        destination = None
        config: Dict[str, Any] = {}
        if audience_destination.destination_id:
            destination = db.query(MessagingDestination).filter(
                MessagingDestination.id == audience_destination.destination_id,
                MessagingDestination.project_id == audience_destination.project_id,
            ).first()
            if destination:
                config = destination_config.decrypt_config(destination.config_encrypted) or {}

        if audience_destination.provider_type == "meta_pixel":
            return self._sync_meta(config, audience_destination, add_payloads, remove_payloads)
        if audience_destination.provider_type == "google_ads":
            return self._sync_google(config, audience_destination, add_payloads, remove_payloads)
        return ProviderSyncResult("skipped", {
            "provider": "tiktok",
            "message": "TikTok customer-file audience adapter is available as a beta contract but credentials are not wired for this tenant yet.",
            "would_add": len(add_payloads),
            "would_remove": len(remove_payloads),
        })

    def _sync_meta(
        self,
        config: Dict[str, Any],
        audience_destination: MessagingAudienceDestination,
        add_payloads: List[Dict[str, Any]],
        remove_payloads: List[Dict[str, Any]],
    ) -> ProviderSyncResult:
        access_token = config.get("access_token") or config.get("api_key")
        external_id = audience_destination.external_audience_id
        payloads = add_payloads + remove_payloads
        if not access_token or not external_id:
            return ProviderSyncResult("failed", {
                "provider": "meta",
                "uploaded": 0,
                "missing": ["access_token" if not access_token else None, "external_audience_id" if not external_id else None],
            }, failed_count=len(payloads), error_message="Meta audience sync requires access_token and external_audience_id.")

        api_version = str(config.get("api_version") or "v20.0").lstrip("/")
        schema = ["EMAIL_SHA256", "PHONE_SHA256", "EXTERN_ID"]
        def rows(batch: List[Dict[str, Any]]) -> List[List[str]]:
            return [
                [
                    payload["hashes"].get("email_sha256") or "",
                    payload["hashes"].get("phone_sha256") or "",
                    payload["hashes"].get("external_id_sha256") or "",
                ]
                for payload in batch
            ]

        def payload_body(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
            return {
                "schema": schema,
                "data": rows(batch),
                "is_raw": False,
            }

        if not payloads:
            return ProviderSyncResult("succeeded", {
                "provider": "meta",
                "uploaded": 0,
                "removed": 0,
                "message": "No audience diff to sync.",
            })

        try:
            responses = {}
            with httpx.Client(timeout=30) as client:
                if add_payloads:
                    response = client.post(
                        f"https://graph.facebook.com/{api_version}/{external_id}/users",
                        data={
                            "payload": json.dumps(payload_body(add_payloads)),
                            "access_token": access_token,
                        },
                    )
                    responses["add"] = response
                if remove_payloads:
                    response = client.request(
                        "DELETE",
                        f"https://graph.facebook.com/{api_version}/{external_id}/users",
                        data={
                            "payload": json.dumps(payload_body(remove_payloads)),
                            "access_token": access_token,
                        },
                    )
                    responses["remove"] = response
            ok = all(response.status_code < 400 for response in responses.values())
            summary = {
                "provider": "meta",
                "uploaded": len(add_payloads) if ok else 0,
                "removed": len(remove_payloads) if ok else 0,
                "responses": {
                    key: {
                        "status_code": response.status_code,
                        "body": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text[:1000],
                    }
                    for key, response in responses.items()
                },
            }
            return ProviderSyncResult("succeeded" if ok else "failed", summary, failed_count=0 if ok else len(payloads), error_message=None if ok else "Meta audience sync returned an error")
        except Exception as exc:
            logger.exception("Meta audience sync failed")
            return ProviderSyncResult("failed", {"provider": "meta"}, failed_count=len(payloads), error_message=str(exc))

    def _google_access_token(self, config: Dict[str, Any]) -> Optional[str]:
        if config.get("access_token"):
            return config.get("access_token")
        refresh_token = config.get("refresh_token")
        client_id = config.get("oauth_client_id") or config.get("client_id")
        client_secret = config.get("oauth_client_secret") or config.get("client_secret")
        if not refresh_token or not client_id or not client_secret:
            return None
        response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json().get("access_token")

    def _sync_google(
        self,
        config: Dict[str, Any],
        audience_destination: MessagingAudienceDestination,
        add_payloads: List[Dict[str, Any]],
        remove_payloads: List[Dict[str, Any]],
    ) -> ProviderSyncResult:
        payloads = add_payloads + remove_payloads
        customer_id = str(config.get("customer_id") or "").replace("-", "")
        developer_token = config.get("developer_token")
        login_customer_id = str(config.get("manager_login_customer_id") or config.get("login_customer_id") or "").replace("-", "")
        user_list = audience_destination.external_audience_id
        if user_list and not str(user_list).startswith("customers/"):
            user_list = f"customers/{customer_id}/userLists/{user_list}"
        missing = [
            name for name, value in {
                "customer_id": customer_id,
                "developer_token": developer_token,
                "external_audience_id": user_list,
            }.items() if not value
        ]
        if missing:
            return ProviderSyncResult("failed", {
                "provider": "google_ads",
                "missing": missing,
                "uploaded": 0,
            }, failed_count=len(payloads), error_message="Google Customer Match requires customer_id, developer_token, OAuth token, and user list ID.")

        try:
            access_token = self._google_access_token(config)
        except Exception as exc:
            return ProviderSyncResult("failed", {"provider": "google_ads"}, failed_count=len(payloads), error_message=f"Google OAuth failed: {exc}")
        if not access_token:
            return ProviderSyncResult("failed", {"provider": "google_ads", "missing": ["access_token"]}, failed_count=len(payloads), error_message="Google OAuth access token is missing.")

        api_version = str(config.get("api_version") or "v24").strip().lstrip("/")
        base = f"https://googleads.googleapis.com/{api_version}/customers/{customer_id}/offlineUserDataJobs"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "developer-token": developer_token,
            "Content-Type": "application/json",
        }
        if login_customer_id:
            headers["login-customer-id"] = login_customer_id

        create_body = {
            "job": {
                "type": "CUSTOMER_MATCH_USER_LIST",
                "customerMatchUserListMetadata": {
                    "userList": user_list,
                    "consent": {
                        "adUserData": "GRANTED",
                        "adPersonalization": "GRANTED",
                    },
                },
            }
        }
        operations = []
        for operation_name, batch in (("create", add_payloads), ("remove", remove_payloads)):
            for payload in batch:
                identifiers = []
                if payload["hashes"].get("email_sha256"):
                    identifiers.append({"hashedEmail": payload["hashes"]["email_sha256"]})
                if payload["hashes"].get("phone_sha256"):
                    identifiers.append({"hashedPhoneNumber": payload["hashes"]["phone_sha256"]})
                if payload["hashes"].get("external_id_sha256"):
                    identifiers.append({"thirdPartyUserId": payload["hashes"]["external_id_sha256"]})
                if identifiers:
                    operations.append({operation_name: {"userIdentifiers": identifiers}})

        if not operations:
            return ProviderSyncResult("succeeded", {
                "provider": "google_ads",
                "uploaded": 0,
                "removed": 0,
                "message": "No audience diff to sync.",
            })

        try:
            with httpx.Client(timeout=45) as client:
                create_response = client.post(f"{base}:create", headers=headers, json=create_body)
                create_response.raise_for_status()
                resource_name = create_response.json().get("resourceName")
                if not resource_name:
                    raise RuntimeError("Google did not return an offline user data job resource name")
                ops_response = client.post(
                    f"https://googleads.googleapis.com/{api_version}/{resource_name}:addOperations",
                    headers=headers,
                    json={"operations": operations, "enablePartialFailure": True},
                )
                run_response = client.post(
                    f"https://googleads.googleapis.com/{api_version}/{resource_name}:run",
                    headers=headers,
                    json={},
                )
            ok = ops_response.status_code < 400 and run_response.status_code < 400
            return ProviderSyncResult("succeeded" if ok else "failed", {
                "provider": "google_ads",
                "resource_name": resource_name,
                "add_operations_status": ops_response.status_code,
                "run_status": run_response.status_code,
                "uploaded": len(add_payloads) if ok else 0,
                "removed": len(remove_payloads) if ok else 0,
                "partial_failure": self._safe_json(ops_response),
            }, failed_count=0 if ok else len(payloads), error_message=None if ok else "Google Customer Match job failed")
        except Exception as exc:
            logger.exception("Google Customer Match sync failed")
            return ProviderSyncResult("failed", {"provider": "google_ads"}, failed_count=len(payloads), error_message=str(exc))

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text[:1000]


audience_sync_service = AudienceSyncService()
