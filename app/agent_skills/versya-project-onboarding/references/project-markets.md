# Project markets

A Versya project is multi-market. Do not propose one project per country merely
to change language, timezone, commercial calendar, sender or Ads destination.

## Semantics

- A market is a commercial/calendar boundary with a stable lowercase key,
  country, optional regions, IANA timezone, locales and calendar tags.
- Locale is a content-variant axis. One market may support several locales and
  the same locale may appear in several markets.
- The project defaults are fallbacks when a contact or operation has not yet
  resolved a more specific market, locale or timezone.
- Market keys are immutable. Archive and restore a market instead of deleting
  or recycling its identity.
- One active market is the fallback default. If another active market exists,
  change the default before archiving it. Archiving the sole active market
  deliberately returns the foundation to an incomplete state.

## Agent workflow

1. Ask which countries/regions are commercial boundaries and what makes their
   calendars, policies, channels or reporting meaningfully different.
2. Call `get_project_market_contract` and paginate `list_project_markets` until
   `has_more=false`.
3. Record recommended markets in the Project Setup Plan. A recommendation is
   not a tenant decision.
4. Create or update one market at a time through dry-run, show
   `consequence_at_gate`, obtain confirmation and read the market back.
5. Set the explicit fallback default. Never infer that the first locale is the
   default market without tenant review.
6. Configure module-owned market behavior in the owning channel, Guardian,
   domain, destination or campaign contract. Do not place arbitrary override
   JSON on the market itself.

Creating or changing a market does not reclassify contacts, activate an
automation, connect a provider or authorize an external send.
