"""
Dev-only authentication bypass.
When DEV_AUTH=true, all requests are authenticated as a dev user
with full admin permissions. No Cognito required.

NEVER enable in production — controlled by DEV_AUTH env var.
"""

import base64
import json
import time
from dataclasses import dataclass, field

from backend.src.config import settings

DEV_USER_EMAIL = "dev@migration-framework.local"
DEV_USER_SUB = "dev-user-00000000-0000-0000-0000-000000000000"
DEV_USER_USERNAME = "dev-admin"
DEV_USER_GROUPS = [
    "Admin",
    "Engineer",
    "Viewer",
]


@dataclass
class DevUser:
    sub: str = DEV_USER_SUB
    username: str = DEV_USER_USERNAME
    email: str = DEV_USER_EMAIL
    groups: list[str] = field(default_factory=lambda: list(DEV_USER_GROUPS))


def create_dev_token() -> str:
    """Create a fake JWT for the dev user (not cryptographically signed)."""
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "sub": DEV_USER_SUB,
        "cognito:username": DEV_USER_USERNAME,
        "email": DEV_USER_EMAIL,
        "cognito:groups": DEV_USER_GROUPS,
        "iat": int(time.time()),
        "exp": int(time.time()) + settings.dev_token_ttl_seconds,
        "iss": "dev-local",
        "token_use": "access",  # nosec B105 — claim value, not a secret
    }

    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{b64(header)}.{b64(payload)}."


def get_dev_user() -> DevUser:
    """Return the dev user instance."""
    return DevUser()
