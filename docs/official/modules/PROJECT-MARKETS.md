# Project Markets

> Canonical multi-market configuration for one Versya project.

## Boundary

A project can contain any practical number of markets. A market is a stable
commercial and calendar boundary, not a language. Locale remains a
content-variant axis, so one market may support several locales and one locale
may be reused across markets.

`project_markets` is canonical. Each row stores a stable key, country, optional
regions, IANA timezone, locales, calendar tags, status and whether it is the
project fallback. `projects.market_config` remains a synchronized compatibility
projection for older clients.

There is no business limit of 50 markets. `list_project_markets` uses a bounded
page size and a stable-key cursor so payload size does not become a project
limit.

## Lifecycle and safety

- Stable keys are immutable and must never be recycled.
- Markets have `active` or `archived` status; archive is recoverable.
- Exactly one active market is the default whenever active markets exist.
- When another active market exists, the default must move before that market
  can be archived. Archiving the sole active market leaves setup incomplete.
- Creating or changing a market does not reclassify contacts, activate
  automations, connect channels or authorize sends.

Module-specific behavior belongs to its owning configuration. Guardian,
channels, domains, destinations and campaigns may reference a market key, but
arbitrary override documents are not stored on the market itself.

## Tenant MCP

1. `get_project_market_contract`
2. Paginate `list_project_markets` until `has_more=false`.
3. Dry-run and confirm `create_project_market` or `update_project_market`.
4. Use `set_default_project_market` for the fallback.
5. Use `set_project_market_status` to archive or restore.

Every write has a payload-bound confirmation token and
`consequence_at_gate.external_sends=0`.
