---
name: versya-project-onboarding
description: "Guide a new or incompletely configured Versya tenant from business interview to a persistent, reviewable Project Setup Plan and then execute approved phases safely through tenant-facing MCP. Use when a tenant asks how to configure a Versya project, migrate an existing product, define lifecycle and attention, or start campaigns without bypassing gates."
---

# Versya Project Onboarding

Operate only through the tenant-facing Versya MCP and documented public data
endpoints. Never use SSH, SQL, containers, unpublished routes or provider
consoles to fill a product capability gap.

Read `references/project-setup-runbook.md` before proposing or changing a
project, and read `references/project-markets.md` whenever markets, calendars,
locales, regional channels or regional destinations are in scope. Treat the MCP
contracts as authoritative when they are newer than this skill.

## Mandatory opening sequence

1. Call `get_mcp_status`. Require `oauth_session.long_running_agent_ready=true`.
2. Call `list_projects`, resolve the tenant's intended project and pass its
   explicit `project_id` to every later tool.
3. Call `get_agent_skill_catalog`, `get_project_onboarding_contract` and
   `assess_project_setup`.
4. Inventory only with granted read scopes. Hidden counts are unknown, not zero.
5. Interview in business language. Never ask a novice tenant to invent JSON,
   lifecycle keys, `purpose_key`, integer priority or attention syntax.

Label every material value `observed`, `inferred`, `recommended` or
`tenant_decided`. Only an explicit tenant choice may be recorded as an accepted
`tenant_decided` decision.

## Persist the proposal

Create a versioned Project Setup Plan with `create_project_setup_plan`. Keep the
smallest coherent phases and their dependencies, required decisions, scopes,
tools, blockers, approval question and exit evidence. Use
`record_project_setup_decision` for each business choice and source-backed fact.
Use `get_project_setup_plan` after every decision because the plan fingerprint
changes and previous phase approvals may become stale.

Phase approval is not execution authorization. `approve_project_setup_phase`
only records that the tenant accepts the exact proposal and decisions at that
fingerprint. Every module write still needs its own dry-run, consequence gate
and confirmation token. No setup-plan operation authorizes an external send.

Use `compare_project_setup_plans` before replacing a proposal and
`set_project_setup_plan_status` to complete, supersede or archive it. Never
silently edit history.

## Execute approved phases

- Configure project fallbacks and the observable success event with
  `configure_project_foundation`. Call `get_project_market_contract`, then use
  `list_project_markets` and the first-class market write tools for markets.
  Market is a business/calendar boundary; locale is a content-variant axis.
  They are related but not interchangeable. Page size is not a project limit.
- Establish stable identity, consent and live factual ingestion. Historical
  Project Import is conditional and never replaces the live path.
- Define Type/Stage/Age from durable relationship, current condition and time
  since Type entry. Validate and compare in shadow before authority.
- Define stable business purposes, attention order, entry effects, manual-touch
  policy, cooldowns and caps before active journeys.
- Keep locale and channel variants on the same business `purpose_key`. Multiple
  Event Actions may be active for that key only when the impact gate proves
  their event predicates mutually exclusive. Never invent suffixed purposes to
  bypass an overlap; consolidate one owner or leave the fallback inactive.
- Build one end-to-end purpose in draft/shadow before expanding.

For every write: dry-run, show the tenant the exact diff or
`consequence_at_gate`, request confirmation, apply with the bound token, then
read back through MCP. Stop when facts, consent, precedence, copy claims or
collision choices are ambiguous.

Only a separately confirmed execution boundary such as
`schedule_campaign_run` may authorize future sends. Selection and Guardian
still recompute at dispatch.
