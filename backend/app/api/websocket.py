"""
WebSocket endpoint for real-time processing progress.

Client connects to: ws://.../ws/progress/{dataset_id}?token=<JWT>
Backend subscribes to Redis pub/sub channel and streams updates.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status

from app.core.config import settings
from app.core.security import decode_token

log = structlog.get_logger()
router = APIRouter(prefix="/ws", tags=["websocket"])


class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, dataset_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.setdefault(dataset_id, []).append(ws)
        log.info("ws_connected", dataset_id=dataset_id)

    def disconnect(self, dataset_id: str, ws: WebSocket) -> None:
        conns = self._connections.get(dataset_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            self._connections.pop(dataset_id, None)
        log.info("ws_disconnected", dataset_id=dataset_id)

    async def send(self, dataset_id: str, message: dict) -> None:
        conns = self._connections.get(dataset_id, [])
        dead = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(dataset_id, ws)


manager = ConnectionManager()


async def _authenticate_ws(token: Optional[str]) -> Optional[str]:
    """Validate JWT from WebSocket query param, return user_id or None."""
    if not token:
        return None
    try:
        payload = decode_token(token)
        return payload.get("sub")
    except Exception:
        return None


@router.websocket("/progress/{dataset_id}")
async def ws_progress(
    websocket: WebSocket,
    dataset_id: str,
    token: Optional[str] = Query(None),
) -> None:
    """Stream real-time processing progress for a dataset."""

    user_id = await _authenticate_ws(token)
    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(dataset_id, websocket)

    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    channel = f"dataset:{dataset_id}:progress"

    # Send last known state immediately
    try:
        last = await redis.get(f"dataset:{dataset_id}:last_progress")
        if last:
            await websocket.send_text(last)
    except Exception:
        pass

    async def _subscribe() -> None:
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    try:
                        await websocket.send_text(data)
                        parsed = json.loads(data)
                        if parsed.get("status") in ("completed", "failed"):
                            break
                    except Exception:
                        break
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    # Heartbeat task to keep connection alive
    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await websocket.send_json({"type": "ping"})
            except Exception:
                break

    try:
        await asyncio.gather(
            _subscribe(),
            _heartbeat(),
            return_exceptions=True,
        )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws_error", dataset_id=dataset_id, error=str(exc))
    finally:
        manager.disconnect(dataset_id, websocket)
        await redis.aclose()
