# Lifecycle and orchestration — agent contract

Versya separates classification from messaging ownership. A Lifecycle Model computes where a contact is; `purpose_key` identifies why an automation may act. Import, lifecycle activation and purpose cutover are independent approvals.

## Lifecycle vocabulary

- **Lifecycle**: a versioned, project-scoped deterministic classifier that gives every active non-sandbox contact one Position `(Type, Stage, Age)`.
- **Type**: a durable commercial relationship. A Type change resets the Type-entry clock.
- **Stage**: one current, mutually exclusive operational condition inside the selected Type.
- **Age**: a bucket of time since entering Type. It is not time since Stage entry, last activity or last message.
- **Position**: the materialized Type, Stage and Age for one contact/model, with entry timestamps, `computed_at`, provenance and matched-rule explanation.
- **Distribution**: coverage and counts by Type, Type/Stage and Type/Stage/Age, including missing Positions.
- **Explanation per contact**: stored Position versus a fresh calculation, first matching Type/Stage rules, priorities, conditions and observed rule evidence.

The axes, first-match semantics, Type-entry clock, model statuses and cutover modes are platform-canonical. Keys such as `customer`, `prospect`, `trial_new` and `payment_issue` are project taxonomy. They are a recommended baseline, not a global Versya enum.

For a simple subscription product, a useful approximation is:

| Type | Typical Stages |
|---|---|
| `prospect` | `lead_captured`, `registered`, `trial_new`, `trial_activated`, `trial_expiring`, `trial_exhausted`, `trial_expired` |
| `customer` | `active`, `renewal_due`, `payment_issue` |
| `former_customer` | `canceled`, `expired` |
| `unknown` | `unclassified` |

Rules are ordered and first-match-wins. Put stronger, more specific evidence first and one catch-all last. If identity reconciliation combines several source records for one real person/account, use the strongest authoritative commercial evidence unless the tenant declares another policy. For example, an active paid subscription can reasonably outrank a free registration summary.

Do not infer a paid relationship from a generic flag named `active`. Separate row validity from commercial evidence: paid normally requires a positive-price, non-trial entitlement or a positive completed payment; a valid free/trial entitlement remains a prospect condition. Confirm whether the source models commerce per contact, account or workspace before exporting facts.

## Tenant interview and model creation

A tenant does not need to know the JSON contract. Its agent must call `get_lifecycle_model_contract`, ask:

1. Which facts are authoritative and how will they stay current?
2. Which durable relationships should be Types?
3. Which operational conditions should be Stages inside each Type?
4. Which condition wins when facts conflict?
5. What are the catch-alls?
6. Which moment starts Type age and which buckets are useful?

The agent generates the definition, calls `validate_lifecycle_model_definition`, then `create_lifecycle_model(dry_run=true)`. Persistence requires a checksum-bound confirmation. An identical checksum reuses the existing model. Creation does not activate the model and sends nothing.

After creation, inspect `get_lifecycle_distribution`, `compare_lifecycle_materialization` and selected `explain_contact_lifecycle` results. `activate_lifecycle_model(... target_status="shadow")` materializes Positions with no transition events and no messages. Only a reviewed shadow model can later become active.

## Purpose ownership

`purpose_key` is a stable project-scoped business intent and ownership key, for example `trial.activation`, `checkout.recovery` or `renewal.reminder`. It is not a channel, locale, template, campaign ID or source implementation. All variants serving one objective share one key.

More than one active Event Action may share that key only when the activation gate can prove their predicates are mutually exclusive, such as `lang == pt` versus `lang == en` on the same event. Uncertain or overlapping definitions remain blocked as competing decision owners. Do not mint channel- or locale-suffixed purpose keys to bypass the gate: consolidate one owner, make the predicates provably disjoint, or keep the alternative inactive until an explicit fallback design exists.

Before authoring automation, define the purpose brief: objective/success event, trigger and lifecycle audience, stop conditions, authoritative facts, transactional or promotional lane, permission requirement, cooldown/caps, channels and locale/template variants.

Use `list_orchestration_purposes` to find assets without a key and `assess_orchestration_readiness` before a transfer. Modes are:

- `legacy`: source decides; purpose-aware Versya execution is blocked.
- `shadow`: source still decides; Versya remains blocked while outputs are compared.
- `versya`: Versya decides; the source emits facts only.

Every change increments the orchestration epoch. Mirror the new mode and epoch to the source. Rollback is another forward epoch change.

## Outbound source and activation gate

Every outbound `source_type` must be registered in the platform contract. The registration declares its lane, whether it is automated, whether it participates in Candidate+Selection, the authored definition kind and any deliberate exemption. Unknown source strings fail before provider I/O. A promotional automation also fails closed unless its `source_id` resolves to an enabled project definition with `purpose_key` and `attention_policy`, and Candidate+Selection are both in `enforce`.

The registered attention sources are campaigns, funnels, decision-producing Event Actions and event-triggered templates. Conversational replies and manual/test sends are explicit non-promotional exemptions. Deferred sends and retries retain the original durable attention identity and use the same contact/scope lock.

For templates, `is_active` means the content can be selected or rendered; `automation_enabled` means `trigger_events` may execute it. Creation, import and generic update always leave automation disabled. Author `trigger_events`, purpose and attention policy while disabled; call `preview_orchestration_impact(asset_kind="template")`; then call `activate_template_automation` through its dry-run and confirmation flow. Disable before editing an active template automation.

Campaigns, funnels, Event Actions and triggered templates use the same exact-impact activation gate. The preview is send-free and returns blockers, materialized consequences and a definition-bound fingerprint. MCP confirmation is bound to that fingerprint and current definition. Activation itself creates no campaign run and performs no external send.

Every preview also returns `consequence_at_gate`. This is the tenant-facing interpretation of the rule: which definitions and currently materialized contacts remain active, become occluded, are rejected, are suspended, or are exited/canceled. The consequence participates in the impact fingerprint. A changed definition, policy, materialized count or consequence invalidates the prior confirmation.

## Bounded future attention

Selection reasons over both due intents and known future intents. The project owns four bounded controls: planning horizon, collision window, recheck interval and maximum candidates per contact. An individual policy owns the semantic opt-in `future_reservation`. A future intent without that opt-in is visible in previews but cannot hold a due intent.

`future_plan` has three operational modes:

- `off`: only due-time arbitration affects dispatch;
- `shadow`: compute and persist who would be held, while preserving due-only dispatch;
- `enforce`: a higher-ranked opted-in future intent may hold a lower-ranked due intent within the collision window.

The plan is never provider authorization. Every hold is temporary and bounded by the configured horizon/window, candidate expiry and recheck interval. At the dispatch deadline, Versya locks the contact plus attention scope, reloads due and future contenders, and computes the binding winner again. Guardian then independently rechecks consent, opt-out, quiet hours, cooldowns and pressure caps. Facts that were genuinely unknown before attention was consumed cannot retroactively change that past decision.

Use `get_attention_planning_contract` before setting policy, `configure_attention_planning` through dry-run and consequence-bound confirmation, and `preview_contact_attention_plan` for representative contacts. The same explanation is stored on pending send intents through `attention_policy_snapshot`, `planning_status` and `planning_snapshot`.

For recurring lifecycle nurture, bind the audience to the reviewed model and
follow [Campaign recipes](CAMPAIGN-RECIPES.md). Recipe/Episode planning is not a
lifecycle transition and cannot authorize a send.
