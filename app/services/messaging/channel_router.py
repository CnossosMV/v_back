"""
Per-locale channel routing (content i18n, Phase 3).

resolve_send_instance picks the sender instance for a contact's locale on a given
channel, falling back to the caller's default instance when there is no mapping.

Gated by USE_LOCALE_CHANNEL_ROUTING. When off (or unmapped), the default instance is
returned unchanged — so behaviour is identical to before the i18n work.
"""
import logging

from app.models import LocaleChannelMap

logger = logging.getLogger(__name__)


def _flag_on(db=None, project_id: int | None = None) -> bool:
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "locale_channel_routing") == "enforce"


def resolve_send_instance(
    db,
    project_id: int,
    locale: str | None,
    channel_type: str,
    default_instance_id: int | None,
) -> int | None:
    """Return the instance id to send from for (locale, channel_type).

    Flag off / no locale / no mapping ⇒ default_instance_id (unchanged).
    """
    if not _flag_on(db, project_id) or not locale:
        return default_instance_id

    mapped = db.query(LocaleChannelMap.instance_id).filter(
        LocaleChannelMap.project_id == project_id,
        LocaleChannelMap.locale == locale,
        LocaleChannelMap.channel_type == channel_type,
    ).scalar()

    return mapped if mapped is not None else default_instance_id
