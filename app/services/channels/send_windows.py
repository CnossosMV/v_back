"""
Best-time-to-send (BTS) windows — pure helpers, no DB access.

Uses stdlib zoneinfo (pytz is NOT a project dependency — several legacy
call sites lazy-import it and would fail; do not add pytz here).

A send-windows payload is a dict keyed by channel ("_default" = all channels):

    {"_default": {"enabled": true,
                  "windows": [{"days": [0,1,2,3,4], "start": "09:00", "end": "18:00"}]},
     "whatsapp": {"enabled": false, "windows": []}}

Semantics:
- Key ABSENT at a level        -> inherit from the next level.
- Key present, enabled=false   -> explicitly unrestricted (inheritance stops).
- Key present, enabled=true    -> the windows list applies.
- days: 0=Monday .. 6=Sunday (Python weekday()); omitted = every day.
- start > end = midnight-crossing window; its `days` match the START day.
- Times are in the CONTACT's local timezone.

Resolution order (first key present wins):
  contact[channel] -> contact["_default"] -> project[channel] -> project["_default"]
"""
import logging
import re
from datetime import datetime, timedelta, time as dtime, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_KEY = "_default"
KNOWN_CHANNEL_KEYS = {DEFAULT_KEY, "whatsapp", "email", "sms", "web"}
MAX_WINDOWS = 4
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_UTC = dt_timezone.utc


def validate_send_windows(payload: Any) -> List[str]:
    """Validate a send-windows payload; returns a list of error strings."""
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["send_windows must be an object keyed by channel"]
    for key, cfg in payload.items():
        if key not in KNOWN_CHANNEL_KEYS:
            errors.append(f"unknown channel key '{key}' (allowed: {sorted(KNOWN_CHANNEL_KEYS)})")
            continue
        if not isinstance(cfg, dict):
            errors.append(f"{key}: config must be an object")
            continue
        enabled = cfg.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(f"{key}: 'enabled' must be a boolean")
            continue
        windows = cfg.get("windows", [])
        if not isinstance(windows, list):
            errors.append(f"{key}: 'windows' must be a list")
            continue
        if enabled and not windows:
            errors.append(f"{key}: enabled requires at least one window")
        if len(windows) > MAX_WINDOWS:
            errors.append(f"{key}: at most {MAX_WINDOWS} windows")
        for i, w in enumerate(windows):
            if not isinstance(w, dict):
                errors.append(f"{key}.windows[{i}]: must be an object")
                continue
            start, end = w.get("start"), w.get("end")
            if not (isinstance(start, str) and _TIME_RE.match(start)):
                errors.append(f"{key}.windows[{i}]: start must be HH:MM")
            if not (isinstance(end, str) and _TIME_RE.match(end)):
                errors.append(f"{key}.windows[{i}]: end must be HH:MM")
            if start and end and start == end:
                errors.append(f"{key}.windows[{i}]: start and end must differ")
            days = w.get("days")
            if days is not None:
                if (not isinstance(days, list) or not days
                        or any(not isinstance(d, int) or d < 0 or d > 6 for d in days)
                        or len(set(days)) != len(days)):
                    errors.append(
                        f"{key}.windows[{i}]: days must be a non-empty list of unique ints 0-6 (0=Monday)"
                    )
    return errors


def resolve_window(
    user_windows: Optional[Dict[str, Any]],
    project_windows: Optional[Dict[str, Any]],
    channel: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the effective window config for a channel.

    Returns (window_cfg, source). window_cfg None = unrestricted.
    source in: contact_channel, contact, project_channel, project, None.
    """
    tiers = [
        (user_windows, channel, "contact_channel"),
        (user_windows, DEFAULT_KEY, "contact"),
        (project_windows, channel, "project_channel"),
        (project_windows, DEFAULT_KEY, "project"),
    ]
    for container, key, source in tiers:
        if not isinstance(container, dict) or not key:
            continue
        cfg = container.get(key)
        if cfg is None:
            continue
        if not isinstance(cfg, dict) or not isinstance(cfg.get("enabled"), bool):
            logger.warning("[send-window] malformed config at %s/%s — skipping tier", source, key)
            continue
        if not cfg["enabled"]:
            return None, source  # explicit opt-out stops inheritance
        windows = cfg.get("windows") or []
        if not windows:
            logger.warning("[send-window] enabled with no windows at %s — fail-open", source)
            return None, source
        return cfg, source
    return None, None


def resolve_timezone(user_tz: Optional[str], project_tz: Optional[str]) -> ZoneInfo:
    for tz_name in (user_tz, project_tz):
        if tz_name:
            try:
                return ZoneInfo(tz_name)
            except Exception:
                logger.warning("[send-window] unknown timezone %r — falling back", tz_name)
    return ZoneInfo("UTC")


def _parse_hhmm(value: str) -> dtime:
    hour, minute = map(int, value.split(":"))
    return dtime(hour=hour, minute=minute)


def _window_matches(window: Dict[str, Any], local_dt: datetime) -> bool:
    """True if local_dt falls inside the window (handles midnight-crossing)."""
    start = _parse_hhmm(window["start"])
    end = _parse_hhmm(window["end"])
    days = window.get("days")
    now_t = local_dt.time()
    weekday = local_dt.weekday()

    if start < end:
        return (days is None or weekday in days) and start <= now_t < end
    # Midnight-crossing: [start, 24h) on the window's day OR [0, end) on the
    # following day (days key matches the START day).
    if start <= now_t:
        return days is None or weekday in days
    if now_t < end:
        prev_day = (weekday - 1) % 7
        return days is None or prev_day in days
    return False


def is_within_window(window_cfg: Dict[str, Any], now_utc: datetime, tz: ZoneInfo) -> bool:
    """True if now (naive UTC) falls inside any of the config's windows."""
    local_dt = now_utc.replace(tzinfo=_UTC).astimezone(tz)
    return any(_window_matches(w, local_dt) for w in (window_cfg.get("windows") or []))


def next_window_start(
    window_cfg: Dict[str, Any], now_utc: datetime, tz: ZoneInfo
) -> Optional[datetime]:
    """Earliest upcoming window start strictly after now, as NAIVE UTC
    (pipeline convention). None if no window start found within 8 days
    (invalid config — caller fails open).

    DST notes (zoneinfo/PEP 495): a start inside a spring-forward gap maps to
    a valid instant automatically; for ambiguous fall-back times fold=1 picks
    the LATER occurrence — conservative, never sends early."""
    local_now = now_utc.replace(tzinfo=_UTC).astimezone(tz)
    windows = window_cfg.get("windows") or []
    best: Optional[datetime] = None

    for day_offset in range(8):
        day = (local_now + timedelta(days=day_offset)).date()
        weekday = day.weekday()
        for w in windows:
            days = w.get("days")
            if days is not None and weekday not in days:
                continue
            candidate = datetime.combine(day, _parse_hhmm(w["start"]), tzinfo=tz)
            candidate = candidate.replace(fold=1)
            if candidate <= local_now:
                continue
            if best is None or candidate < best:
                best = candidate

    if best is None:
        return None
    return best.astimezone(_UTC).replace(tzinfo=None)
