"""GET /workbench/transformations: live ATX list, managed-only by default, cached."""
from __future__ import annotations

import asyncio

import pytest

from backend.src.api.v1 import workbench

LIVE = [
    {"name": "AWS/java-version-upgrade", "description": "Upgrade Java", "version": "2025-11-11"},
    {"name": "AWS/security-issue-fixer", "description": "Fix findings", "version": "2026-08-06"},
    {"name": "AWS/internal-python-upgrade", "description": "Amazon-internal", "version": "1"},
    {"name": "springboot4-security-testing", "description": "user TD", "version": "x", "owner": "someone"},
]


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setitem(workbench._TD_CACHE, "items", [])
    monkeypatch.setitem(workbench._TD_CACHE, "at", 0.0)


def _call(**kw):
    return asyncio.run(workbench.list_transformations(**kw))


def test_managed_only_by_default(monkeypatch):
    monkeypatch.setattr(workbench, "_atx_list", lambda: LIVE)
    names = [t["name"] for t in _call().data]
    assert names == ["AWS/java-version-upgrade", "AWS/security-issue-fixer"]


def test_scope_all_returns_user_published_definitions(monkeypatch):
    monkeypatch.setattr(workbench, "_atx_list", lambda: LIVE)
    assert len(_call(scope="all").data) == 4


def test_list_is_cached_and_survives_a_failed_refresh(monkeypatch):
    calls = []

    def atx():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("ATX CLI timed out")
        return LIVE

    monkeypatch.setattr(workbench, "_atx_list", atx)
    monkeypatch.setattr(workbench.settings, "atx_list_min_refresh_seconds", 0)
    assert len(_call().data) == 2
    assert len(_call().data) == 2 and len(calls) == 1          # served from cache
    assert len(_call(refresh=True).data) == 2 and len(calls) == 2  # refresh failed → cached list


def test_refresh_is_throttled(monkeypatch):
    """refresh=true re-runs the slow CLI at most once per atx_list_min_refresh_seconds."""
    calls = []
    monkeypatch.setattr(workbench, "_atx_list", lambda: calls.append(1) or LIVE)
    monkeypatch.setattr(workbench.settings, "atx_list_min_refresh_seconds", 60)
    _call()
    _call(refresh=True)
    _call(refresh=True)
    assert len(calls) == 1  # both refreshes inside the window were served from cache
    workbench._TD_CACHE["at"] -= 61
    _call(refresh=True)
    assert len(calls) == 2


def test_cli_failure_without_a_cache_is_an_error(monkeypatch):
    def atx():
        raise RuntimeError("ATX CLI not installed")

    monkeypatch.setattr(workbench, "_atx_list", atx)
    resp = _call()
    assert resp.status == "error" and "not installed" in resp.data["message"]
