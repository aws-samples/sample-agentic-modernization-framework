"""Neptune failures fall back to the DynamoDB graph."""
import socket
from unittest.mock import patch

from backend.src.api.v1 import graph as graph_api
from backend.src.config import settings


def test_neptune_timeout_falls_back_to_graph_dynamo(monkeypatch):
    monkeypatch.setattr(settings, "graph_backend", "neptune")
    monkeypatch.setattr(settings, "neptune_fallback_to_dynamodb", True)
    want = {"nodes": [{"id": "APP-1"}], "edges": []}
    with patch("backend.src.db.neptune.build_topology", side_effect=socket.timeout("The read operation timed out")), \
         patch("backend.src.db.graph_dynamo.build_topology", return_value=want) as dyn:
        assert graph_api._derived().build_topology() == want
        dyn.assert_called_once()


def test_fallback_disabled_propagates(monkeypatch):
    monkeypatch.setattr(settings, "graph_backend", "neptune")
    monkeypatch.setattr(settings, "neptune_fallback_to_dynamodb", False)
    with patch("backend.src.db.neptune.build_topology", side_effect=socket.timeout("t")):
        try:
            graph_api._derived().build_topology()
        except socket.timeout:
            return
    raise AssertionError("expected the Neptune error to propagate")


def test_neptune_success_does_not_touch_dynamo(monkeypatch):
    monkeypatch.setattr(settings, "graph_backend", "neptune")
    with patch("backend.src.db.neptune.build_topology", return_value={"nodes": [], "edges": []}), \
         patch("backend.src.db.graph_dynamo.build_topology") as dyn:
        graph_api._derived().build_topology()
        dyn.assert_not_called()


def test_neptune_uses_its_own_timeout():
    assert settings.neptune_query_timeout_seconds == 60
    src = open("backend/src/db/neptune.py").read()
    assert "timeout=settings.neptune_query_timeout_seconds" in src
