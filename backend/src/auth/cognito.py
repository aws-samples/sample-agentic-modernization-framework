"""
Cognito JWT validation with JWKS caching.
Validates access tokens issued by the configured Cognito User Pool.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx
from jose import JWTError, jwk, jwt

from backend.src.config import settings

logger = logging.getLogger(__name__)

# JWKS cache: {keys: [...], fetched_at: float}
_jwks_cache: dict | None = None
_JWKS_CACHE_TTL = settings.jwks_cache_ttl_seconds
# When an unknown kid last forced a refetch: a token with a bogus kid must not turn
# every request into a JWKS download (rate-limited to jwks_min_refresh_seconds).
_last_forced_refresh: float = 0.0
_jwks_lock = threading.Lock()

# The JWKS URL is built from these two settings only; validating their shape pins the fetch to the
# configured pool on the regional Cognito host (no host/path injection via a mis-set env var).
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_POOL_ID_RE = re.compile(r"^(?P<region>[a-z]{2}(-[a-z]+)+-\d)_[A-Za-z0-9]+$")
_ALG = "RS256"


def jwks_is_fresh() -> bool:
    """True when validation can run from the cached JWKS (no network call needed)."""
    return bool(_jwks_cache and (time.time() - _jwks_cache["fetched_at"]) < _JWKS_CACHE_TTL)


def reset_jwks_cache() -> None:
    """Forget the cached JWKS and the refresh rate limit (tests)."""
    global _jwks_cache, _last_forced_refresh
    with _jwks_lock:
        _jwks_cache = None
        _last_forced_refresh = 0.0


class CognitoJWTValidator:
    """Validates JWTs issued by AWS Cognito."""

    def __init__(self):
        self.region = settings.aws_region
        self.user_pool_id = settings.cognito_user_pool_id
        self.app_client_id = settings.cognito_app_client_id
        self.issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"
        self.jwks_url = f"{self.issuer}/.well-known/jwks.json"

    def fetch_jwks(self) -> dict:
        """Fetch JWKS from Cognito with in-memory caching."""
        global _jwks_cache

        now = time.time()
        if _jwks_cache and (now - _jwks_cache["fetched_at"]) < _JWKS_CACHE_TTL:
            return _jwks_cache
        return self._download_jwks()

    def _check_pool(self) -> None:
        """Fail closed unless region + pool id are well-formed and agree (JWKS/issuer host pinning)."""
        m = _POOL_ID_RE.match(self.user_pool_id or "")
        if not _REGION_RE.match(self.region or "") or not m or m.group("region") != self.region:
            raise JWTError("Cognito user pool id / region are not a valid pair")

    def _download_jwks(self) -> dict:
        global _jwks_cache
        self._check_pool()
        with _jwks_lock:
            logger.info("Fetching JWKS from %s", self.jwks_url)
            resp = httpx.get(self.jwks_url, timeout=settings.http_timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            _jwks_cache = {"keys": data["keys"], "fetched_at": time.time()}
            return _jwks_cache

    def _get_signing_key(self, token: str) -> dict:
        """Extract the signing key from JWKS that matches the token's kid."""
        headers = jwt.get_unverified_headers(token)
        kid = headers.get("kid")
        if not kid:
            raise JWTError("Token missing 'kid' header")

        jwks = self.fetch_jwks()
        for key in jwks["keys"]:
            if key["kid"] == kid:
                return key

        # Key not found — Cognito may have rotated its keys: refetch once, but at
        # most once per jwks_min_refresh_seconds across all requests.
        global _last_forced_refresh
        now = time.time()
        with _jwks_lock:
            allowed = now - _last_forced_refresh >= settings.jwks_min_refresh_seconds
            if allowed:
                _last_forced_refresh = now
        if allowed:
            jwks = self._download_jwks()
            for key in jwks["keys"]:
                if key["kid"] == kid:
                    return key

        raise JWTError(f"Unable to find signing key for kid: {kid}")

    def validate_token(self, token: str) -> dict:
        """
        Validate a Cognito JWT and return the claims.
        Extracts: sub, username, email, cognito:groups.

        Beyond signature/expiry/issuer, the token must be meant for THIS app
       : an access token's `client_id`, or an ID token's
        `aud`, must equal the configured app client id, and `token_use` must be
        `access` or `id`. Another app client of the same pool is refused. Fails
        closed when no app client id is configured.
        """
        if not self.app_client_id or not self.user_pool_id:
            raise JWTError("Cognito user pool / app client id not configured")
        self._check_pool()
        # Pin the algorithm before touching keys: Cognito signs with RS256 only (no "none"/HS* confusion).
        if jwt.get_unverified_header(token).get("alg") != _ALG:
            raise JWTError("Token alg must be RS256")

        signing_key = self._get_signing_key(token)
        if signing_key.get("kty") != "RSA" or signing_key.get("alg", _ALG) != _ALG:
            raise JWTError("JWKS key is not an RS256 RSA key")
        public_key = jwk.construct(signing_key, _ALG)

        # `aud` is checked below per token_use: access tokens carry `client_id`
        # instead, and python-jose silently accepts a token without `aud`.
        claims = jwt.decode(
            token,
            public_key.to_dict(),
            algorithms=[_ALG],
            issuer=self.issuer,
            options={"verify_at_hash": False, "verify_aud": False, "require_exp": True,
                     "require_iat": False, "require_sub": True},
        )
        check_token_audience(claims, self.app_client_id)

        return {
            "sub": claims.get("sub", ""),
            "username": claims.get("cognito:username", claims.get("username", "")),
            "email": claims.get("email", ""),
            "groups": claims.get("cognito:groups", []),
        }


def check_token_audience(claims: dict, app_client_id: str) -> None:
    """Raise JWTError unless the claims are an access/ID token for `app_client_id`."""
    token_use = claims.get("token_use")
    if token_use == "access":  # nosec B105 — Cognito token_use claim value, not a secret
        audience = claims.get("client_id")
    elif token_use == "id":  # nosec B105 — Cognito token_use claim value, not a secret
        aud = claims.get("aud")
        audience = aud[0] if isinstance(aud, list) and len(aud) == 1 else aud
    else:
        raise JWTError(f"Unsupported token_use: {token_use!r}")
    if not app_client_id or not isinstance(audience, str) or audience != app_client_id:
        raise JWTError("Token was not issued for this app client")
    if not claims.get("sub"):
        raise JWTError("Token missing 'sub'")
