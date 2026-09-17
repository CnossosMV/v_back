# Project Import v1 — agent contract

Project Import is a customer-push workflow for contacts, consent evidence, factual event history, terminal message history, lifecycle semantics and draft/disabled automation definitions. Keep the existing Versya project for the same product/environment; a new project is for a distinct brand, legal boundary, environment or identity namespace.

## Sequence

1. Call `list_projects` and retain an explicit target `project_id`.
2. Call `get_project_import_contract`; stop on a version mismatch.
3. Export deterministic source-owned IDs. Email and phone detect collisions but never select or merge contacts. Omit history that originated in the same Versya project.
4. Create/resume by stable `client_import_id`.
5. Execute the returned raw HTTPS `PUT` with the exact ZIP bytes and every returned header, including the one-time upload token.
6. Validate. Repair every blocking finding in a new/corrected artifact; never patch the Versya database.
7. Dry-run apply. Require `can_apply=true`, zero external sends, zero live event dispatch and zero enabled automations.
8. Request confirmation, review its bundle checksum and validation fingerprint, then repeat with the short-lived token to enqueue.
9. Poll `get_project_import` through `queued`, `applying`, `reconciling`, and `completed`.
10. Call `audit_project_import`; completion is accepted only when `accepted=true`. Page `list_project_import_records` if a gate fails.
11. Call `get_lifecycle_model_contract`, inspect distribution/materialization parity and explain representative contacts. Put the reviewed model in shadow.
12. Define stable business-intent `purpose_key` values, inventory unmapped automations and transfer one purpose at a time. Import completion is neither lifecycle activation nor orchestration cutover.

## Bundle

Transport is ZIP, at most 100 MiB compressed, 500 MiB expanded, 32 files and 2 MiB per NDJSON line. Required files are `manifest.json` and `contacts.ndjson`. Optional files are events, permissions, messages, position snapshots/transitions and JSON arrays under `automations/` for templates, event schemas/actions, funnels and campaigns. The manifest declares every data file with SHA-256 and record count.

`contacts.ndjson.external_id` is the only contact upsert key. Historical events/messages have stable source `external_id` values plus `contact_external_id`; they remain idempotent across different imports. Facts enter as processed `derive_only`, decisions/outcomes/system events as `audit_only`, and messages as terminal historical ledger rows. Imported automations stay disabled/draft. Restrictive opt-out always wins.

For a correction snapshot, use `import_mode=authoritative_fields`. A field such as `email` may be listed directly. Use `properties` only when the source intentionally owns and replaces the complete properties object. Prefer `properties.<namespace>`—for example `properties.tabloide`—when correcting one source-owned top-level namespace; Versya preserves every other namespace without requiring the agent to read or merge target properties. Do not declare both `properties` and a namespaced property field.

The machine-readable contract returned by `get_project_import_contract` is authoritative for supported fields, states, finding dispositions and acceptance gates.

Lifecycle axes are platform concepts, while Type/Stage/Age keys are project taxonomy. A tenant may use the recommended subscription baseline or another model. See [Lifecycle and orchestration](LIFECYCLE-ORCHESTRATION.md).
