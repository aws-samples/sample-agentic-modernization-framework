"""
Role-based access control.
Maps Cognito groups to ordered roles and provides a FastAPI dependency
for enforcing minimum role requirements on endpoints.

Groups (from Cognito):
  - Admin: Full access — manage workflows, approve the deploy gate, configure settings
  - Engineer: Migration engineers — submit inputs, approve steps, start/cancel runs
  - Viewer: Read-only access — view dashboards and reports

A user in no known group is a Viewer (least privilege). Authentication (the
middleware) only proves identity; every route also declares the role it needs
: each router is mounted with `require_role(Role.VIEWER)` and
every mutation carries `require_role(Role.ENGINEER)` or higher.
tests/unit/test_rbac_routes.py walks app.routes and fails on a route without one.
"""

from enum import IntEnum

from fastapi import Depends, HTTPException, Request


class Role(IntEnum):
    VIEWER = 0
    ENGINEER = 1
    ADMIN = 2


# Map Cognito group names to roles (case-insensitive lookup)
GROUP_TO_ROLE: dict[str, Role] = {
    "Viewer": Role.VIEWER,
    "viewer": Role.VIEWER,
    "viewers": Role.VIEWER,
    "Engineer": Role.ENGINEER,
    "engineer": Role.ENGINEER,
    "migration-engineers": Role.ENGINEER,
    "Admin": Role.ADMIN,
    "admin": Role.ADMIN,
    "admins": Role.ADMIN,
}


def role_of(user) -> Role:
    """The highest role among the user's groups; no (known) group = VIEWER."""
    groups = getattr(user, "groups", None) or []
    return max((GROUP_TO_ROLE.get(g, Role.VIEWER) for g in groups), default=Role.VIEWER)


def ensure_role(user, minimum_role: Role) -> None:
    """Raise 401/403 unless `user` is authenticated and holds at least `minimum_role`.

    Also used inside handlers for checks that depend on the request body (approving
    the deploy gate, setting an Admin-only app field), after the route dependency ran.
    """
    if not user or not getattr(user, "sub", ""):
        raise HTTPException(status_code=401, detail="Not authenticated")
    current = role_of(user)
    if current < minimum_role:
        raise HTTPException(
            status_code=403,
            detail=f"Requires {minimum_role.name} role. Your role: {current.name}",
        )


def require_role(minimum_role: Role):
    """FastAPI dependency that enforces a minimum role level.

    Usage:
        @router.post("/dangerous-action")
        def dangerous(user=require_role(Role.ADMIN)):
            ...

    or router-wide: ``app.include_router(r, dependencies=[require_role(Role.VIEWER)])``.
    """

    async def dependency(request: Request):
        user = getattr(request.state, "user", None)
        ensure_role(user, minimum_role)
        return user

    # Read by the route-walk test so it can tell which role a route requires.
    dependency.min_role = minimum_role  # type: ignore[attr-defined]
    return Depends(dependency)
