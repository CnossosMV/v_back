"""
Real-time WebSocket endpoints for inbox live updates and user notifications.
"""

import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.services.realtime import redis_pubsub
from app.services.authorization_service import check_project_access

import jwt
import os

logger = logging.getLogger(__name__)
router = APIRouter(tags=["realtime"])

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"

HEARTBEAT_INTERVAL = 30  # seconds
REDIS_RESUBSCRIBE_DELAY = 1  # seconds


def _authenticate_ws(token: str) -> dict | None:
    """Validate JWT token and return user payload or None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None


async def _run_realtime_websocket(websocket: WebSocket, channel: str):
    """
    Keep a realtime WebSocket open until the client disconnects.

    Heartbeat and Redis subscription failures are supervised explicitly so an
    ended Redis listen loop does not tear down an otherwise healthy socket.
    """
    disconnected = asyncio.Event()
    send_lock = asyncio.Lock()

    async def _send_json(payload: dict):
        async with send_lock:
            await websocket.send_json(payload)

    async def _heartbeat():
        while not disconnected.is_set():
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if disconnected.is_set():
                    break
                await _send_json({"type": "ping"})
            except asyncio.CancelledError:
                raise
            except WebSocketDisconnect:
                disconnected.set()
                break
            except Exception as e:
                logger.info(f"WebSocket heartbeat failed on {channel}: {e}")
                disconnected.set()
                break

    async def _subscribe():
        while not disconnected.is_set():
            try:
                async for event in redis_pubsub.subscribe(channel):
                    if disconnected.is_set():
                        break
                    await _send_json(event)

                if not disconnected.is_set():
                    logger.warning(f"Redis subscription ended for {channel}; resubscribing")
                    await asyncio.sleep(REDIS_RESUBSCRIBE_DELAY)
            except asyncio.CancelledError:
                raise
            except WebSocketDisconnect:
                disconnected.set()
                break
            except Exception as e:
                logger.warning(f"Realtime subscription loop failed for {channel}: {e}")
                if not disconnected.is_set():
                    await asyncio.sleep(REDIS_RESUBSCRIBE_DELAY)

    async def _receive():
        try:
            while True:
                await websocket.receive_text()
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            disconnected.set()
        except Exception as e:
            logger.info(f"WebSocket receive ended on {channel}: {e}")
            disconnected.set()

    tasks = [
        asyncio.create_task(_heartbeat()),
        asyncio.create_task(_receive()),
    ]

    if redis_pubsub.is_available():
        tasks.append(asyncio.create_task(_subscribe()))

    try:
        await disconnected.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/ws/inbox/{project_id}")
async def inbox_websocket(
    websocket: WebSocket,
    project_id: int,
    token: str = Query(...),
):
    """
    WebSocket for real-time inbox updates.
    Subscribes to project:{project_id}:inbox Redis channel.
    Auth via ?token=JWT query param.
    """
    # Authenticate
    payload = _authenticate_ws(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid token")
        return

    user_id = payload.get("user_id") or payload.get("sub")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token payload")
        return

    # Check project access (support_agent+)
    db: Session = SessionLocal()
    try:
        from app.models import User
        from app.services.authorization_service import is_workspace_admin
        user = db.query(User).filter(User.id == int(user_id)).first()
        if not user:
            await websocket.close(code=4003, reason="User not found")
            return

        is_admin = is_workspace_admin(db, user)
        if not is_admin:
            membership = check_project_access(db, int(user_id), project_id, "support_agent")
            if not membership:
                await websocket.close(code=4003, reason="Access denied")
                return
    finally:
        db.close()

    await websocket.accept()

    if not redis_pubsub.is_available():
        # Send a warning but keep connection alive for polling fallback
        await websocket.send_json({"type": "warning", "data": {"message": "Real-time updates unavailable"}})

    channel = f"project:{project_id}:inbox"

    await _run_realtime_websocket(websocket, channel)


@router.websocket("/ws/notifications")
async def notifications_websocket(
    websocket: WebSocket,
    token: str = Query(...),
):
    """
    WebSocket for per-user notifications (badge count, etc).
    Subscribes to user:{user_id}:notifications Redis channel.
    """
    payload = _authenticate_ws(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid token")
        return

    user_id = payload.get("user_id") or payload.get("sub")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token payload")
        return

    await websocket.accept()

    if not redis_pubsub.is_available():
        await websocket.send_json({"type": "warning", "data": {"message": "Real-time updates unavailable"}})

    channel = f"user:{int(user_id)}:notifications"

    await _run_realtime_websocket(websocket, channel)
