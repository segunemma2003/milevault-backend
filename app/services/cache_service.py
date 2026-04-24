"""
Redis cache service — idempotency keys, session data, rate limit state, pub/sub.
Gracefully degrades to a no-op in-memory dict when Redis is unavailable.
"""
import json
import logging
from typing import Any, Optional, Tuple
from app.config import settings

logger = logging.getLogger(__name__)

_redis_client = None
_memory_cache: dict = {}   # Fallback when Redis is down


def get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                retry_on_timeout=True,
            )
            _redis_client.ping()
        except Exception as e:
            logger.warning(f"Redis unavailable, using in-memory fallback: {e}")
            _redis_client = None
    return _redis_client


def cache_set(key: str, value: Any, ttl: int = 300) -> bool:
    """Store a value with TTL (seconds). Returns True on success."""
    try:
        r = get_redis()
        serialized = json.dumps(value)
        if r:
            return bool(r.setex(key, ttl, serialized))
        _memory_cache[key] = serialized
        return True
    except Exception as e:
        logger.error(f"cache_set failed for key={key}: {e}")
        return False


def cache_get(key: str) -> Optional[Any]:
    """Retrieve a cached value. Returns None if missing or expired."""
    try:
        r = get_redis()
        raw = r.get(key) if r else _memory_cache.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.error(f"cache_get failed for key={key}: {e}")
        return None


def cache_delete(key: str) -> bool:
    try:
        r = get_redis()
        if r:
            return bool(r.delete(key))
        _memory_cache.pop(key, None)
        return True
    except Exception:
        return False


def idempotency_check(key: str) -> Optional[Any]:
    """Returns cached response for an idempotency key if it exists."""
    return cache_get(f"idempotency:{key}")


def idempotency_store(key: str, response: Any) -> None:
    """Store the response body for an idempotency key (24h TTL)."""
    cache_set(f"idempotency:{key}", response, ttl=settings.IDEMPOTENCY_TTL)


def rate_limit_check(identifier: str, limit: int, window: int) -> Tuple[bool, int]:
    """
    Sliding window rate limiter.
    Returns (is_allowed, remaining_requests).
    """
    from time import time
    key = f"rate:{identifier}"
    now = int(time())
    window_start = now - window

    try:
        r = get_redis()
        if r:
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, 0, window_start)
            pipe.zadd(key, {str(now): now})
            pipe.zcard(key)
            pipe.expire(key, window)
            results = pipe.execute()
            count = results[2]
        else:
            count = 1  # Always allow when Redis is down
        remaining = max(0, limit - count)
        return count <= limit, remaining
    except Exception:
        return True, limit   # Fail open


