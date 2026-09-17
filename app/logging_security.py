"""Logging helpers that prevent query-string credentials from reaching logs."""

from __future__ import annotations

import logging
import re
from typing import Any


_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:api|key|api_key|token|access_token)=)[^&\s\"']+"
)

_SENSITIVE_LOGGER_NAMES = (
    "httpx",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "gunicorn.error",
    "gunicorn.access",
)


def redact_sensitive_query_values(value: str) -> str:
    return _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", value)


class SensitiveQueryFilter(logging.Filter):
    """Redact credentials embedded in URL query strings before formatting."""

    @staticmethod
    def _redact_arg(value: Any) -> Any:
        if isinstance(value, str):
            return redact_sensitive_query_values(value)
        rendered = str(value)
        if _SENSITIVE_QUERY_VALUE.search(rendered):
            return redact_sensitive_query_values(rendered)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_query_values(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: self._redact_arg(value) for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(self._redact_arg(value) for value in record.args)
        return True


def install_sensitive_query_filter() -> SensitiveQueryFilter:
    log_filter = SensitiveQueryFilter()
    loggers = [logging.getLogger()]
    loggers.extend(logging.getLogger(name) for name in _SENSITIVE_LOGGER_NAMES)
    for logger in loggers:
        # Uvicorn/Gunicorn configure their own non-root handlers before loading
        # the application. Logger-level filters do not propagate from parent
        # loggers, so protect both each concrete logger and its current
        # handlers.
        logger.addFilter(log_filter)
        for handler in logger.handlers:
            handler.addFilter(log_filter)
    return log_filter
