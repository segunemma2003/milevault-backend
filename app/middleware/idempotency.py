"""
Idempotency middleware — prevents duplicate mutations on POST/PUT/DELETE.
Clients send: Idempotency-Key: <uuid>
The response body is cached for 24h; duplicate requests return the cached response.
"""
import json
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from app.services.cache_service import idempotency_check, idempotency_store

logger = logging.getLogger(__name__)

IDEMPOTENT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SKIP_PATHS = {"/api/v1/auth/login", "/api/v1/auth/refresh", "/ws"}


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in IDEMPOTENT_METHODS:
            return await call_next(request)

        # Skip WebSocket and specific paths
        path = request.url.path
        if any(path.startswith(skip) for skip in SKIP_PATHS):
            return await call_next(request)

        key = request.headers.get("Idempotency-Key")
        if not key:
            return await call_next(request)

        # Validate key format (must be UUID-like, 10-128 chars)
        if not (10 <= len(key) <= 128):
            return JSONResponse(
                status_code=422,
                content={
                    "error": "INVALID_IDEMPOTENCY_KEY",
                    "message": "Idempotency-Key must be between 10 and 128 characters.",
                },
            )

        # Check cache
        cached = idempotency_check(key)
        if cached is not None:
            logger.info(f"Idempotency hit for key={key[:8]}***")
            return JSONResponse(
                status_code=cached.get("status_code", 200),
                content=cached.get("body"),
                headers={"X-Idempotency-Replayed": "true"},
            )

        # Execute request
        response = await call_next(request)

        # Cache 2xx responses
        if 200 <= response.status_code < 300:
            body_bytes = b""
            async for chunk in response.body_iterator:
                body_bytes += chunk
            try:
                body = json.loads(body_bytes)
                idempotency_store(key, {"status_code": response.status_code, "body": body})
            except Exception:
                pass

            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        return response
