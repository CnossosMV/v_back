# Versya Ads Gateway / Stape-Equivalent Audit

Last updated: 2026-05-18

This note captures where Versya stands after implementing the server-side ads gateway dry-run setup for Tabloid, and what to plan next after validating dry-run deliveries.

## Current Position

Versya is currently a good server-side tracking MVP for Tabloid dogfooding. It can ingest canonical events, normalize attribution, queue provider deliveries, build platform-specific payloads, and expose delivery/debug rows.

It is not yet fully Stape-equivalent. The main missing Stape-class pieces are the first-party tracking proxy, cookie keeper, and multi-tenant tracking subdomain onboarding.

## Dry-Run State

Configured or ready for dry-run:

- GA4 Measurement Protocol
- Meta Conversions API
- TikTok Events API
- Google Ads basic destination config

Google Ads is currently only ready for dry-run/basic account setup. Real delivery still needs developer token, OAuth credentials, refresh/access token flow, and conversion action IDs per mapping.

## Backend/CAPI Readiness Notes

Tabloid frontend and backend are now mostly ready to provide the fields Versya needs for backend/CAPI dry-run payloads, especially for the current dogfood flow.

Tabloid's frontend GTM path was also tightened so browser pixels and Versya receive a more consistent event shape. `Analytics.track`/GTM now normalize and mirror:

- `event_id` / `eventID`
- `value`, `payment_amount`, and `conversion_value`
- `currency`, defaulting to `BRL` when missing
- `transaction_id` and `order_id`
- `content_id` / `content_ids` where a design or plan ID exists
- `external_id`, `email`, and phone aliases when the user has been identified

This means Meta/TikTok/Google browser tags should have the same dedupe ID and stronger value/currency fields, and Versya should have enough raw material to build server-side CAPI payloads in dry-run.

Fields currently captured or propagated for frontend-originated events:

- `event_id`
- `campaign_origin`
- `utm_*`
- `gclid`, `gbraid`, `wbraid`
- `fbclid`
- `ttclid`
- `li_fat_id`
- `msclkid`
- `_fbp`, `_fbc`
- `_ga`, `_gcl_aw`, `_gcl_gb`
- `_ttp`, `ttp`
- page URL, referrer, landing page
- user agent and IP address on Versya ingest
- email, phone, and external/user ID when the user is identified
- numeric `value`, `currency`, `transaction_id`, and `order_id` for conversion events

Backend-originated purchase events now send canonical `purchase` to Versya with:

- stable `event_id`
- transaction/order IDs
- `value`
- `currency`
- user identity

When backend events arrive without browser context, Versya can rely on stored first/last-touch attribution from earlier frontend events for the same user or anonymous profile. This requires at least one prior frontend event to have reached Versya after the ad click.

Remaining gaps before calling CAPI forwarding fully production-grade:

- Derive `_fbc` from `fbclid` when `_fbc` is missing, using Meta's expected shape: `fb.1.{timestamp}.{fbclid}`.
- Phone availability is only as good as Tabloid's collected profile data. Email is usually present after auth; phone may be absent for signup/trial users.
- Backend purchase attribution depends on previous browser attribution capture. Direct backend-only users with no prior Versya browser event may lack click IDs.
- Address/name match fields are not yet sent: first name, last name, city, state, zip, country. These can improve EMQ but are not required for dry-run.
- Generic Versya SDK still needs first-class event_id generation, dataLayer mirroring, and durable click-ID cookie persistence. Tabloid's app-specific frontend path is currently stronger than the generic SDK.
- Browser pixel EMQ warnings can still appear if the user is anonymous or Tabloid does not know email/phone at the moment the GTM tag fires. Server-side CAPI can improve this later by enriching from stored user/profile data, but it cannot invent phone/address fields that were never collected.

For Tabloid dogfooding, this is enough to test dry-run payload quality. Before disabling dry-run for paid traffic broadly, prioritize `_fbc` derivation and richer identity normalization.

## Audit

| Area | Status | Notes |
|---|---:|---|
| Server-side event distribution | Partial | Wired: Meta CAPI, TikTok Events API, GA4 Measurement Protocol, Google Ads click conversion upload/OCI-style. Missing: LinkedIn CAPI, X/Twitter, Pinterest, Reddit. Google Ads Enhanced Conversions for Leads is not fully implemented. |
| User identity payload / Event Match Quality | Partial | Sends hashed email, phone, external ID, IP, UA, `_fbp`, `_fbc`, `ttclid`, `ttp`, and Google click IDs when available. Missing: deriving `_fbc` from `fbclid`, address fields, more click IDs, and platform-specific normalization audit. |
| Event deduplication | Partial/good for Tabloid | Tabloid frontend sends the same `event_id` to Versya and GTM. Backend purchase emits stable `event_id`. Generic Versya SDK still needs first-class event_id generation and dataLayer mirroring. |
| First-party tracking subdomain proxy | Not yet | No Cloudflare Worker/reverse proxy for Meta, TikTok, GTM, GA4, or ad endpoint proxying. This is the biggest Stape gap. |
| Cookie Keeper | Not yet | No proxy middleware that refreshes `_fbp`, `_fbc`, `_ga`, `_ttp`, etc. using HTTP `Set-Cookie`. |
| Click ID capture and persistence | Partial | Captures `gclid`, `gbraid`, `wbraid`, `fbclid`, `ttclid`, `li_fat_id`, `msclkid`. Missing `twclid`, `epik`. Tabloid persists attribution in localStorage; generic SDK captures current attribution but should persist click IDs as durable first-party cookies. |
| Consent mode integration | Partial | Versya has marketing/analytics consent checks and explicit skipped deliveries. Missing Google Consent Mode v2 fields: `analytics_storage`, `ad_storage`, `ad_user_data`, `ad_personalization`, and denied-mode forwarding behavior. |
| Real-time debugging UI | Partial | Destinations page has mapping status and server-side delivery debug. Needs richer inbound payload, outbound payload, response body, filtering, and per-user/event stream inspection. |
| Multi-tenant subdomain onboarding | Not yet | Messaging domains exist for SDK setup, but not Stape-style tracking subdomain verification, SSL provisioning, tenant edge routing, proxy cookie scoping, and per-tenant proxy logs. |
| Frontend SDK behavior | Partial | SDK writes `_vsya_vid`, has `identify()`, dataLayer listener support, consent API, and reads ad cookies/click IDs. Missing 2-year cookie expiry, event_id per event, automatic dataLayer mirror with event_id, click-ID cookie persistence, and clearer direct/wrapper mode UX. |

## Code Anchors

- `app/services/messaging/destination_dispatcher.py`: provider payload builders, queueing, retries, dry-run behavior, consent skips.
- `app/services/messaging/event_attribution.py`: attribution normalization, click/cookie capture, campaign origin.
- `app/sdk/versya-messaging.js`: generic SDK cookies, attribution capture, dataLayer listener, identify, consent.
- `app/routers/messaging/event_mappings.py`: event mapping CRUD and default ad mapping seed endpoint.
- `app/routers/messaging/destinations.py`: destination config CRUD and delivery debug APIs.
- Tabloid frontend: `src/services/analyticsTracker.ts` and `src/utils/marketingAttribution.ts`.
- Tabloid backend: `app/services/versya_service.py` and payment completion emission path.

## What To Validate In Dry-Run

For each destination, trigger Tabloid events and inspect Versya delivery debug.

Events to validate:

- `trial_started`
- `billing.checkout_started`
- `purchase`

Expected dry-run delivery status:

- `skipped`
- error/reason: `Dry run enabled; provider call not sent`

Expected payload/debug hints:

- Shared `event_id`
- `campaign_origin`
- captured `gclid`, `gbraid`, `wbraid`, `fbclid`, `ttclid` when present
- `_fbp`, `_fbc`, `_ga`, `_ttp` when present
- IP and user agent
- value/currency/order ID for purchase

Provider-specific checks:

- Meta: `event_id`, `event_name`, `event_time`, `event_source_url`, `action_source=website`, `fbp/fbc`, hashed identifiers if available.
- GA4: `client_id` or `user_id`, `session_id` if available, event params, transaction/value/currency.
- Google Ads: `gclid`/`gbraid`/`wbraid`, customer ID, conversion action ID placeholder or configured mapping, conversion time, value/currency/order ID.
- TikTok: `event_id`, `event`, `ttclid`, `ttp`, IP/UA, hashed identifiers, value/currency/order ID.

## Recommended Next Plan After Dry-Run

1. Improve identity payload completeness.
   - Derive `_fbc` from `fbclid` as `fb.1.{timestamp}.{fbclid}` when `_fbc` is missing.
   - Add first name, last name, city, state, zip, country hashing where profile data exists.
   - Add `twclid` and `epik` capture.
   - Audit platform-specific hashing/normalization rules.

2. Make generic SDK dedupe first-class.
   - Generate `event_id` per SDK `track()`.
   - Include `event_id` in Versya ingest payload.
   - Mirror event to `window.dataLayer` with the same `event_id` when direct mode is enabled.
   - Avoid loops when wrapper/listener mode is enabled.

3. Persist click IDs in the SDK.
   - Store click IDs in first-party JS cookies as a fallback before proxy exists.
   - Use 90-180 day expiry.
   - Include stored click IDs on later events even when the URL no longer has them.

4. Improve debug UI.
   - Add expandable delivery details with inbound event, normalized attribution, outbound payload, response body, and retry info.
   - Add filters by event name, destination, status, user/anonymous ID, and time range.

5. Finish Google Ads real sending.
   - Add OAuth/developer token setup docs/UI.
   - Add conversion action ID mapping UX polish.
   - Decide whether to implement Enhanced Conversions for Leads separately from click conversion upload.

6. Add Tier 2 adapters when needed.
   - LinkedIn Conversions API first, because `li_fat_id` is already captured.
   - Then Pinterest, Reddit, X based on customer demand.

7. Plan Stape-equivalent infrastructure.
   - Cloudflare Worker reverse proxy.
   - Customer tracking subdomain onboarding.
   - DNS verification and SSL provisioning.
   - Cookie keeper middleware.
   - Tenant routing and proxy logs.

8. Consent Mode v2.
   - Store Google consent signals.
   - Forward Google events in denied mode with limited identifiers where required.
   - Keep explicit skip logs for non-Google providers when consent is missing.

## Product Takeaway

Versya + GTM is enough for Tabloid dry-run and likely enough for useful dogfooding once real sends are enabled. It should improve reliability and backend conversion truth for Meta, TikTok, GA4, and Google Ads.

Versya is not yet a full Stape replacement because Stape's strongest feature is not only server-side API forwarding. It is the first-party proxy plus cookie persistence layer. That should be treated as the next major product/infrastructure phase after the dry-run proves event quality.
