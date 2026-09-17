"""Self-describing tenant contract for continuous profile and event ingestion."""

from __future__ import annotations

from typing import Any

from app.mcp.config import mcp_resource_url


def event_ingestion_contract_document() -> dict[str, Any]:
    base = mcp_resource_url("").rstrip("/")

    def endpoint(path: str) -> str:
        return f"{base}{path}" if base else path

    return {
        "contract": "versya.live-ingestion",
        "contract_version": "1.0",
        "control_plane": "MCP",
        "data_plane": "authenticated HTTPS JSON",
        "endpoints": {
            "contact_snapshot": {
                "method": "POST",
                "url": endpoint("/api/v1/users"),
                "required_permission": "identify",
                "purpose": "Upsert identity and current source-owned contact properties.",
            },
            "event": {
                "method": "POST",
                "url": endpoint("/api/v1/events"),
                "required_permission": "track",
                "purpose": "Append one factual occurrence; may carry a current contact patch atomically.",
            },
        },
        "authentication": {
            "kind": "project_backend_api_key_hmac_sha256",
            "headers": ["X-API-Key", "X-Timestamp", "X-Signature", "X-Nonce"],
            "signature_input": "<unix_timestamp>.<exact_request_body_bytes>",
            "nonce": "Fresh unpredictable value per HTTP attempt; an accepted nonce cannot be reused.",
            "secret_handling": "Store only in the source secret manager; never in source control, bundles, logs or prompts.",
            "provisioning": (
                "Project admins may call create_event_ingestion_key through MCP. It is dry-run and confirmation "
                "gated, grants only identify/track, and returns the secret once."
            ),
        },
        "identity": {
            "upsert_key": "user_id",
            "target_field": "MessagingUser.external_id scoped to project_id",
            "mutable_endpoints": ["email", "phone"],
            "rule": "Email and phone are not contact upsert keys.",
        },
        "event_envelope": {
            "required": ["event", "event_id", "user_id"],
            "optional": [
                "anonymous_id", "properties", "contact_properties", "timestamp",
                "session_id", "context", "ads_consent", "email_permission",
            ],
            "event_id": (
                "Stable source occurrence ID. Reusing it for the same event/contact returns the existing event; "
                "reusing it for another event/contact is a conflict. Equal concurrent live retries are serialized."
            ),
            "properties": "Immutable occurrence facts; not a general contact-profile patch.",
            "contact_properties": (
                "Current contact facts deep-merged into the profile. Explicit false/null values are retained. "
                "A source namespace may carry _observed_at; an older delayed snapshot cannot replace a newer one."
            ),
            "timestamp": "Business occurrence time in RFC 3339; server ingestion time remains separate.",
        },
        "contact_snapshot_envelope": {
            "required": ["user_id"],
            "optional": ["email", "phone", "name", "properties", "email_permission"],
            "properties": "Deep-merged current facts; keep source-owned truth under one top-level namespace.",
            "namespace_clock": (
                "Put RFC 3339 _observed_at inside each source-owned top-level namespace to prevent delayed retries "
                "from regressing current truth."
            ),
        },
        "semantics": {
            "current_truth": "Lifecycle property rules read the contact snapshot, not arbitrary event properties.",
            "occurrences": "Events are append-only facts and may feed event/aggregate lifecycle rules.",
            "ordering": "When both change, publish the current contact snapshot before or with the event.",
            "source_delivery": "Use a durable outbox, preserve user_id/event_id and retry transient failures with backoff.",
            "consent": "Use email_permission evidence; a restrictive denial or withdrawal wins over an older grant.",
            "ownership": "Facts continue in legacy, shadow and versya modes; purpose_key separately owns decisions.",
        },
        "lifecycle": {
            "convergence": "Active and shadow models are materialized asynchronously by the position sweep.",
            "verification": [
                "search_contacts by stable external user_id",
                "get_contact_timeline and confirm the stable event_id",
                "explain_contact_lifecycle and compare current versus stored Position",
                "get_event_ingestion_status and require no missing recent Positions",
            ],
            "shadow_gate": "External sends caused by the shadow lifecycle model must remain zero.",
        },
        "tenant_canaries": [
            "first identified registration",
            "free or trial activation",
            "trial exhaustion and time expiry",
            "first positive completed payment",
            "payment issue",
            "cancellation or expiry",
            "consent withdrawal",
            "exact retry and delayed delivery",
        ],
        "boundary": (
            "Use MCP and documented public endpoints only. Missing key provisioning, health or evidence "
            "capabilities are product gaps; SSH/SQL are not tenant workflow fallbacks."
        ),
    }
