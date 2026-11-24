"""RBAC on every route (AC1).

The route walk fails when a /api/v1 route has no role dependency, or a mutating
route requires less than ENGINEER. The HTTP tests then prove it end to end with
Cognito-shaped Viewer / Engineer / Admin / no-group users (validation stubbed).
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.src.auth.rbac import Role
from backend.src.config import settings
from backend.src.main import app

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Routes under /api/v1 that deliberately have NO role dependency. Each needs a reason.
NO_ROLE_ALLOWLIST = {
    # Dev-only login (exists only when DEV_AUTH=true; auth middleware skips them).
    "/api/v1/auth/dev-login",
    "/api/v1/auth/dev-user",
}

# Mutating routes allowed below ENGINEER. Each needs a reason. (None today: every
# POST/PUT under /api/v1 writes state or drives the Harness/Step Functions.)
MUTATING_BELOW_ENGINEER_ALLOWLIST: set[tuple[str, str]] = set()

# Routes that must require ADMIN at the route level.
ADMIN_ROUTES = {
    ("POST", "/api/v1/workflow/teardown"),
    ("POST", "/api/v1/skills/upload"),
}


def _min_roles(dependant) -> list[Role]:
    roles = []
    for dep in dependant.dependencies:
        role = getattr(dep.call, "min_role", None)
        if role is not None:
            roles.append(role)
        roles.extend(_min_roles(dep))
    return roles


def _api_routes():
    return [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/v1")]


def test_route_walk_every_api_route_has_a_role_and_mutations_need_engineer():
    routes = _api_routes()
    assert len(routes) > 40  # the walk actually sees the app
    missing, weak = [], []
    for r in routes:
        roles = _min_roles(r.dependant)
        if not roles:
            if r.path not in NO_ROLE_ALLOWLIST:
                missing.append(f"{sorted(r.methods)} {r.path}")
            continue
        for m in r.methods & MUTATING:
            if max(roles) < Role.ENGINEER and (m, r.path) not in MUTATING_BELOW_ENGINEER_ALLOWLIST:
                weak.append(f"{m} {r.path} ({max(roles).name})")
    assert not missing, f"routes without require_role: {missing}"
    assert not weak, f"mutating routes below ENGINEER: {weak}"


def test_admin_routes_require_admin():
    by_key = {(m, r.path): r for r in _api_routes() for m in r.methods}
    for key in ADMIN_ROUTES:
        assert max(_min_roles(by_key[key].dependant)) == Role.ADMIN, key


# ── HTTP checks with Cognito-shaped users ──────────────────────────────────

_GROUPS = {"viewer": ["Viewer"], "engineer": ["Engineer"], "admin": ["Admin"], "nogroup": []}


def _claims(_self, token: str) -> dict:
    if token not in _GROUPS:
        raise ValueError("bad token")
    return {"sub": f"sub-{token}", "username": token, "email": f"{token}@example.com", "groups": _GROUPS[token]}


@pytest.fixture
def cognito_client(monkeypatch):
    """dev_auth off; the middleware's JWT validation returns the user named by the token."""
    monkeypatch.setattr(settings, "dev_auth", False)
    with patch("backend.src.auth.cognito.CognitoJWTValidator.validate_token", _claims), \
         patch("backend.src.auth.cognito.jwks_is_fresh", return_value=True):
        yield TestClient(app, raise_server_exceptions=False)


def _h(who: str) -> dict:
    return {"Authorization": f"Bearer {who}"}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "APP-RBAC", path)


def test_unauthenticated_is_401(cognito_client):
    assert cognito_client.get("/api/v1/portfolio/apps").status_code == 401
    assert cognito_client.post("/api/v1/workflow/start", json={"app_id": "X"}).status_code == 401


def test_viewer_gets_403_on_every_mutating_route(cognito_client):
    checked = 0
    for r in _api_routes():
        for m in sorted(r.methods & MUTATING):
            resp = cognito_client.request(m, _concrete(r.path), json={}, headers=_h("viewer"))
            assert resp.status_code == 403, f"{m} {r.path} → {resp.status_code}"
            assert "Requires" in resp.json()["detail"]
            checked += 1
    assert checked >= 15


def test_no_group_user_is_a_viewer(cognito_client):
    resp = cognito_client.post("/api/v1/workflow/start", json={"app_id": "X"}, headers=_h("nogroup"))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Requires ENGINEER role. Your role: VIEWER"
    # ...but can read
    assert cognito_client.get("/api/v1/system/capabilities", headers=_h("nogroup")).status_code == 200


def test_engineer_cannot_reach_admin_routes(cognito_client):
    for m, path in ADMIN_ROUTES:
        resp = cognito_client.request(m, path, json={}, headers=_h("engineer"))
        assert resp.status_code == 403, path
        assert resp.json()["detail"] == "Requires ADMIN role. Your role: ENGINEER"


def _no_workflow_table():
    t = MagicMock()
    t.get_item.return_value = {}
    return t


def test_approving_the_deploy_gate_needs_admin(cognito_client, monkeypatch):
    from backend.src.config import settings

    monkeypatch.setattr(settings, "deploy_enabled", True)
    body = {"app_id": "APP-RBAC", "step": "execution_review", "approved": True,
            "inputs": {"deploy_commit": "a" * 40}}
    with patch("backend.src.api.v1.workflow.get_table", return_value=_no_workflow_table()) as gt:
        resp = cognito_client.post("/api/v1/workflow/approve", json=body, headers=_h("engineer"))
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Requires ADMIN role. Your role: ENGINEER"
        gt.assert_not_called()  # refused before any token lookup
        # Admin passes RBAC (then: no active workflow → 404)
        assert cognito_client.post("/api/v1/workflow/approve", json=body, headers=_h("admin")).status_code == 404


def test_engineer_may_approve_execution_review_when_deploy_is_disabled(cognito_client, monkeypatch):
    from backend.src.config import settings

    monkeypatch.setattr(settings, "deploy_enabled", False)
    body = {"app_id": "APP-RBAC", "step": "execution_review", "approved": True}  # no deploy_commit needed
    with patch("backend.src.api.v1.workflow.get_table", return_value=_no_workflow_table()):
        assert cognito_client.post("/api/v1/workflow/approve", json=body, headers=_h("engineer")).status_code == 404


def test_engineer_may_approve_other_gates_and_reject_the_deploy_gate(cognito_client):
    with patch("backend.src.api.v1.workflow.get_table", return_value=_no_workflow_table()):
        for body in ({"app_id": "APP-RBAC", "step": "design_review", "approved": True},
                     {"app_id": "APP-RBAC", "step": "execution_review", "approved": False, "comment": "no"}):
            resp = cognito_client.post("/api/v1/workflow/approve", json=body, headers=_h("engineer"))
            assert resp.status_code == 404, body  # past RBAC; no active workflow


def test_viewer_can_read(cognito_client):
    assert cognito_client.get("/api/v1/system/capabilities", headers=_h("viewer")).status_code == 200


def test_health_stays_open(cognito_client):
    assert cognito_client.get("/health").status_code == 200


def test_api_docs_not_served_without_dev_auth(cognito_client):
    # main.py mounts the docs only when DEV_AUTH is on at import (tests import with
    # it on); with it off the middleware no longer exempts them from auth.
    from backend.src.auth.middleware import _skips_auth

    assert not _skips_auth("/docs") and not _skips_auth("/openapi.json")
    assert cognito_client.get("/openapi.json").status_code == 401
