# Versya Stape-Like Sellable Foundation Decisions

Date: 2026-05-19

## Product Direction

Versya should be built as a sellable first-party server-side tracking layer, with Tabloide.pro as the first tenant. Dogfooding should validate the product path, but implementation should avoid Tabloide-only assumptions.

## Cloudflare Decision

V1 uses Versya-managed Cloudflare for SaaS / Custom Hostnames.

Customers do not connect their own Cloudflare account in V1. The customer-facing Versya UI asks them to add a tracking hostname, usually `track.customer.com`, then shows the DNS records to create and the resulting DNS, SSL, proxy, and cookie-keeper status.

Operational credentials stay in Versya infrastructure:

- `CLOUDFLARE_API_TOKEN` or `CF_API_TOKEN`
- `CLOUDFLARE_ZONE_ID` or `CF_ZONE_ID`
- `VERSYA_TRACKING_CNAME_TARGET`
- `VERSYA_EDGE_CONFIG_SECRET`

Do not store these secrets in docs, project settings, frontend state, or customer-visible fields.

## V1 Scope

V1 proxies only Versya-owned traffic through the first-party tracking hostname:

- `/sdk/versya-messaging.js`
- `/api/v1/track`
- `/api/v1/page`
- `/api/v1/identify`
- related SDK endpoints such as verify-install, consent, alias, visitor-data, recover-identity, and config

The Worker captures click IDs, refreshes first-party cookies, enriches Versya event payloads with attribution context, and writes lightweight proxy logs back to Versya.

## Deferred To V2

Provider endpoint proxying is intentionally not part of V1. V2 can evaluate proxying:

- `connect.facebook.net`
- `www.facebook.com/tr`
- `analytics.tiktok.com`
- `www.googletagmanager.com`
- `www.google-analytics.com`
- `region1.google-analytics.com`

This is closer to Stape parity, but it is more brittle and creates a larger maintenance/security surface.

Customer-owned Cloudflare connection is also deferred. It may become an advanced option later, but V1 avoids requiring customers to authorize Cloudflare from Versya.

## Open Decisions For Later

- Whether Cloudflare for SaaS hostname costs should be bundled, metered per hostname, or exposed as an add-on.
- Whether proxy request logs should be sampled by default for high-volume customers.
- Whether edge tenant config should move from backend fallback plus KV cache to a push-only KV deployment workflow.
- Whether customers need a self-serve custom domain health page outside the main Destinations/Domains UI.
- Whether V2 provider proxying should be offered only to higher plans because of operational risk.
- Whether to add an internal admin UI for Cloudflare credentials and provisioning diagnostics.

## Rollout Gate

Keep provider destinations in dry-run until Tabloide confirms:

- first-party tracking hostname resolves and serves the SDK,
- `_vsya_*`, `_fbp`, `_fbc`, `_ga`, and `_ttp` cookies are refreshed where available,
- `trial_started`, `begin_checkout`, and `purchase` preserve the same `event_id` in GTM and Versya,
- Google Ads payloads include `gclid`, `gbraid`, or `wbraid`,
- Meta/TikTok payloads include hashed identity when the user is known.

## Reference Docs

- Cloudflare Workers custom domains: https://developers.cloudflare.com/workers/configuration/routing/custom-domains
- Cloudflare for SaaS plans: https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/plans/
- Cloudflare custom domains with SSL for SaaS: https://developers.cloudflare.com/use-cases/saas/custom-domains/
