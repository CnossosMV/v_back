# Project Setup Plan runbook

The Project Setup Plan is shared memory between the tenant and its agents. It
stores the proposal, evidence-labeled decisions, phase gates and approvals so a
later session can continue without reconstructing business intent from chat.

## Suggested interview

Ask only questions that change configuration:

- What first business outcome matters, and which observable event proves it?
- Which system owns stable identity, current source facts and consent evidence?
- Which durable commercial relationships and current conditions matter?
- What starts the relationship clock and when does age change treatment?
- Which countries/regions, timezones, languages and channels are approved?
- When communications compete, which outcome wins and what happens below it?
- How should a recent manual commercial touch affect scheduled marketing?
- What may an agent draft, and who may activate or authorize a send?

Translate answers into tool payloads yourself. Proposed defaults remain
`recommended` until the tenant decides.

## MCP sequence

1. `assess_project_setup`
2. `create_project_setup_plan(dry_run=true)`
3. Repeat with `dry_run=false`, review the pending action, then confirm.
4. Record each decision through the same three-step gate.
5. Read the plan and phase gates. Resolve every blocker and required tenant
   decision.
6. Approve one phase at its current fingerprint.
7. Execute only that phase with the specific module tool and its independent
   gate.
8. Read back state, update decisions if evidence changed, and continue.

An accepted decision must use `evidence_kind=tenant_decided`. Observations,
inferences and recommendations may be recorded as `proposed` or `rejected`, but
cannot masquerade as tenant approval.

## Foundation semantics

`project_markets` is the canonical market inventory. Call
`get_project_market_contract`, paginate `list_project_markets`, and use the
dedicated create/update/default/status tools. `projects.market_config` is a
compatibility projection, not the write surface for new agents. Use IANA
timezone names and two-letter country codes. `default_locale` must be in
`supported_locales`, and every market locale must be supported by the project.
Do not confuse the maximum page size with a maximum number of project markets.

`goal_event` is a stable factual event key that proves the project's primary
outcome. It is not a sentence, campaign goal, channel metric or inferred state.

## Safety and continuity

Any decision revision changes the plan fingerprint. An earlier approval then
appears stale and must be reviewed against the new facts. This is deliberate.
Approvals never cascade into configuration, activation or sends.

If a required action lacks a tenant-facing MCP/public capability, add it to
`product_gaps` and stop that phase. Do not bypass the boundary.
