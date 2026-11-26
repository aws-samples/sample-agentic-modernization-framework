"""Cached boto3 clients for the API control plane.

boto3 clients are thread-safe and expensive to build (endpoint + credential
resolution), so routers share one per (service, region) instead of creating
one per request. Every client gets bounded timeouts and standard-mode retries
from Settings. Clients that need a different read timeout (chat / Harness
invoke) or a deliberately fresh client (design.py's stale-credential retry)
keep building their own.
"""

from __future__ import annotations

import threading

import boto3
from botocore.config import Config

from backend.src.config import settings

_lock = threading.Lock()
# (service, region) → (factory, client). The factory is remembered so a replaced
# `boto3.client` (tests patch it) gets a fresh client instead of a stale cached one.
_clients: dict[tuple[str, str], tuple[object, object]] = {}


def client_config() -> Config:
    return Config(
        connect_timeout=settings.aws_connect_timeout_seconds,
        read_timeout=settings.aws_read_timeout_seconds,
        retries={"max_attempts": settings.aws_max_attempts, "mode": "standard"},
    )


def client(service: str, region: str | None = None):
    """Shared boto3 client for `service` in `region` (default: settings.aws_region)."""
    key = (service, region or settings.aws_region)
    factory = boto3.client
    hit = _clients.get(key)
    if hit is not None and hit[0] is factory:
        return hit[1]
    with _lock:
        hit = _clients.get(key)
        if hit is not None and hit[0] is factory:
            return hit[1]
        c = factory(service, region_name=key[1], config=client_config())
        _clients[key] = (factory, c)
        return c


def clear_cache() -> None:
    """Drop cached clients (tests that switch moto contexts / credentials)."""
    with _lock:
        _clients.clear()
