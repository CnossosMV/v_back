from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from app.database import engine, Base, SessionLocal
from app.routers import (
    customers, smtp_config, whatsapp_config, secrets, auth, whatsapp, projects,
    funnels, meta_whatsapp, meta_connections, meta_webhooks,
    postforme_credentials, postforme_accounts, postforme_posts, postforme_media,
    chatbots, chat, knowledge_base, whitelist_signup,
    webhooks, support_inbox, messaging_providers, chat_widget, event_actions,
    agent_teams, specialists, router_config, specialist_tools,
    team_playbooks, team_analytics, llm_config, llm_usage,
    funnel_analytics,
    funnel_sandbox,
    api_connections,
    scoring, policies,
    personalization,
    segments,
    handler_channel_links,
    inbox_rules, routing_states,
    media_assets,
    approvals,
    webhook_sources, webhook_ingest,
    send_layer,
    positions,
    bandit,
    engine as engine_router,
    decision_gates,
    capabilities,
    base_cells,
    project_members,
    realtime,
    push_notifications,
    knowledge_library,
    email_tracking,
    email_delivery_webhooks,
    unsubscribe,
    channel_health,
    sending_domains,
    channel_configs,
    email_inbound,
    extension_auth, nocode_mappings,
    project_variables,
    email_instances,
    mes,
    journey,
    mcp_settings,
    contact_verification,
    contact_groups,
    campaigns,
    channel_delivery_profiles,
    contact_profiles,
    project_imports,
    agent_skills,
)
from app.routers.messaging import (
    domains_router, api_keys_router, templates_router,
    users_router, events_router, logs_router, public_api_router,
    event_schemas_router, dsar_router, destinations_router,
    event_mappings_router, gtm_router, tracking_domains_router,
    tracking_domains_internal_router, audiences_router
)
from app.routers.messaging import contacts as messaging_contacts
from app.routers.messaging import accounts as messaging_accounts
from app.middleware import DynamicCORSMiddleware
import os
import logging
from dotenv import load_dotenv
from app.logging_security import install_sensitive_query_filter

load_dotenv()

# Configure root logger so all app loggers (schedulers, services) are visible
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
install_sensitive_query_filter()
logger = logging.getLogger(__name__)

if os.getenv("ALLOW_AUTO_CREATE_TABLES", "").lower() in {"1", "true", "yes"}:
    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        logger.exception("Automatic table creation failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop background workers.

    Uses a PostgreSQL advisory lock so that only ONE Gunicorn worker
    runs the background schedulers. The others just serve HTTP requests.
    The lock is session-scoped and auto-released if the worker crashes.
    """
    from sqlalchemy import text as sa_text
    from app.services.event_actions.scheduler import scheduled_action_worker
    from app.services.funnel_scheduler import funnel_scheduler_worker
    from app.services.scoring.scoring_scheduler import scoring_scheduler_worker
    from app.services.segment_scheduler import segment_scheduler_worker
    from app.services.inbox_expiry_worker import inbox_expiry_worker
    from app.services.webhook.webhook_worker import webhook_worker
    from app.services.messaging.destination_worker import destination_delivery_worker
    from app.services.messaging.audience_sync_worker import audience_sync_worker
    from app.services.messaging.audience_webhook_worker import audience_webhook_worker
    from app.services.channels.scheduled_send_worker import scheduled_send_worker
    from app.services.channels.sending_reputation_worker import sending_reputation_worker
    from app.services.scoring.mes_scheduler import mes_scheduler_worker
    from app.services.journey.materializer import journey_materializer_worker
    from app.services.messaging.event_drain_worker import event_drain_worker
    from app.services.position.position_sweep_worker import position_sweep_worker
    from app.services.bandit.reward_sweep_worker import reward_sweep_worker
    from app.services.position.base_compiler_worker import base_compiler_worker
    from app.services.meta_oauth_cleanup_worker import meta_oauth_cleanup_worker
    from app.services.channels.registry import init_channel_registry
    from app.services.contact_verification.worker import contact_verification_worker
    from app.services.campaigns.worker import campaign_worker
    from app.services.project_import_worker import project_import_worker
    from app.services.campaigns.recipe_worker import campaign_recipe_worker

    # Fail closed: Meta channels handle page tokens / app secrets — refuse to
    # boot without an encryption key rather than silently storing plaintext.
    if (os.getenv("META_APP_ID") or os.getenv("META_APP_SECRET")) and not os.getenv("ENCRYPTION_KEY"):
        raise RuntimeError(
            "ENCRYPTION_KEY must be set when Meta channels are configured "
            "(META_APP_ID/META_APP_SECRET present). Refusing to start."
        )

    init_channel_registry()

    mcp_stack = AsyncExitStack()
    for mcp_server in getattr(app.state, "mcp_servers", []):
        await mcp_stack.enter_async_context(mcp_server.session_manager.run())

    # Cleanup any stale debug events on startup
    try:
        from app.services.nocode_debug_service import NoCodeDebugService
        cleanup_db = SessionLocal()
        NoCodeDebugService(cleanup_db).cleanup_expired()
        cleanup_db.close()
    except Exception:
        pass

    # Init Redis for real-time pub/sub
    from app.services.realtime.redis_pubsub import init_redis, close_redis
    await init_redis()

    # Acquire advisory lock so only one worker runs schedulers.
    # Lock ID 737373 is arbitrary but deterministic.
    scheduler_lock_db = SessionLocal()
    is_scheduler_leader = False
    try:
        is_scheduler_leader = scheduler_lock_db.execute(
            sa_text("SELECT pg_try_advisory_lock(737373)")
        ).scalar()
    except Exception:
        scheduler_lock_db.close()
        scheduler_lock_db = None

    if is_scheduler_leader:
        logger.info("This worker acquired scheduler lock — starting background workers")
        await scheduled_action_worker.start(SessionLocal)
        await funnel_scheduler_worker.start(SessionLocal)
        await scoring_scheduler_worker.start(SessionLocal)
        await segment_scheduler_worker.start(SessionLocal)
        await inbox_expiry_worker.start(SessionLocal)
        await webhook_worker.start(SessionLocal)
        await destination_delivery_worker.start(SessionLocal)
        await audience_sync_worker.start(SessionLocal)
        await audience_webhook_worker.start(SessionLocal)
        await scheduled_send_worker.start(SessionLocal)
        await sending_reputation_worker.start(SessionLocal)
        await mes_scheduler_worker.start(SessionLocal)
        await journey_materializer_worker.start(SessionLocal)
        await event_drain_worker.start(SessionLocal)
        await position_sweep_worker.start(SessionLocal)
        await reward_sweep_worker.start(SessionLocal)
        await base_compiler_worker.start(SessionLocal)
        await meta_oauth_cleanup_worker.start(SessionLocal)
        await contact_verification_worker.start(SessionLocal)
        await campaign_worker.start(SessionLocal)
        await project_import_worker.start(SessionLocal)
        await campaign_recipe_worker.start(SessionLocal)
    else:
        logger.info("Another worker holds scheduler lock — this worker serves HTTP only")

    yield

    if is_scheduler_leader:
        await campaign_recipe_worker.stop()
        await project_import_worker.stop()
        await campaign_worker.stop()
        await contact_verification_worker.stop()
        await meta_oauth_cleanup_worker.stop()
        await base_compiler_worker.stop()
        await reward_sweep_worker.stop()
        await position_sweep_worker.stop()
        await event_drain_worker.stop()
        await journey_materializer_worker.stop()
        await mes_scheduler_worker.stop()
        await sending_reputation_worker.stop()
        await scheduled_send_worker.stop()
        await audience_webhook_worker.stop()
        await audience_sync_worker.stop()
        await destination_delivery_worker.stop()
        await webhook_worker.stop()
        await inbox_expiry_worker.stop()
        await segment_scheduler_worker.stop()
        await scoring_scheduler_worker.stop()
        await funnel_scheduler_worker.stop()
        await scheduled_action_worker.stop()

    await mcp_stack.aclose()

    await close_redis()

    # Release advisory lock
    if scheduler_lock_db:
        try:
            scheduler_lock_db.close()
        except Exception:
            pass


app = FastAPI(title="Customer Management API", version="1.0.0", lifespan=lifespan)

_OFFICIAL_DOCS_DIR = Path(__file__).resolve().parents[1] / "docs" / "official"
app.mount(
    "/docs/official",
    StaticFiles(directory=str(_OFFICIAL_DOCS_DIR)),
    name="official-docs",
)

# Get CORS origins from environment variable (for non-SDK endpoints)
cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3001,http://localhost:3002,http://localhost:5173").split(",")

# Standard CORS for dashboard/admin endpoints
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)

# Dynamic CORS for SDK endpoints - allows any registered domain
app.add_middleware(DynamicCORSMiddleware)

from app.mcp.mounts import configure_mcp
configure_mcp(app)

app.include_router(agent_skills.router)  # Public, versioned tenant-agent skill distribution

app.include_router(auth.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(customers.router, prefix="/api/v1")
app.include_router(smtp_config.router, prefix="/api/v1")
app.include_router(whatsapp_config.router, prefix="/api/v1")
app.include_router(whatsapp.router, prefix="/api/v1")  # WhatsApp Evolution API
app.include_router(meta_whatsapp.router, prefix="/api/v1")  # WhatsApp Meta Cloud API
app.include_router(meta_connections.router, prefix="/api/v1")  # Messenger/Instagram page connections (OAuth)
app.include_router(meta_webhooks.router, prefix="/api/v1")  # Messenger/Instagram app-level webhook
app.include_router(funnels.router, prefix="/api/v1")  # Funnel Engine
app.include_router(secrets.router, prefix="/api/v1")

# Post for Me Integration
app.include_router(postforme_credentials.router, prefix="/api/v1")  # API key management
app.include_router(postforme_accounts.router, prefix="/api/v1")  # Social accounts
app.include_router(postforme_posts.router, prefix="/api/v1")  # Posts management
app.include_router(postforme_media.router, prefix="/api/v1")  # Media uploads

# Chatbot / AI Agents
app.include_router(chatbots.router, prefix="/api/v1")  # Chatbot CRUD
app.include_router(chat.router, prefix="/api/v1")  # Chat conversations
app.include_router(knowledge_base.router, prefix="/api/v1")  # Knowledge base management

# Public marketing website
app.include_router(whitelist_signup.router, prefix="/api/v1")  # Whitelist signups from static website

# Messaging Middleware
app.include_router(domains_router, prefix="/api/v1")  # Domain management
app.include_router(api_keys_router, prefix="/api/v1")  # API key management
# channels_router removed — replaced by channel_configs.router (project-level channel management)
app.include_router(templates_router, prefix="/api/v1")  # Template management
from app.routers.messaging import locale_channel_map as locale_channel_map_router
app.include_router(locale_channel_map_router.router, prefix="/api/v1")  # i18n per-locale sender routing
app.include_router(users_router, prefix="/api/v1")  # Messaging users
app.include_router(events_router, prefix="/api/v1")  # Event tracking
app.include_router(logs_router, prefix="/api/v1")  # Message logs
app.include_router(public_api_router)  # Public SDK endpoints (identify, track, send)
app.include_router(event_schemas_router, prefix="/api/v1")  # Event schema definitions
app.include_router(dsar_router, prefix="/api/v1")  # DSAR privacy requests
app.include_router(destinations_router, prefix="/api/v1")  # Analytics destinations
app.include_router(event_mappings_router, prefix="/api/v1")  # Event name mappings
app.include_router(gtm_router, prefix="/api/v1")  # GTM container management
app.include_router(tracking_domains_router, prefix="/api/v1")  # First-party tracking domains
app.include_router(tracking_domains_internal_router, prefix="/api/v1")  # Edge/Worker config and logs
app.include_router(audiences_router, prefix="/api/v1")  # Ads audience sync
app.include_router(messaging_contacts.router)  # Contact management & merge
app.include_router(messaging_accounts.router)  # B2B account management

# Unified Chat System
app.include_router(webhooks.router, prefix="/api/v1")  # Twilio webhooks
app.include_router(support_inbox.router, prefix="/api/v1")  # Support inbox & tickets
app.include_router(handler_channel_links.router, prefix="/api/v1")  # Handler-channel links
app.include_router(messaging_providers.router, prefix="/api/v1")  # Messaging providers
app.include_router(chat_widget.router, prefix="/api/v1")  # Chat widget config & public API
app.include_router(event_actions.router, prefix="/api/v1")  # Event Actions automation

# Agent Teams Orchestration
app.include_router(agent_teams.router, prefix="/api/v1")  # Agent Teams CRUD, migration & chat
app.include_router(specialists.router, prefix="/api/v1")  # Specialist agents, knowledge & guardrails
app.include_router(router_config.router, prefix="/api/v1")  # Router config & routing rules
app.include_router(specialist_tools.router, prefix="/api/v1")  # Specialist tools & execution
app.include_router(team_playbooks.router, prefix="/api/v1")  # Playbook templates
app.include_router(team_analytics.router, prefix="/api/v1")  # Team analytics & metrics
app.include_router(llm_config.router, prefix="/api/v1")  # Project LLM config & API keys
app.include_router(llm_usage.router, prefix="/api/v1")  # LLM usage tracking
app.include_router(funnel_analytics.router, prefix="/api/v1")  # Funnel analytics & metrics
app.include_router(funnel_sandbox.router, prefix="/api/v1")  # Funnel sandbox testing
app.include_router(api_connections.router, prefix="/api/v1")  # Project API connections
app.include_router(scoring.router, prefix="/api/v1")  # Intent scoring definitions & analytics
app.include_router(mes.router, prefix="/api/v1")  # Message effectiveness scoring
app.include_router(journey.router, prefix="/api/v1")  # Event graph
app.include_router(policies.router, prefix="/api/v1")  # Automation policy guardrails
app.include_router(personalization.router, prefix="/api/v1")  # Visitor personalization config
app.include_router(segments.router, prefix="/api/v1")  # Segment rules & classification
app.include_router(inbox_rules.router, prefix="/api/v1")  # Inbox assignment rules
app.include_router(routing_states.router, prefix="/api/v1")  # Contact routing states
app.include_router(media_assets.router, prefix="/api/v1")  # Media assets for chatbots & agent teams
app.include_router(approvals.router, prefix="/api/v1")  # Debug mode approval queue
app.include_router(webhook_sources.router, prefix="/api/v1")  # Webhook source CRUD & management
app.include_router(webhook_ingest.router, prefix="/api/v1")  # Public webhook ingest endpoint
app.include_router(send_layer.router, prefix="/api/v1")  # Send layer config & logs
app.include_router(send_layer.channels_router, prefix="/api/v1")  # Channel capabilities
app.include_router(positions.router, prefix="/api/v1")  # Contact Position read API (Phase 3)
app.include_router(bandit.router, prefix="/api/v1")  # Bandit recommend API (Phase 4)
app.include_router(engine_router.router, prefix="/api/v1")  # Lifecycle engine status (ops)
app.include_router(decision_gates.router, prefix="/api/v1")  # Persisted consequence choices
app.include_router(capabilities.router, prefix="/api/v1")  # Agent/operator capability boundary
app.include_router(base_cells.router, prefix="/api/v1")  # Base-cell authoring (Phase 3 UX)
app.include_router(project_members.router, prefix="/api/v1")  # Project members & roles
app.include_router(push_notifications.router, prefix="/api/v1")  # Push notification subscriptions
app.include_router(knowledge_library.router, prefix="/api/v1")  # Knowledge asset library
app.include_router(realtime.router)  # WebSocket endpoints (no /api/v1 prefix)
app.include_router(channel_health.router, prefix="/api/v1")  # Channel health dashboard
app.include_router(contact_verification.router, prefix="/api/v1")  # Contact verification jobs and policy
app.include_router(contact_profiles.router, prefix="/api/v1")  # Contact endpoints, permission evidence and data quality
app.include_router(sending_domains.router, prefix="/api/v1")  # Sending-domain pace governor admin
app.include_router(channel_configs.router, prefix="/api/v1")  # Project channel configs
app.include_router(email_inbound.router, prefix="/api/v1")  # Email inbound address management
app.include_router(email_inbound.webhook_router)  # Email inbound webhooks (no /api/v1 prefix)
app.include_router(email_tracking.router)  # Email open tracking pixel (no /api/v1 prefix — short URL)
app.include_router(email_delivery_webhooks.router)  # Email bounce/complaint webhooks (no /api/v1 prefix)
app.include_router(unsubscribe.router)  # One-click unsubscribe (no /api/v1 prefix)
app.include_router(extension_auth.router, prefix="/api/v1")  # Extension auth tokens
app.include_router(nocode_mappings.router, prefix="/api/v1")  # NoCode mapping CRUD + publish
app.include_router(project_variables.router, prefix="/api/v1")  # Project-level template variables
app.include_router(email_instances.router, prefix="/api/v1")  # Email instances (SMTP + API)
app.include_router(mcp_settings.router, prefix="/api/v1")  # MCP connector settings
app.include_router(contact_groups.router, prefix="/api/v1")  # Reusable contact groups
app.include_router(campaigns.router, prefix="/api/v1")  # Campaign planning and durable runs
app.include_router(channel_delivery_profiles.router, prefix="/api/v1")  # Campaign sender capacity
app.include_router(project_imports.router, prefix="/api/v1")  # Project Import + lifecycle model approval

@app.get("/")
def read_root():
    return {"message": "Customer Management API"}

@app.get("/health")
def health_check():
    from datetime import datetime
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat()
    }


# ── SDK file serving with cache control + version endpoint ─────────────
_SDK_DIR = Path(__file__).resolve().parent / "sdk"

from app.sdk import SDK_VERSION, SDK_ETAG

@app.get("/sdk/version")
def sdk_version():
    """Return the current SDK version (public, unauthenticated)."""
    return JSONResponse(
        content={"version": SDK_VERSION},
        headers={"Cache-Control": "public, max-age=0, must-revalidate"},
    )

@app.get("/sdk/versya-messaging.js")
def serve_sdk(request: Request):
    """Serve the Versya SDK with ETag support and forced revalidation."""
    cache_headers = {
        "Cache-Control": "public, max-age=0, must-revalidate",
        "CDN-Cache-Control": "public, max-age=60, must-revalidate",
        "ETag": SDK_ETAG,
    }

    # ETag-based 304 Not Modified
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == SDK_ETAG:
        return Response(status_code=304, headers=cache_headers)

    path = _SDK_DIR / "versya-messaging.js"
    return FileResponse(
        path,
        media_type="application/javascript",
        headers=cache_headers,
    )
