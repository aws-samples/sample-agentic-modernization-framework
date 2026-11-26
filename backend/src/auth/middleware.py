"""
Authentication middleware for FastAPI.
Extracts Bearer token, validates via Cognito (or dev bypass), and sets request.state.user.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from backend.src.config import settings

logger = logging.getLogger(__name__)

# Paths that skip authentication. The API docs (/docs, /redoc, /openapi.json) are
# only mounted when DEV_AUTH is on (main.py); without it they don't exist and would
# need a token anyway. The dev-auth routes exist only with DEV_AUTH.
SKIP_AUTH_PATHS = frozenset(
    {
        "/health",
        "/api/v1/auth/dev-login",
        "/api/v1/auth/dev-user",
    }
)
DEV_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def _skips_auth(path: str) -> bool:
    return path in SKIP_AUTH_PATHS or (settings.dev_auth and path in DEV_DOCS_PATHS)


@dataclass
class AuthUser:
    """Authenticated user attached to request.state.user."""

    sub: str = ""
    username: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)


class AuthMiddleware(BaseHTTPMiddleware):
    """Extract and validate Bearer tokens on every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip auth for public paths
        if _skips_auth(request.url.path):
            request.state.user = AuthUser()
            return await call_next(request)

        # Dev auth bypass
        if settings.dev_auth:
            from backend.src.auth.dev_user import get_dev_user

            dev = get_dev_user()
            request.state.user = AuthUser(
                sub=dev.sub,
                username=dev.username,
                email=dev.email,
                groups=dev.groups,
            )
            return await call_next(request)

        # Production: validate Cognito JWT
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header.removeprefix("Bearer ").strip()

        try:
            from backend.src.auth.cognito import CognitoJWTValidator, jwks_is_fresh

            validator = CognitoJWTValidator()
            if jwks_is_fresh():
                claims = validator.validate_token(token)  # cached keys: CPU only
            else:
                # A JWKS download is a blocking HTTP call — keep it off the event loop.
                claims = await asyncio.to_thread(validator.validate_token, token)
            request.state.user = AuthUser(
                sub=claims["sub"],
                username=claims["username"],
                email=claims["email"],
                groups=claims["groups"],
            )
        except Exception as exc:
            logger.warning("JWT validation failed: %s", exc)
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        return await call_next(request)
