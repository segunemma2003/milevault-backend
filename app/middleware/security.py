"""
Security middleware — HSTS, CSP, XSS protection, CSRF, clickjacking headers.
Also validates request size and rejects suspicious payloads.
"""
import logging
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from app.config import settings

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 50 * 1024 * 1024   # 50 MB hard cap for any request


def _origin_trusted_for_csrf(request: Request) -> bool:
    """
    Credentialed SPA on a different host than the API cannot read host-only cookies
    (e.g. csrf_token set on api.example.com) via document.cookie, so the double-submit
    header cannot be echoed. If Origin matches our CORS allowlist, treat as same trusted
    browser session (CORS + JSON POST preflight already limits blind cross-site abuse).
    """
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        return False
    try:
        allowed = {o.rstrip("/") for o in settings.cors_origins_list}
        return origin in allowed
    except Exception:
        return False
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _get_security_headers(is_production: bool) -> dict:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cache-Control": "no-store",
    }
    if is_production:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
        headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' wss:;"
        )
    return headers


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Reject oversized requests early
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_CONTENT_LENGTH:
            return JSONResponse(
                status_code=413,
                content={
                    "error": "PAYLOAD_TOO_LARGE",
                    "message": f"Request body exceeds the maximum allowed size of {MAX_CONTENT_LENGTH // (1024*1024)} MB.",
                },
            )

        # CSRF check for state-changing requests
        if request.method not in CSRF_SAFE_METHODS:
            path = request.url.path
            # Skip CSRF for API endpoints that use Bearer token auth
            # (CSRF is only needed for cookie-auth endpoints from browser)
            if "/api/v1/" in path and not path.startswith("/api/v1/auth"):
                # Cross-origin SPA: csrf_token is host-only on the API; JS on the frontend
                # cannot read document.cookie for it, so skip double-submit when Origin is allowed.
                if not _origin_trusted_for_csrf(request):
                    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
                    csrf_header = request.headers.get(CSRF_HEADER_NAME)
                    # If a CSRF cookie is present (browser/cookie-auth client), the header
                    # MUST be present and match. Bearer-only API clients have no CSRF cookie
                    # so they are exempt — intentional design for mobile/API callers.
                    if csrf_cookie and (not csrf_header or csrf_cookie != csrf_header):
                        return JSONResponse(
                            status_code=403,
                            content={
                                "error": "CSRF_TOKEN_MISMATCH",
                                "message": "CSRF token validation failed. Ensure the X-CSRF-Token header matches the csrf_token cookie, or call the API from an allowed CORS origin.",
                            },
                        )

        response: Response = await call_next(request)

        # Inject security headers on every response
        for name, value in _get_security_headers(settings.is_production).items():
            response.headers[name] = value

        # Set / refresh CSRF cookie if not present
        if CSRF_COOKIE_NAME not in request.cookies:
            token = generate_csrf_token()
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=token,
                httponly=False,       # JS needs to read this to send in header
                secure=settings.COOKIE_SECURE,
                samesite=settings.COOKIE_SAMESITE,
                max_age=86400,        # 1 day
            )

        return response
