"""Validation and diffing for tenant-visible project foundation settings."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,119}$")
_MARKET_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_ALLOWED_SETTINGS = {
    "default_locale",
    "supported_locales",
    "default_timezone",
    "goal_event",
    "market_config",
}


def _timezone(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 64:
        raise ValueError(f"{field} must contain a valid IANA timezone")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"{field} must contain a valid IANA timezone") from exc
    return normalized


def _locale(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not _LOCALE_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a BCP-47-like locale such as pt-BR or en")
    return normalized


def _string_list(value: Any, field: str, maximum: int = 50) -> List[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    if len(value) > maximum:
        raise ValueError(f"{field} may contain at most {maximum} values")
    result: List[str] = []
    for item in value:
        normalized = _locale(item, field)
        if normalized not in result:
            result.append(normalized)
    return result


def normalize_market_config(value: Any, supported_locales: List[str]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("market_config must be an object")
    unknown = set(value) - {"default_market_key", "markets"}
    if unknown:
        raise ValueError(f"market_config contains unsupported fields: {sorted(unknown)}")
    markets = value.get("markets")
    if not isinstance(markets, list) or not markets:
        raise ValueError("market_config.markets must contain at least one market")

    normalized_markets = []
    keys = set()
    for index, market in enumerate(markets):
        if not isinstance(market, dict):
            raise ValueError(f"market_config.markets[{index}] must be an object")
        unknown_market = set(market) - {
            "key", "country_code", "region_codes", "timezone", "locales", "calendar_tags"
        }
        if unknown_market:
            raise ValueError(
                f"market_config.markets[{index}] contains unsupported fields: {sorted(unknown_market)}"
            )
        key = str(market.get("key") or "").strip().lower()
        if not _MARKET_KEY_RE.fullmatch(key) or key in keys:
            raise ValueError("Every market requires a unique stable lowercase key")
        keys.add(key)
        country = str(market.get("country_code") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError(f"Market {key} requires a two-letter country_code")
        timezone = _timezone(market.get("timezone"), f"market {key} timezone")
        locales = _string_list(market.get("locales"), f"market {key} locales")
        unsupported = [locale for locale in locales if locale not in supported_locales]
        if unsupported:
            raise ValueError(f"Market {key} references unsupported project locales: {unsupported}")
        region_codes = []
        for region in market.get("region_codes") or []:
            normalized_region = str(region).strip().upper()
            if not normalized_region or len(normalized_region) > 80:
                raise ValueError(f"Market {key} has an invalid region code")
            if normalized_region not in region_codes:
                region_codes.append(normalized_region)
        calendar_tags = []
        for tag in market.get("calendar_tags") or []:
            normalized_tag = str(tag).strip().lower()
            if not _MARKET_KEY_RE.fullmatch(normalized_tag):
                raise ValueError(f"Market {key} has an invalid calendar tag")
            if normalized_tag not in calendar_tags:
                calendar_tags.append(normalized_tag)
        normalized_markets.append({
            "key": key,
            "country_code": country,
            "region_codes": region_codes,
            "timezone": timezone,
            "locales": locales,
            "calendar_tags": calendar_tags,
        })

    default_key = str(value.get("default_market_key") or "").strip().lower()
    if default_key not in keys:
        raise ValueError("market_config.default_market_key must reference one configured market")
    return {"default_market_key": default_key, "markets": normalized_markets}


def public_foundation(project: Any) -> Dict[str, Any]:
    # ``project_markets`` is canonical after migration 154.  The JSONB column
    # remains a compatibility projection for old clients and migrations.  A
    # transient test/project or a not-yet-backfilled database safely falls
    # back to that projection.
    market_config = deepcopy(getattr(project, "market_config", None))
    rows = getattr(project, "markets", None)
    if rows:
        active = sorted(
            [row for row in rows if getattr(row, "status", "active") == "active"],
            key=lambda row: row.key,
        )
        default = next((row for row in active if row.is_default), None)
        if active and default:
            market_config = {
                "default_market_key": default.key,
                "markets": [
                    {
                        "key": row.key,
                        "country_code": row.country_code,
                        "region_codes": list(row.region_codes or []),
                        "timezone": row.timezone,
                        "locales": list(row.locales or []),
                        "calendar_tags": list(row.calendar_tags or []),
                    }
                    for row in active
                ],
            }
    return {
        "default_locale": project.default_locale,
        "supported_locales": project.supported_locales or [project.default_locale],
        "default_timezone": project.default_timezone,
        "goal_event": project.goal_event,
        "market_config": market_config,
    }


def normalize_foundation_update(project: Any, settings: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings must contain at least one project foundation field")
    unknown = set(settings) - _ALLOWED_SETTINGS
    if unknown:
        raise ValueError(f"settings contains unsupported fields: {sorted(unknown)}")

    current = public_foundation(project)
    target = deepcopy(current)
    if "default_locale" in settings:
        target["default_locale"] = _locale(settings["default_locale"], "default_locale")
    if "supported_locales" in settings:
        target["supported_locales"] = _string_list(settings["supported_locales"], "supported_locales")
    if target["default_locale"] not in target["supported_locales"]:
        target["supported_locales"] = [target["default_locale"], *target["supported_locales"]]
    if "default_timezone" in settings:
        target["default_timezone"] = _timezone(settings["default_timezone"], "default_timezone")
    if "goal_event" in settings:
        raw_goal = settings["goal_event"]
        if raw_goal is None:
            target["goal_event"] = None
        else:
            goal = str(raw_goal).strip().lower()
            if not _KEY_RE.fullmatch(goal):
                raise ValueError("goal_event must be a stable event key, not a label or sentence")
            target["goal_event"] = goal
    if "market_config" in settings:
        target["market_config"] = normalize_market_config(
            settings["market_config"], target["supported_locales"]
        )
    elif target["market_config"] is not None:
        target["market_config"] = normalize_market_config(
            target["market_config"], target["supported_locales"]
        )

    changes = {
        key: {"from": current[key], "to": target[key]}
        for key in target
        if current[key] != target[key]
    }
    return {"before": current, "after": target, "changes": changes}


def apply_foundation(project: Any, target: Dict[str, Any]) -> None:
    for field in _ALLOWED_SETTINGS:
        setattr(project, field, deepcopy(target[field]))
