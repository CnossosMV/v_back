"""
Redis Pub/Sub singleton for real-time event broadcasting.

Channels:
  project:{project_id}:inbox  — inbox events (ticket CRUD, messages)
  user:{user_id}:notifications — per-user push notifications
"""

import asyncio
import json
import logging
import os
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)

_redis_pool = None
_redis_available = False


async def init_redis():
    """Initialize the Redis connection pool. Call once at startup."""
    global _redis_pool, _redis_available
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        logger.warning("REDIS_URL not set — real-time features disabled")
        return

    try:
        import redis.asyncio as aioredis
        _redis_pool = aioredis.from_url(
            redis_url,
            decode_responses=True,
            max_connections=20,
        )
        # Test connection
        await _redis_pool.ping()
        _redis_available = True
        logger.info("Redis connected for real-time pub/sub")
    except Exception as e:
        logger.warning(f"Redis connection failed — real-time features disabled: {e}")
        _redis_pool = None
        _redis_available = False


async def close_redis():
    """Close the Redis connection pool. Call at shutdown."""
    global _redis_pool, _redis_available
    if _redis_pool:
        await _redis_pool.close()
        _redis_pool = None
        _redis_available = False


async def publish(channel: str, event: dict) -> bool:
    """Publish an event to a Redis channel. Returns True if published."""
    if not _redis_available or not _redis_pool:
        return False
    try:
        await _redis_pool.publish(channel, json.dumps(event))
        return True
    except Exception as e:
        logger.error(f"Redis publish error on {channel}: {e}")
        return False


async def subscribe(channel: str) -> AsyncGenerator[dict, None]:
    """Subscribe to a Redis channel. Yields events as dicts."""
    if not _redis_available or not _redis_pool:
        return

    import redis.asyncio as aioredis
    pubsub = _redis_pool.pubsub()
    try:
        await pubsub.subscribe(channel)
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    yield json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Redis subscribe error on {channel}: {e}")
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()


def is_available() -> bool:
    """Check if Redis is connected and available."""
    return _redis_available


async def publish_inbox_event(project_id: int, event_type: str, data: dict):
    """Convenience: publish to project inbox channel."""
    channel = f"project:{project_id}:inbox"
    event = {"type": event_type, "data": data}
    await publish(channel, event)


async def publish_user_notification(user_id: int, event_type: str, data: dict):
    """Convenience: publish to user notification channel."""
    channel = f"user:{user_id}:notifications"
    event = {"type": event_type, "data": data}
    await publish(channel, event)
