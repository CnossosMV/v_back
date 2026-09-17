"""
Template variant selection for content i18n (Phase 1).

A template is a *family*: rows share (project_id, slug) and differ by `locale`.
resolve_template picks the variant matching the contact's locale, falling back to
the project's default locale, then to any variant of that slug.

Gated by USE_TEMPLATE_VARIANTS. When off, selection is slug-only (today's behaviour).
"""
import logging

from app.models import Project
from app.models.messaging import MessagingTemplate

logger = logging.getLogger(__name__)


def _flag_on(db=None, project_id: int | None = None) -> bool:
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "template_variants") == "enforce"


def _base_query(db, project_id: int, slug: str):
    return db.query(MessagingTemplate).filter(
        MessagingTemplate.project_id == project_id,
        MessagingTemplate.slug == slug,
    )


def resolve_template(db, project_id: int, slug: str, locale: str | None = None):
    """Return the best MessagingTemplate for (project, slug, locale), or None.

    Flag off  → first row matching the slug (locale-agnostic, == today).
    Flag on   → exact (slug, locale) → (slug, project.default_locale) → any (slug).
    Active variants are preferred over inactive ones.
    """
    if not _flag_on(db, project_id):
        return _base_query(db, project_id, slug).order_by(
            MessagingTemplate.is_active.desc(), MessagingTemplate.id.asc()
        ).first()

    rows = _base_query(db, project_id, slug).order_by(
        MessagingTemplate.is_active.desc(), MessagingTemplate.id.asc()
    ).all()
    if not rows:
        return None

    by_locale = {}
    for r in rows:
        by_locale.setdefault(r.locale, r)

    if locale and locale in by_locale:
        return by_locale[locale]

    language = (locale or "").split("-", 1)[0]
    if language:
        same_language = next(
            (row for row in rows if (row.locale or "").split("-", 1)[0] == language),
            None,
        )
        if same_language is not None:
            return same_language

    # A US contact must never fall back to Portuguese content.
    if (locale or "").endswith("-US"):
        if "en-US" in by_locale:
            return by_locale["en-US"]
        return next((row for row in rows if row.locale != "pt-BR"), None)

    default_locale = (
        db.query(Project.default_locale).filter(Project.id == project_id).scalar()
    )
    if default_locale and default_locale in by_locale:
        return by_locale[default_locale]

    return rows[0]
