# Campaign recipes — MCP agent contract

A campaign recipe is Versya's persistent answer to “what should we prepare
when the tenant does not author a new campaign this week?”. It is planning
policy, not executable messaging.

## Concept boundaries

- **Base** is standing Type/Stage/Age nurture and remains eligible whenever no
  higher episode owns attention.
- **Recipe** binds one `lifecycle_model_id`, rule-based audience, `purpose_key`,
  country/region/timezone, calendar rules, attention policy and copy policy.
- **Episode** is one dated opportunity generated inside a bounded horizon. It
  freezes the opportunity, collisions, content brief and editorial deadline.
- **Campaign** is an inactive executable draft materialized from an Episode.
- **Run** is the separately approved delivery occurrence. Only confirmed run
  scheduling authorizes future external sends.

The recipe worker runs every five minutes. It creates Episode records and may
create inactive drafts only for explicitly automatic fixed content. It never
activates campaigns, creates runs, calls a provider or sends.

## Tenant interview

Do not ask a novice tenant for JSON. Ask for:

1. the business objective and success/stop facts (`purpose_key` follows from
   this brief, not from channel or template names);
2. the lifecycle audience and reviewed lifecycle model;
3. country, optional region and timezone;
4. recurring windows and explicit order when they overlap;
5. attention entry/fallthrough behavior and pressure limits;
6. copy mode, source facts, allowed/forbidden claims, CTA, locales and review
   requirement;
7. planning horizon and how far ahead copy should be ready.

## Deterministic MCP sequence

1. Resolve and always pass `project_id`; call `get_campaign_recipe_contract`.
2. Interview the tenant, then call `preview_campaign_recipe`. Present every
   occurrence, collision, required copy item and `consequence_at_gate`.
3. Call `create_draft_campaign_recipe` as dry-run, request confirmation and
   repeat with the returned token.
4. Activate recipe planning with the same dry-run/pending/confirm sequence in
   `set_campaign_recipe_state`. This still reports `external_sends=0`.
5. Poll `get_campaign_planning_inbox`. Never invent a collision winner. For
   agent-authored modes, write only from the frozen brief and source facts.
6. Call `materialize_campaign_episode`; it creates an inactive campaign draft.
7. Call `preview_campaign_plan` and `preview_orchestration_impact`, resolve all
   blockers, then confirm `activate_campaign`.
8. Call `evaluate_campaign_run_consequences`. Present the options, call
   `choose_campaign_run_consequence`, and stop if the tenant chooses
   `do_nothing`.
9. Call `schedule_campaign_run` through dry-run and confirmation. State clearly
   that confirmation authorizes the reported future external sends.
10. Monitor with `get_campaign_run_status`. Use `cancel_campaign_run` through
    preview and confirmation when the business decision changes; already
    submitted provider calls cannot be recalled.

## Copy modes and unattended behavior

- `fixed`: reuse approved actions; optional automatic materialization creates
  drafts only.
- `agent_draft`: leave an inbox item until an agent writes and a human approves
  the episode.
- `bounded_autonomy`: an agent may vary copy only within declared source facts,
  claims, tone and locale. Backend dispatch never improvises copy.

An unresolved Episode is blocked. A missed Episode expires. In both cases no
run is created and Base remains eligible. Pausing or archiving a Recipe is
always available and does not depend on activation validation.

## Required scopes

Read work needs `versya.projects:read`, `versya.campaigns:read` and, for contact
explanations, `versya.contacts:read`. Recipe/campaign writes, consequence choice,
run scheduling and cancellation need `versya.campaigns:write`; activation,
scheduling and cancellation require project admin. OAuth scope, active connector
grant and project membership all have to allow the call.

No step requires SSH, SQL, a provider console or an unpublished API.
