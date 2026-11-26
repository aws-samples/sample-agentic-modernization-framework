"""
Branch naming for every write AMF makes to an application repository.

Transformation pushes (workbench), and the IaC / CI-CD commits made by the
tool_execution Lambda, all land on ONE branch per app: ``migration/<app_id>``
(lowercased, unsafe characters replaced). Nothing is pushed to the default
branch; a human reviews and merges the migration branch.

The Lambda cannot import this module (it ships only lambda-src/), so it carries
an identical ``migration_branch``; backend/tests/unit/test_journey_exec_transform.py
pins the two together.
"""

from __future__ import annotations

import re

MIGRATION_BRANCH_PREFIX = "migration/"
# Branch names treated as a repo's shared/default line — never pushed to directly.
_REVIEW_BRANCH_RE = re.compile(r"^(?:demo|migration)/[A-Za-z0-9._/-]+$")


def migration_branch(app_id: str) -> str:
    """``migration/<app_id lower>`` — the branch AMF commits generated changes to."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", (app_id or "").lower()).strip("-.") or "app"
    return f"{MIGRATION_BRANCH_PREFIX}{slug}"


def resolve_push_branch(app_id: str, branch: str, source_ref: str = "") -> tuple[str, str]:
    """Map a step's (branch, source_ref) to the branch the worker may push to.

    A review/migration branch (``demo/*`` / ``migration/*``) is kept as is. Any
    other branch is treated as the repo's default line: the worker works on the
    app's migration branch instead, created from ``origin/<branch>`` if absent.
    """
    branch = (branch or "").strip() or "main"
    if _REVIEW_BRANCH_RE.fullmatch(branch):
        return branch, source_ref
    return migration_branch(app_id), source_ref or f"origin/{branch}"
