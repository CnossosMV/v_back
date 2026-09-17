# Project onboarding via MCP

Versya onboarding is a guided configuration proposal, not a wizard that turns
every feature on. A tenant-owned code agent starts with
`get_project_onboarding_contract`, interviews the tenant in business language,
inventories only tenant-visible state and recommends the smallest coherent
end-to-end value slice.

The contract is project-specific and read-only. It returns public project
metadata, safe inventory counts, lifecycle model summaries, the tenant
interview, a configuration ladder and the required proposal format. Calling it
has `external_sends=0`. The inventory is scope-aware: bootstrap access exposes
the base project view, while lifecycle, import and campaign details are omitted
and returned with the exact missing read scope until that scope is granted.

## Evidence discipline

Every statement in the agent's proposal must be labeled as one of:

- `observed`: returned by MCP or supplied as a source fact;
- `inferred`: a conclusion with its evidence and uncertainty;
- `recommended`: a proposed default with reasons and trade-offs;
- `tenant_decided`: an explicit business choice made by the tenant.

An agent must never turn a recommendation into a tenant decision, invent a
lifecycle key or purpose priority, or ask a novice tenant to author tool JSON.
The agent translates business answers into a dry-run payload.

## Zero-to-one sequence

1. Call `get_mcp_status`; require a durable OAuth session and only the read
   scopes needed for discovery.
2. Resolve the target with `list_projects`, then call
   `get_project_overview` and `get_project_onboarding_contract` with the
   explicit `project_id`.
3. Ask about first business value, identity/source truth, consent, current
   commercial relationships, market/locales, approved channels, attention
   ordering and operating autonomy.
4. Inventory channels, templates, variables, funnels, Event Actions, campaigns,
   Recipes and lifecycle models through their list/contract tools.
5. Present a gap matrix and phased proposal. Each phase is `ready`,
   `needs_tenant_decision`, `blocked` or `defer`.
6. Request write scopes only when an approved phase needs them. Every mutation
   begins with dry-run and shows consequences before confirmation.
7. Establish stable identity, consent and live factual ingestion. Use Project
   Import only when historical source data exists; import does not replace the
   live path.
8. Create or reuse one lifecycle model, validate it and compare it in shadow.
9. Define purpose and attention policy before activating journeys.
10. Build one purpose end to end in draft/shadow. Expand only after its facts,
    explanations, collisions, monitoring and rollback are reviewable.

## Required proposal

The agent's first deliverable contains:

1. project snapshot;
2. business answers and open questions;
3. recommended foundation with reasons;
4. phased configuration plan and dependencies;
5. exact next MCP calls and required scopes;
6. approval and `consequence_at_gate` boundaries;
7. tenant-safe product gaps.

Discovery, model preview, Recipe planning and campaign materialization must all
report zero external sends. Only a separately confirmed executable boundary,
such as `schedule_campaign_run`, authorizes future sends.

## New project versus migration

A new project skips Project Import when no historical system exists. It still
needs identity, consent, ingestion, lifecycle semantics and purpose/attention
decisions. A migration preserves the existing Versya project, imports only
source-owned history/facts, excludes history already originating in Versya and
then joins the same onboarding ladder.

If a required setting has no project-scoped MCP or documented public write
path, the agent reports a product gap. It must not switch to SSH, SQL, a private
route or a provider console.

## Persistent Project Setup Plan

The initial proposal is durable project state, not chat-only context. After
`assess_project_setup`, the agent creates a version with
`create_project_setup_plan`. A plan stores phases, dependencies, required
scopes/tools/tenant decisions, blockers, approval questions, assumptions,
product gaps and exit evidence.

Business facts and choices are append-only revisions through
`record_project_setup_decision`. Every revision is labeled `observed`,
`inferred`, `recommended` or `tenant_decided`; only an explicit
`tenant_decided` value can be accepted. The plan fingerprint includes the
latest revision of every decision. Changing a decision therefore makes prior
phase approvals stale and forces review of the new facts.

`approve_project_setup_phase` records business approval for one exact
fingerprint. It always returns `execution_authorized=false` and
`external_sends=0`. Approval never dispatches generic tool calls, activates a
module or authorizes a send. The agent executes each approved phase with the
specific module tool and its independent dry-run, consequence gate and
confirmation token.

Use `list_project_setup_plans`, `get_project_setup_plan` and
`compare_project_setup_plans` to resume or review across sessions. Use
`set_project_setup_plan_status` to complete, supersede or archive a plan;
history is never silently deleted.

## Project foundation semantics

`configure_project_foundation` is the tenant-facing write path for project
fallbacks (`default_locale`, `supported_locales`, `default_timezone`) and
`goal_event`. It keeps accepting `market_config` as a compatibility bulk input,
but new agents use the first-class market contract and CRUD tools. All writes
require `versya.projects:write`, project admin, dry-run and payload-bound
confirmation.

A market is a business and calendar boundary: stable key, country, optional
regions, timezone, supported locales and calendar tags. Locale remains a
content-variant axis. A tenant can therefore use more than one locale in a
market or the same locale in more than one market without conflating the two.
The `goal_event` is a stable factual event key proving the project's primary
outcome, not a sentence or inferred lifecycle state.

Markets are canonical rows in `project_markets`; `projects.market_config` is a
synchronized compatibility projection. Agents call
`get_project_market_contract`, paginate `list_project_markets`, and use
`create_project_market`, `update_project_market`,
`set_default_project_market` and `set_project_market_status`. The page-size cap
does not limit how many markets a project may own. See
[Project Markets](PROJECT-MARKETS.md).

## Agent skill distribution

The self-contained `versya-project-onboarding` skill is published as a
versioned ZIP with a deterministic checksum. `get_agent_skill_catalog` returns
its manifest, direct entrypoint, download URL, SHA-256 and fallback contract.
Public read-only endpoints are rooted at
`/agent-skills/versya-project-onboarding/`. If an agent cannot install a skill,
`get_project_onboarding_contract` keeps the MCP workflow self-describing.
