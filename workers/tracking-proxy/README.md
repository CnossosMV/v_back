# Versya Tracking Proxy Worker

V1 proxies only Versya SDK and ingest endpoints through a customer-controlled tracking hostname. It does not proxy Meta, TikTok, GTM, GA4, or Google Ads browser endpoints yet; that remains the V2 Stape-parity layer.

Required Worker bindings:

- `TENANT_CONFIG_KV`: optional-but-recommended cache for hostname edge configs.
- `VERSYA_API_ORIGIN`: Versya backend origin, for example `https://api.versya.app`.
- `EDGE_CONFIG_SECRET`: Worker secret matching backend `VERSYA_EDGE_CONFIG_SECRET`.
- `PROXY_VERSION`: visible in forwarded headers and proxy logs.

Main behavior:

- Looks up tenant config by `Host`.
- Serves `/sdk/versya-messaging.js` from the configured SDK/API origin.
- Forwards `/track`, `/page`, `/identify`, and `/api/v1/...` SDK endpoints to Versya.
- Captures click IDs from query params and existing cookies.
- Refreshes first-party tracking cookies with `Set-Cookie`.
- Enriches JSON ingest payloads with attribution/cookie context.
- Posts sampled proxy logs to `/api/v1/internal/tracking-domains/proxy-log`.
