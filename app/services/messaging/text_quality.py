"""Content-quality checks used immediately before provider delivery."""
from __future__ import annotations

from collections.abc import Iterable


MOJIBAKE_MARKERS = ("\u00c3", "\u00c2", "\u00e2\u20ac", "\ufffd")


def find_mojibake(values: Iterable[object]) -> str | None:
    """Return the first suspicious marker found in rendered user-facing text."""
    for value in values:
        if not isinstance(value, str):
            continue
        for marker in MOJIBAKE_MARKERS:
            if marker in value:
                return marker
    return None


def has_mojibake(*values: object) -> bool:
    return find_mojibake(values) is not None

