"""
Messaging Middleware Routers
"""
from .domains import router as domains_router
from .api_keys import router as api_keys_router
# channels_router deprecated - replaced by channel_configs router
from .templates import router as templates_router
from .users import router as users_router
from .events import router as events_router
from .logs import router as logs_router
from .public_api import router as public_api_router
from .dsar import router as dsar_router
from .event_schemas import router as event_schemas_router
from .destinations import router as destinations_router
from .event_mappings import router as event_mappings_router
from .gtm import router as gtm_router
from .tracking_domains import router as tracking_domains_router
from .tracking_domains import internal_router as tracking_domains_internal_router
from .audiences import router as audiences_router

__all__ = [
    'domains_router',
    'api_keys_router',

    'templates_router',
    'users_router',
    'events_router',
    'logs_router',
    'public_api_router',
    'dsar_router',
    'event_schemas_router',
    'destinations_router',
    'event_mappings_router',
    'gtm_router',
    'tracking_domains_router',
    'tracking_domains_internal_router',
    'audiences_router',
]
