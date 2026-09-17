# Live event ingestion — agent contract

Project Import is a snapshot. New contacts and changing lifecycle facts use the public backend API; MCP remains the discovery and audit control plane. A tenant agent must call `get_event_ingestion_contract` instead of inferring request fields from a sample integration. A project admin can provision the least-privilege runtime credential with `create_event_ingestion_key`: dry-run, checksum-like confirmation binding, and a one-time secret returned only after confirmation.

Use signed `POST /api/v1/users` to upsert identity and current source-owned contact properties. Use signed `POST /api/v1/events` to append occurrences. An event can include `contact_properties` when a source transaction changes current truth and emits an event together. Contact patches are recursively merged so one source namespace does not overwrite another; explicit false/null values clear stale truth. Put RFC 3339 `_observed_at` inside each source-owned top-level namespace: a delayed older snapshot remains an event fact but cannot regress newer contact truth.

Every event retry should retain a stable source `event_id`. The same ID for the same project, contact and event returns the existing Versya event, including simultaneous live retries. Reuse for another contact or event name is rejected. `X-Nonce` identifies one signed HTTP attempt and must be fresh on retry. `timestamp` is the business occurrence time; server ingestion time remains independent.

`user_id` is the stable source contact ID. Email and phone are mutable endpoints and collision evidence, not upsert keys. Keep commercial truth under a source namespace and distinguish a positive paid entitlement/payment from a merely active free or trial row.

Lifecycle property rules read current contact properties; arbitrary event properties do not silently become profile truth. After a canary, use `search_contacts`, `get_contact_timeline`, `explain_contact_lifecycle` and `get_event_ingestion_status`. Active and shadow Positions converge asynchronously through the position sweep. Shadow must cause zero external sends.

Use a source outbox, publish current truth before or with the occurrence, preserve IDs across retries, and quarantine permanent schema/auth errors. Permission is dedicated evidence, not a profile boolean; restrictive denial/withdrawal wins. Facts continue in every orchestration mode while `purpose_key` controls decisions.

The tenant workflow may not depend on SSH, SQL, container access or unpublished routes. If a required capability is absent from MCP/public API, record it as a product gap rather than bypassing the tenant boundary.
