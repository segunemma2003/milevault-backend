"""
Per-endpoint sliding-window rate limiter backed by Redis.
Limits keyed on (IP + path) for anonymous, (user_id + path) for authenticated.
"""
import logging
from time import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

LIMITS: dict = {
    "/api/v1/auth/login": (10, 60),       # 10 req/min
    "/api/v1/auth/register": (5, 60),     # 5 reg/min
    "/api/v1/auth/refresh": (20, 60),
    "/api/v1/uploads": (20, 60),
    "/api/v1/kyc": (10, 60),
    "default": (120, 60),                  # 120 req/min for everything else
}


def _get_limit(path: str):
    for prefix, limit in LIMITS.items():
        if prefix != "default" and path.startswith(prefix):
            return limit
    return LIMITS["default"]


def _client_key(request: Request) -> str:
    # Prefer authenticated user ID, fallback to IP
    user = getattr(request.state, "user_id", None)
    if user:
        return f"user:{user}"
    forwarded = request.headers.get("X-Forwarded-For")
    ip = forwarded.split(",")[0].strip() if forwarded else request.client.host
    return f"ip:{ip}"


def _sliding_window_check(key: str, limit: int, window: int) -> tuple[bool, int, int]:
    """Returns (allowed, remaining, retry_after_seconds)."""
    try:
        from app.services.cache_service import get_redis
        r = get_redis()
        if not r:
            return True, limit, 0

        now = int(time())
        window_start = now - window
        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {str(now): now})
        pipe.zcard(key)
        pipe.expire(key, window)
        results = pipe.execute()
        count = results[2]
        remaining = max(0, limit - count)
        retry_after = window if count > limit else 0
        return count <= limit, remaining, retry_after
    except Exception:
        return True, limit, 0   # Fail open — never block due to Redis outage


class RateLimiterMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip WebSocket and static
        if path.startswith("/ws") or path.startswith("/uploads"):
            return await call_next(request)

        limit, window = _get_limit(path)
        client_key = _client_key(request)
        rate_key = f"rate:{client_key}:{path.split('?')[0]}"

        allowed, remaining, retry_after = _sliding_window_check(rate_key, limit, window)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RATE_LIMIT_EXCEEDED",
                    "message": f"Too many requests. You are allowed {limit} requests per {window} seconds. Please slow down.",
                    "retry_after_seconds": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
