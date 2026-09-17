"""
Localization lint (content i18n, Phase 2).

When a source template is duplicated to a new locale, scan it and return a checklist
so nothing localization-sensitive is carried over blindly:

  - classify every {{variable}} → money / date / number / text
  - flag HARDCODED money / date literals in prose (these need a local value, NOT a
    currency conversion — money is never auto-converted; see the i18n plan)

Pure analysis: no DB, no LLM. Reuses TemplateRenderer's spec parser so filter-based
classification matches exactly how the renderer formats.
"""
import re

from app.services.messaging.template_renderer import template_renderer

# {{var}} kind inference by name when no explicit |filter is present.
_MONEY_NAME = re.compile(r"(price|amount|total|value|valor|preco|preço|cost|fee|subtotal)", re.I)
_DATE_NAME = re.compile(r"(date|data|_at$|expires?|expira|vence|venc|deadline|prazo|when|quando)", re.I)
_NUMBER_NAME = re.compile(r"(qty|quantity|quantidade|count|num|qtd|units?|unidades?)", re.I)

# Hardcoded literals in prose.
_MONEY_LITERAL = re.compile(
    r"(R\$|US\$|\$|€|£)\s?\d[\d.,]*"            # symbol → number  ($9.90, R$ 9,90)
    r"|\d[\d.,]*\s?(reais|euros?|d[óo]lares?|libras?)",  # number → word
    re.I,
)
_DATE_LITERAL = re.compile(
    r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b"          # 15/06 or 15/06/2026
    r"|\b\d{1,2}h(\s?(às|as|-|to)\s?\d{1,2}h)?\b",  # 9h, 9h às 18h
    re.I,
)


def _classify_var(name: str, filters) -> tuple[str, str | None]:
    """Return (kind, explicit_filter). Filter wins; else infer from the name."""
    for fn, _arg in filters:
        if fn in ("money", "date", "number"):
            return fn, fn
    if _MONEY_NAME.search(name):
        return "money", None
    if _DATE_NAME.search(name):
        return "date", None
    if _NUMBER_NAME.search(name):
        return "number", None
    return "text", None


def lint_template(body: str, subject: str | None = None) -> dict:
    """Analyse a template body (+subject) and return a localization checklist."""
    body = body or ""
    seen: dict[str, dict] = {}

    for text in (body, subject):
        if not text:
            continue
        for m in template_renderer.VARIABLE_PATTERN.finditer(text):
            name, filters, _default = template_renderer._parse_spec(m.group(1).strip())
            if name in seen:
                continue
            kind, explicit = _classify_var(name, filters)
            note = None
            if kind == "money" and explicit:
                note = "Money variable — the new locale must supply its own {amount, currency}. Not converted."
            elif kind == "money":
                note = "Looks like money — use the |money filter and supply {amount, currency} per locale (never converted)."
            elif kind == "date" and not explicit:
                note = "Looks like a date — use the |date filter so it formats per locale."
            seen[name] = {
                "name": name,
                "kind": kind,
                "filter": explicit,
                "needs_attention": kind in ("money", "date") and not explicit,
                "note": note,
            }

    hardcoded = []
    combined = "\n".join(t for t in (subject, body) if t)
    for m in _MONEY_LITERAL.finditer(combined):
        hardcoded.append({
            "type": "money",
            "text": m.group(0).strip(),
            "hint": "Hardcoded price — set the local value for the new locale (NOT a currency conversion).",
        })
    for m in _DATE_LITERAL.finditer(combined):
        hardcoded.append({
            "type": "date",
            "text": m.group(0).strip(),
            "hint": "Hardcoded date/time — confirm it for the new locale (format and value).",
        })

    variables = list(seen.values())
    attention = [v for v in variables if v["needs_attention"]]
    return {
        "variables": variables,
        "hardcoded": hardcoded,
        "summary": {
            "variable_count": len(variables),
            "needs_attention_count": len(attention),
            "hardcoded_count": len(hardcoded),
            "ready": not attention and not hardcoded,
        },
    }
