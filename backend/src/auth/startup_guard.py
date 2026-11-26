"""Hosted-mode auth precondition.

The API must never serve a hosted deployment with the dev-auth bypass (every
request = Admin) or without Cognito configured. Checked at startup; a violation
raises, so uvicorn exits instead of serving an unauthenticated control plane.
"""

from __future__ import annotations

import ipaddress
import os
import sys

_LOOPBACK_NAMES = {"localhost"}


class InsecureAuthConfigError(RuntimeError):
    """The auth configuration is unsafe for this deployment; refuse to start."""


def is_hosted(cfg) -> bool:
    """True for the hosted API (ENABLE_PUBLIC_HOSTING, or the hosted task's MODE)."""
    hosted_modes = {m.strip().lower() for m in str(cfg.hosted_run_modes or "").split(",") if m.strip()}
    return bool(cfg.enable_public_hosting) or str(cfg.run_mode or "").strip().lower() in hosted_modes


def bind_host(argv: list[str] | None = None, environ: dict | None = None) -> str:
    """The host uvicorn binds: `--host X` / `--host=X` on its command line, else $UVICORN_HOST, else
    uvicorn's default 127.0.0.1."""
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return str(environ.get("UVICORN_HOST") or "127.0.0.1")


def is_loopback(host: str) -> bool:
    h = str(host or "").strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def assert_auth_config_safe(cfg, host: str | None = None) -> None:
    """Raise InsecureAuthConfigError when the auth configuration is unsafe.

    - hosted (see is_hosted) ⇒ DEV_AUTH off and Cognito pool + app client set;
    - DEV_AUTH on while a Cognito user pool is configured is a conflicting (and
      likely leaked-from-hosted) configuration — refused everywhere;
    - DEV_AUTH on while the API binds a non-loopback address (0.0.0.0, a LAN IP) —
      every reachable client would be Admin. `host` defaults to bind_host().
    """
    problems: list[str] = []
    if cfg.dev_auth:
        host = bind_host() if host is None else host
        if not is_loopback(host):
            problems.append(f"DEV_AUTH=true while binding non-loopback host {host!r} (use --host 127.0.0.1)")
    cognito_pool = str(cfg.cognito_user_pool_id or "").strip()
    cognito_client = str(cfg.cognito_app_client_id or "").strip()
    if is_hosted(cfg):
        if cfg.dev_auth:
            problems.append("DEV_AUTH=true in hosted mode")
        if not cognito_pool or not cognito_client:
            problems.append("hosted mode needs COGNITO_USER_POOL_ID and COGNITO_APP_CLIENT_ID")
    elif cfg.dev_auth and cognito_pool:
        problems.append("DEV_AUTH=true while COGNITO_USER_POOL_ID is set (unset one of them)")
    if problems:
        raise InsecureAuthConfigError("Refusing to start: " + "; ".join(problems))
