# Versya Remote MCP

Versya exposes its tenant-safe product MCP at `https://api.versya.io/mcp/`. Authentication uses Keycloak OAuth, connector scopes and project membership. Resolve the target with `list_projects` and pass `project_id` explicitly; never infer an ID from documentation.

## Connect an agent

Enable a product MCP connector for the user in Versya first and grant only the project/scopes needed for the job. A Codex user can then connect without a copied bearer token or client secret:

```bash
codex mcp add versya --url https://api.versya.io/mcp/
codex mcp login versya --scopes offline_access,versya.projects:read,versya.lifecycle:read,versya.lifecycle:write,versya.ingestion:read,versya.ingestion:write
```

Keep `--scopes` explicit and limited to the workflow. Do not rely on a client's automatic scope selection: an authorization server may publish scopes used by other, separately protected resources. Versya rejects `versya.admin:ops` during tenant client registration.

The protected-resource metadata identifies the canonical `https://api.versya.io/mcp/` URL as its `resource`. The unauthenticated endpoint advertises `versya.projects:read offline_access` as its bootstrap grant, so OAuth clients receive a usable audience-bound token and a consented offline refresh token for long-running work. Short access tokens remain intentional and are refreshed by the OAuth client. Agents must request the additional product scopes required by their planned workflow. Reauthorize whenever the workflow expands; restarting the client does not add scopes to an existing token. Start every workflow with `get_mcp_status` and require `oauth_session.long_running_agent_ready=true`; otherwise reauthorize with `offline_access` before continuing.

Restart or reactivate the MCP server after login so the newly authorized tools are loaded. Other OAuth-capable MCP clients follow the same discovery flow from the protected-resource metadata advertised by Versya.

Agent OAuth clients are public PKCE clients registered dynamically. Registration accepts only loopback callback hosts (`127.0.0.1` and `localhost` by default), requires user consent, permits the standard OIDC scopes `openid`, `profile` and `email`, explicitly permits the `offline_access` session scope and remains subject to Keycloak's maximum-client policy. These standard scopes let current OAuth-capable agents complete dynamic registration; they do not grant Versya product permissions. `offline_access` changes refresh durability only and does not bypass connector or project authorization. The product endpoint metadata never advertises `versya.admin:ops`, even when the separately mounted administrative MCP is enabled. Tenant clients never receive that scope; it requires a dedicated client and explicit platform opt-in. The effective product permission on every call is the intersection of token scopes, the user's active MCP connector installation and project membership.

Project discovery returns only tenant-visible project metadata. Cryptographic material such as the internal PII hashing salt is never part of `list_projects` or `get_project_overview`.

## Project onboarding and migration tools

- `get_project_onboarding_contract`: project-specific zero-to-one interview, safe inventory, configuration ladder and proposal contract. It is read-only and returns `external_sends=0`.
- `get_event_ingestion_contract`: authoritative public profile/event envelope, HMAC, identity and convergence policy.
- `get_event_ingestion_status`: API-key metadata, stable event-ID coverage and recent-contact lifecycle convergence without SSH/SQL.
- `create_event_ingestion_key`: confirmation-gated least-privilege `identify`/`track` credential; its secret is returned once.
- `get_project_import_contract`: self-describing bundle, identity, safety and workflow contract.
- `create_project_import`: idempotently create/resume and issue a one-time binary upload capability.
- `list_project_imports`, `get_project_import`: resume and poll durable state.
- `validate_project_import`: current-state validation with grouped actionable findings.
- `apply_project_import`: revalidated dry-run, checksum/fingerprint-bound confirmation and durable enqueue.
- `audit_project_import`: machine-checkable acceptance gates and ledger summary.
- `list_project_import_records`: paginated source-to-target reconciliation ledger.
- `get_lifecycle_model_contract`, `validate_lifecycle_model_definition`, `create_lifecycle_model`: interview, validate and register a project taxonomy without editing JSON in the UI.
- `list_lifecycle_models`, `get_lifecycle_distribution`, `compare_lifecycle_materialization`, `explain_contact_lifecycle`, `compare_lifecycle_models`, `activate_lifecycle_model`: observe and approve lifecycle separately from import.
- `get_orchestration_contract`, `list_orchestration_purposes`, `assess_orchestration_readiness`, `list_orchestration_cutovers`, `set_orchestration_cutover`: define intent, find unmanaged automations and transfer purpose/epoch ownership.
- `get_attention_planning_contract`: explain the tenant choices and platform invariants for bounded future attention.
- `preview_contact_attention_plan`: show due and known-future contenders, projected owner, dispatch candidate and `consequence_at_gate` for one contact without sending.
- `configure_attention_planning`: validate dependencies, return exact consequences, then apply project policy only through a payload-bound confirmation token.
- `list_campaigns`, `get_campaign`, `create_draft_campaign`, `update_draft_campaign`, `preview_campaign_plan`: author and evaluate campaigns without activation, runs or sends.
- `get_campaign_recipe_contract`, `preview_campaign_recipe`, `list_campaign_recipes`, `get_campaign_recipe`, `create_draft_campaign_recipe`, `update_campaign_recipe`, `set_campaign_recipe_state`: persist recurring audience/calendar/copy policy without creating a run or send.
- `get_campaign_planning_inbox`, `materialize_campaign_episode`: expose dated copy work and create inactive campaign drafts only.
- `evaluate_campaign_run_consequences`, `choose_campaign_run_consequence`, `schedule_campaign_run`, `get_campaign_run_status`, `cancel_campaign_run`: complete the consequence-gated run lifecycle. Only confirmed scheduling authorizes future sends; cancellation cannot recall provider calls already submitted.
- `preview_orchestration_impact`: compare a campaign, funnel, Event Action or event-triggered template with every active definition and return an exact approval fingerprint with zero sends. Same-purpose Event Action variants coexist only when their event predicates are provably disjoint; uncertain overlap remains a blocker.
- `list_channel_health`: distinguish outbound attempts from provider-submitted sends, exclude imported history, and expose aggregated status and blocked-reason counts without recipient data.
- `activate_campaign`, `activate_funnel`, `set_event_action_state`, `activate_template_automation`: confirmation-gated activation through the common impact contract. Template creation/import never enables event execution.

### Persistent setup and skill tools

- `get_agent_skill_catalog`: versioned onboarding skill URLs, package checksum and install fallback.
- `assess_project_setup`: scope-aware foundation readiness and missing tenant decisions.
- `create_project_setup_plan`, `list_project_setup_plans`, `get_project_setup_plan`, `compare_project_setup_plans`: durable, versioned proposals across agent sessions.
- `record_project_setup_decision`: append-only evidence-labeled decision revisions; only tenant-decided values may be accepted.
- `approve_project_setup_phase`: business approval bound to the current plan fingerprint; it never executes a module or authorizes sends.
- `set_project_setup_plan_status`: complete, supersede or archive without deleting history.
- `configure_project_foundation`: exact-diff, confirmation-gated project fallback/goal configuration with legacy market bulk compatibility.
- `get_project_market_contract`, `list_project_markets`, `create_project_market`, `update_project_market`, `set_default_project_market`, `set_project_market_status`: paginated first-class multi-market lifecycle.

Setup reads use `versya.projects:read`. Plan/foundation writes use
`versya.projects:write`; phase approval and foundation changes additionally
require project admin. Market page size is an operational payload bound, not a
project market-count limit. Every setup operation reports zero external sends.

Required migration scopes are `versya.projects:read`, `versya.imports:read`, `versya.imports:write`, `versya.lifecycle:read` and `versya.lifecycle:write`. Live ingestion status uses `versya.ingestion:read`; credential provisioning uses `versya.ingestion:write`. Attention planning inspection uses `versya.automations:read` and per-contact preview also uses `versya.contacts:read`; configuration uses `versya.automations:write`. Campaign authoring additionally uses `versya.campaigns:read` and `versya.campaigns:write`. Import application, lifecycle activation, credential provisioning, attention-policy configuration and cutover also require project admin and are separate approvals.

The agent builds the ZIP locally. MCP is the control plane; binary bytes go to the exact `upload.raw_url` and headers returned by `create_project_import`. The one-time `X-Versya-Upload-Token` is bound to the project/import, expires quickly and is consumed after success. The agent never needs the connector OAuth credential and never base64-encodes the bundle into model context.

No project-onboarding, migration or campaign-planning step requires SSH, SQL, production shell access or Versya access to source database credentials. Live integration uses the public signed data plane and is verified through MCP. See [Project onboarding](PROJECT-ONBOARDING.md), [Project markets](PROJECT-MARKETS.md), [Project Import](PROJECT-IMPORT.md), [Lifecycle and orchestration](LIFECYCLE-ORCHESTRATION.md), [Campaign recipes](CAMPAIGN-RECIPES.md) and [Live event ingestion](EVENT-INGESTION.md).
