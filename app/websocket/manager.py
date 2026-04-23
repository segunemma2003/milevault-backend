"""
WebSocket connection manager with Redis pub/sub for horizontal scaling.
Each transaction gets its own channel: ws:transaction:{id}
"""
import asyncio
import json
import logging
from typing import Dict, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages active WebSocket connections grouped by channel (transaction_id or user_id).
    Uses Redis pub/sub so messages can be broadcast across multiple server instances.
    """

    def __init__(self):
        # {channel: {websocket, ...}}
        self._connections: Dict[str, Set[WebSocket]] = {}
        self._redis_pubsub = None
        self._listen_task = None

    async def _get_redis_async(self):
        try:
            import aioredis
            from app.config import settings
            return await aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception:
            return None

    async def connect(self, websocket: WebSocket, channel: str):
        await websocket.accept()
        if channel not in self._connections:
            self._connections[channel] = set()
        self._connections[channel].add(websocket)
        logger.info(f"WS connected to channel={channel} (total={len(self._connections[channel])})")

    def disconnect(self, websocket: WebSocket, channel: str):
        if channel in self._connections:
            self._connections[channel].discard(websocket)
            if not self._connections[channel]:
                del self._connections[channel]
        logger.info(f"WS disconnected from channel={channel}")

    async def send_to_channel(self, channel: str, message: dict):
        """
        Broadcast a message to all connections on this channel (local).
        Also publishes to Redis so other server instances receive it.
        """
        data = json.dumps(message)

        # Local broadcast
        dead: Set[WebSocket] = set()
        for ws in self._connections.get(channel, set()):
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.disconnect(ws, channel)

        # Redis publish (fire-and-forget)
        asyncio.create_task(self._redis_publish(channel, data))

    async def _redis_publish(self, channel: str, data: str):
        try:
            r = await self._get_redis_async()
            if r:
                await r.publish(f"ws:{channel}", data)
                await r.aclose()
        except Exception as e:
            logger.debug(f"Redis publish failed (degraded mode): {e}")

    async def send_to_user(self, user_id: str, message: dict):
        """Send a direct notification to a specific user's personal channel."""
        await self.send_to_channel(f"user:{user_id}", message)

    async def send_personal(self, websocket: WebSocket, message: dict):
        """Send to a single WebSocket connection."""
        try:
            await websocket.send_text(json.dumps(message))
        except Exception:
            pass

    async def start_redis_subscriber(self):
        """
        Long-running task: subscribes to all ws:* Redis channels and
        forwards messages to local WebSocket connections.
        Allows horizontal scaling (multiple server instances).
        """
        try:
            import aioredis
            from app.config import settings
            r = await aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            pubsub = r.pubsub()
            await pubsub.psubscribe("ws:*")  # wildcard subscription

            async for msg in pubsub.listen():
                if msg["type"] != "pmessage":
                    continue
                # Redis channel name: "ws:transaction:abc" → local channel: "transaction:abc"
                redis_channel: str = msg["channel"]
                local_channel = redis_channel.removeprefix("ws:")
                data_str = msg["data"]

                dead: Set[WebSocket] = set()
                for ws in self._connections.get(local_channel, set()):
                    try:
                        await ws.send_text(data_str)
                    except Exception:
                        dead.add(ws)
                for ws in dead:
                    self.disconnect(ws, local_channel)
        except Exception as e:
            logger.warning(f"Redis subscriber exited: {e}")


# Singleton instance used across the app
manager = ConnectionManager()


def build_chat_message_event(message_obj) -> dict:
    sender = getattr(message_obj, "sender", None)
    sender_name = (
        f"{sender.first_name} {sender.last_name}".strip()
        if sender and hasattr(sender, "first_name")
        else "Unknown"
    )
    content = getattr(message_obj, "content", None) or getattr(message_obj, "message", "")
    return {
        "event": "new_message",
        "data": {
            "id": str(message_obj.id),
            "sender_id": str(message_obj.sender_id),
            "sender_name": sender_name,
            "content": content,
            "message_type": getattr(message_obj, "message_type", "text"),
            "created_at": message_obj.created_at.isoformat(),
            "attachments": getattr(message_obj, "attachments", None) or [],
        },
    }


def build_notification_event(notification_obj) -> dict:
    return {
        "event": "notification",
        "data": {
            "id": str(notification_obj.id),
            "title": notification_obj.title,
            "message": notification_obj.message,
            "type": notification_obj.type,
            "created_at": notification_obj.created_at.isoformat(),
        },
    }


def build_transaction_event(event_type: str, transaction_id: str, payload: dict) -> dict:
    return {
        "event": event_type,
        "transaction_id": transaction_id,
        "data": payload,
    }
