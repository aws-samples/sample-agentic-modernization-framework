"""
Graph API — dependency analysis and wave-planning insights.

Two interchangeable backends behind one contract:

  • graph_backend = "neptune" → Amazon Neptune (db/neptune.py: openCypher + IAM auth, a
    projection of the DynamoDB table re-synced on a TTL; POST /graph/sync forces it).
  • otherwise (the default)   → derived from the DynamoDB single
    table, which already holds the discovery connection data.

Both return the identical response shape, so callers and the frontend do not know or care
which backend answered.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from backend.src.auth.rbac import Role, require_role
from backend.src.core.models import ApiResponse, error_response, internal_error
from backend.src.db import graph_dynamo

logger = logging.getLogger(__name__)
router = APIRouter()


def _derived():
    """The graph backend module: Neptune when configured, else the DynamoDB derivation.
    Both expose build_topology / build_wave_insights."""
    from backend.src.config import settings

    if settings.graph_backend.lower() == "neptune":
        from backend.src.db import neptune

        return _NeptuneWithFallback(neptune) if settings.neptune_fallback_to_dynamodb else neptune
    return graph_dynamo


class _NeptuneWithFallback:
    """Call the Neptune projection; on failure serve the same function from graph_dynamo.

    Neptune only mirrors the DynamoDB graph (db/neptune.sync_from_dynamo), so the DynamoDB
    derivation is a correct, slower answer, better than a GRAPH_QUERY_FAILED for the UI."""

    def __init__(self, primary):
        self._primary = primary

    def __getattr__(self, name):
        primary_fn = getattr(self._primary, name)
        fallback_fn = getattr(graph_dynamo, name, None)
        if not callable(primary_fn) or fallback_fn is None:
            return primary_fn

        def call(*args, **kwargs):
            try:
                return primary_fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — any Neptune failure falls back
                logger.warning("Neptune %s failed (%s: %s); serving it from graph_dynamo",
                               name, type(exc).__name__, exc)
                return fallback_fn(*args, **kwargs)

        return call


@router.post("/sync")
def sync_graph(user=require_role(Role.ENGINEER)):
    """Re-project the DynamoDB graph into Neptune now (graph_backend=neptune only)."""
    from backend.src.config import settings

    if settings.graph_backend.lower() != "neptune":
        return error_response("Graph sync applies to graph_backend=neptune only.", code="NOT_APPLICABLE")
    try:
        from backend.src.db import neptune

        return ApiResponse(data=neptune.sync_from_dynamo())
    except Exception as exc:
        logger.error("Neptune sync failed: %s", exc)
        return internal_error(exc, code="GRAPH_SYNC_FAILED")


@router.get("/topology")
def get_graph_topology():
    """Full app-to-app topology for dependency graph visualization.

    Returns nodes (applications) and edges (connections) for rendering.
    """
    try:
        return ApiResponse(data=_derived().build_topology())
    except Exception as exc:
        logger.error("Graph topology failed: %s", exc)
        return internal_error(exc, code="GRAPH_QUERY_FAILED")


@router.get("/wave-insights")
def get_wave_insights():
    """Wave planning insights: shared DBs, complexity scores, recommended order."""
    try:
        return ApiResponse(data=_derived().build_wave_insights())
    except Exception as exc:
        logger.error("Wave insights failed: %s", exc)
        return internal_error(exc, code="GRAPH_QUERY_FAILED")
