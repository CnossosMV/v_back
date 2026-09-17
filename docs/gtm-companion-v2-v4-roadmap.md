# Versya GTM Companion Roadmap: V2 To V4

Date: 2026-05-21

## Product Direction

Versya should not try to replace Google Tag Manager or require server-side GTM. The best product path is to become a GTM companion: Versya manages the shared event contract, captures identity and attribution, mirrors events between Versya and `dataLayer`, and forwards enriched server-side conversions to ad platforms.

The core promise:

> Keep your GTM setup. Versya learns your `dataLayer` events, enriches them, mirrors them server-side, and keeps browser/server events deduped with the same `event_id`.

## Shared Event Contract

V2+ should treat every customer event as a mapping between four layers:

- canonical Versya event name, for example `purchase`
- customer `dataLayer` event name, for example `billing_payment_success`
- provider event names, for example Meta `Purchase`, TikTok `Purchase`, GA4 `purchase`
- provider-specific settings, for example Google Ads conversion action ID

Example mapping:

| Layer | Value |
|---|---|
| Versya canonical event | `purchase` |
| GTM/dataLayer event | `billing_payment_success` |
| Meta | `Purchase` |
| TikTok | `Purchase` or `CompletePayment` depending customer setup |
| GA4 | `purchase` |
| Google Ads | conversion action ID per account |

The mapping is the product. The SDK and backend should execute that mapping in both directions.

## V2: GTM Companion Bridge

Goal: make Versya ingest existing GTM events with no extra instrumentation, and let Versya-created events trigger the customer GTM `dataLayer`.

### SDK Wrapper Mode

Customers with an existing GTM setup enable wrapper mode.

The Versya SDK wraps `window.dataLayer.push`, drains existing events already in `dataLayer`, and forwards mapped events to Versya ingest.

Flow:

```txt
window.dataLayer.push({ event: "billing_payment_success", ... })
  -> Versya SDK sees the event
  -> maps "billing_payment_success" to "purchase"
  -> normalizes value/currency/transaction_id/event_id
  -> enriches with cookies, click IDs, attribution, consent, URL, referrer
  -> sends canonical "purchase" to Versya ingest
  -> Versya dispatches server-side deliveries
```

Wrapper mode must not push the event back into `dataLayer`. It only listens and ingests.

### SDK Direct Mode

Customers who use Versya directly call:

```js
versya.track("purchase", {
  value: 718.8,
  currency: "BRL",
  order_id: "ORD-123"
});
```

Versya sends the event to ingest and mirrors it to `dataLayer` using the customer mapping:

```js
window.dataLayer.push({
  event: "billing_payment_success",
  event_id: "same-id",
  value: 718.8,
  currency: "BRL",
  transaction_id: "ORD-123"
});
```

Direct mode lets GTM browser tags keep working even when the customer instruments through Versya.

### Loop Safety

The SDK must prevent loops between wrapper mode and direct mode. Add internal bridge markers:

- `_vsya_origin`: `sdk`, `datalayer`, `server`, or `gtm_import`
- `_vsya_bridge_id`: random per SDK runtime
- `_vsya_event_id`: canonical event ID
- `_vsya_mirrored`: boolean marker for SDK-created `dataLayer` pushes

Rules:

- Events pushed by Versya into `dataLayer` are marked `_vsya_mirrored=true`.
- Wrapper mode ignores `_vsya_mirrored=true`.
- Wrapper mode forwards only configured events, not every arbitrary `dataLayer` object by default.
- Direct mode mirrors only when the event mapping says `mirror_to_datalayer=true`.

### Event ID Policy

Every mapped event needs one stable event ID.

Resolution order:

1. Existing `event_id`
2. Existing `eventID`
3. Existing `transaction_id` or `order_id` for purchase-like events
4. Generated ID: `{canonical_event}_{timestamp}_{random}`

The same resolved ID must be used for:

- Versya ingest
- `dataLayer` mirror
- Meta `event_id`
- TikTok `event_id`
- GA4 event parameter
- Google Ads `order_id` / transaction identifier when applicable

### Mapping Model Additions

Extend existing destination/event mapping models rather than creating a parallel GTM product model.

Add mapping fields such as:

- `canonical_event_name`
- `datalayer_event_name`
- `ingest_from_datalayer`
- `mirror_to_datalayer`
- `field_map`
- `required_fields`
- `default_values`
- `value_field`
- `currency_field`
- `transaction_id_field`
- `event_id_field`
- `dedupe_strategy`
- `enabled`

`field_map` should support simple transforms first:

- copy field
- fallback list
- constant default
- number parse
- currency default
- nested source path

### UI

Add a GTM Companion section inside Messaging/Destinations or Messaging/Domains.

Minimum V2 screens:

- mapped events table
- canonical event name
- `dataLayer` event name
- direction toggles: ingest from GTM, mirror to GTM
- provider mapping summary
- last seen event time
- recent payload sample
- event ID detected/generated status

### Debugging

Delivery debug should show the full chain:

```txt
dataLayer event seen
  -> canonical Versya event created
  -> attribution attached
  -> provider delivery queued
  -> provider response
```

For each event, show:

- raw `dataLayer` payload
- canonical payload
- normalized attribution
- event ID source
- field mapping warnings
- provider payload preview/response

### V2 Acceptance Criteria

- Existing GTM custom event can be imported as a Versya mapping.
- Existing `dataLayer.push` fires one Versya ingest call without customer code changes.
- Versya `track()` can mirror to the mapped GTM event.
- Browser GTM and Versya server-side deliveries share the same `event_id`.
- Loop prevention is verified with wrapper + direct mode enabled together.
- Tabloide purchase, trial, checkout, and export events work through the bridge.

## V3: Self-Serve GTM Contract Manager

Goal: make this sellable to customers who already have messy GTM containers.

### GTM JSON Import

Import a GTM container JSON export and parse:

- custom event triggers
- tag names
- known platform tags
- data layer variable names
- conversion labels and IDs where present
- paused/active state

Then suggest Versya mappings:

```txt
billing_payment_success -> purchase
trial_started -> trial_started
editor_export_success -> editor.export_success
lead_captured -> lead_captured
```

The importer should not auto-enable everything. It should create a review queue with confidence scores.

### GTM JSON Export

Export a GTM container patch or full JSON that creates:

- custom event triggers
- data layer variables
- optional browser tags
- event ID variables
- value/currency/transaction variables

Export should be deterministic so the customer can round-trip:

```txt
GTM export -> Versya import -> edit mappings -> GTM export
```

### Provider Mapping Assistant

For each canonical event, help customers map to:

- Meta standard event
- TikTok standard event
- GA4 recommended event
- Google Ads conversion action ID/label

The assistant should warn about common issues:

- purchase event has no value/currency
- Google Ads event has no conversion action ID
- Meta/TikTok event lacks email/phone/external ID
- source event lacks event ID
- event is being sent browser-side and server-side without dedupe

### Customer Install Modes

Offer three simple install modes:

- Observe only: listen to `dataLayer`, no provider forwarding.
- Mirror server-side: listen to `dataLayer`, send server-side conversions.
- Two-way bridge: listen to `dataLayer` and mirror Versya events back to GTM.

Avoid exposing low-level details first. Advanced options can sit behind an expandable panel.

### V3 Acceptance Criteria

- A customer can upload a GTM JSON export and get suggested mappings.
- A customer can create/edit the shared event contract in Versya UI.
- A customer can export GTM JSON that preserves event IDs and required variables.
- Debug UI can answer: "Did GTM fire this event, did Versya ingest it, and did each provider receive it?"
- Tabloide can maintain its GTM setup mostly through Versya mappings/export.

## V4: Optional First-Party Browser Tag Proxy

Goal: move closer to Stape-like browser proxying, without requiring sGTM.

V4 should be optional and higher risk. Build only after the GTM companion bridge is stable.

### GTM Loader Proxy

Customer can load GTM through the tracking domain:

```txt
https://track.customer.com/gtm.js?id=GTM-XXXX
```

Worker fetches:

```txt
https://www.googletagmanager.com/gtm.js?id=GTM-XXXX
```

and returns it while refreshing first-party cookies.

This keeps the customer on normal GTM but improves first-party routing for the GTM loader.

### Vendor Endpoint Proxy

Add opt-in endpoint proxying:

- `/meta/tr` -> `https://www.facebook.com/tr`
- `/meta/fbevents.js` -> `https://connect.facebook.net/.../fbevents.js`
- `/tiktok/events.js` -> `https://analytics.tiktok.com/...`
- `/ga/g/collect` -> `https://www.google-analytics.com/g/collect`
- `/gtm/gtm.js` -> `https://www.googletagmanager.com/gtm.js`

The Worker should:

- route by tracking hostname
- refresh first-party cookies
- forward vendor requests
- rewrite safe response headers
- avoid storing sensitive full request payloads by default
- sample proxy logs
- expose health in Versya UI

### Why V4 Is Later

Vendor endpoint proxying is powerful but brittle:

- vendor scripts can change
- CSP/CORS behavior can break
- provider terms and consent expectations must be checked
- request volumes are higher
- debugging is harder

This should not block the sellable V2/V3 product.

### V4 Acceptance Criteria

- GTM loader can be served from a tracking domain.
- Pixel/analytics endpoint proxying can be enabled per provider.
- Cookie keeper refreshes `_ga`, `_gcl_aw`, `_fbp`, `_fbc`, `_ttp`, and `_vsya_*`.
- Debug logs show proxied browser requests without exposing sensitive payloads by default.
- Customers can disable proxying without breaking the event bridge.

## Recommended Build Order

1. Harden V2 SDK bridge:
   - `dataLayer` listener
   - direct-mode mirror
   - loop safety
   - event ID policy
   - click/cookie enrichment

2. Add mapping persistence/UI:
   - canonical event to `dataLayer` event
   - field maps
   - direction toggles
   - provider summary

3. Improve debug:
   - raw event
   - canonical event
   - outbound provider payload
   - response
   - event/user deep link

4. Add GTM import/export:
   - import triggers/tags/variables
   - suggest mappings
   - export deterministic GTM JSON

5. Add optional V4 proxying:
   - GTM loader proxy first
   - then provider endpoint proxying

## Non-Goals

- Do not require server-side GTM.
- Do not make customers migrate away from GTM.
- Do not make provider endpoint proxying a dependency for server-side conversion forwarding.
- Do not auto-enable every imported GTM event.
- Do not create duplicate browser tags unless the customer explicitly exports/installs them.

## Commercial Positioning

Versya should be sold as:

> Server-side conversion tracking and event governance for teams already using GTM.

Not:

> A GTM replacement.

Not yet:

> A full Stape clone.

The immediate value is easier adoption, stronger attribution, dedupe-safe server-side mirroring, and a debug surface that explains what happened to each event.

## Installation Styles And Event Routing Model

Versya should support multiple customer setups without turning each setup into a separate product. Internally, all of them should resolve to the same routing model:

```txt
source node -> canonical Versya event -> destination nodes
```

The UI can present this as:

```txt
Connect event sources
Map events
Choose destinations
Debug deliveries
```

### Customer Type 1: GTM Users

These customers already use:

```txt
dataLayer -> GTM -> Meta/TikTok/Google/browser tags
```

Versya should add:

- `dataLayer` listener
- GTM JSON import/export
- canonical event mapping
- server-side mirror
- `event_id` dedupe
- attribution and cookie enrichment
- delivery/debug UI

This is the easiest adoption path and should be the default go-to-market motion.

### Customer Type 2: Direct Pixel Users

These customers manually inject calls such as:

```js
fbq("track", "Purchase", payload);
ttq.track("Purchase", payload);
gtag("event", "conversion", payload);
```

Versya should support them in two ways:

1. Encourage canonical instrumentation:

```js
versya.track("purchase", payload);
```

2. Optionally provide compatibility wrappers later:

```js
versya.fbq("track", "Purchase", payload);
versya.ttq.track("Purchase", payload);
versya.gtag("event", "conversion", payload);
```

The canonical `versya.track()` path is preferred because it gives Versya clearer control over mapping, dedupe, consent, and server-side delivery.

### Customer Type 3: Versya As Full Wrapper

These customers want Versya to own the event router:

```txt
Customer app -> versya.track() -> browser destinations + server destinations
```

Versya decides, based on configuration:

- whether to push to `dataLayer`
- whether to call browser provider pixels
- whether to send server-side provider events
- which consent rules apply
- which `event_id` is used
- which provider payloads are built

This is the strongest long-term product experience, but it requires more customer trust. It should be offered after the GTM companion flow is stable.

## Node-Based Routing

Versya should model sources and destinations as nodes.

### Source Nodes

Official source nodes:

- Versya SDK
- `dataLayer` / GTM listener
- backend API
- webhook/API event import
- manual/test event

Later source nodes:

- provider callbacks
- ecommerce platform webhooks
- CRM lifecycle events

### Destination Nodes

Official destination nodes:

- GTM `dataLayer`
- Meta CAPI
- TikTok Events API
- GA4 Measurement Protocol
- Google Ads conversions
- generic webhook/custom HTTP

Later official or community nodes:

- Meta browser pixel
- TikTok browser pixel
- Google browser `gtag`
- LinkedIn Conversions API
- Pinterest Conversions API
- Reddit Ads Conversions API
- X/Twitter Conversion API
- Microsoft Ads
- custom customer scripts

## Generic And Community Nodes

The first sellable version should ship official nodes for the major ad platforms and a generic webhook/custom HTTP node.

Community or customer-defined nodes can come later, but they need guardrails:

- no raw PII access by default
- explicit field allowlist
- explicit secret access controls
- clear consent requirements
- dry-run/test mode
- payload preview before activation
- per-node error and retry logs

The generic node should support:

- URL
- HTTP method
- headers
- auth secret reference
- JSON body template
- field mapping from canonical event
- retry policy
- success/error response matching

This gives advanced customers flexibility without forcing Versya to officially maintain every long-tail provider on day one.
