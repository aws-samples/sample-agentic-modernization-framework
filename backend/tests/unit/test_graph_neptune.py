"""Neptune graph backend (db/neptune.py): sync from DynamoDB, freshness, response contracts."""
from __future__ import annotations

import time

import pytest

from backend.src.db import neptune

TOPO = {
    "nodes": [
        {"id": "APP-A", "name": "A", "tier": "tier_1", "strategy": "replatform", "business_unit": "BU",
         "machines": 4, "outbound": 1, "inbound": 0},
        {"id": "APP-B", "name": "B", "tier": "tier_2", "strategy": "rehost", "business_unit": "BU",
         "machines": 1, "outbound": 0, "inbound": 1},
    ],
    "edges": [{"source": "APP-A", "target": "APP-B", "weight": 7, "ports": ["443", "5432"], "types": ["java"]}],
}
SHARED = [{"source": "APP-A", "target": "APP-B", "engine": "postgresql 14", "connections": 2}]


class FakeNeptune:
    """Executes the handful of statements db/neptune.py issues against an in-memory graph."""

    def __init__(self, synced_at=None):
        self.apps, self.edges, self.shares, self.marker = [], [], [], synced_at
        self.statements = []

    def __call__(self, cypher, parameters=None):
        p = parameters or {}
        self.statements.append(cypher.split()[0] + " " + cypher.split()[1])
        if cypher.startswith("MATCH (n) DETACH DELETE"):
            self.apps, self.edges, self.shares, self.marker = [], [], [], None
            return []
        if cypher.startswith("UNWIND $apps"):
            self.apps = list(p["apps"])
            return []
        if cypher.startswith("UNWIND $edges"):
            self.edges = list(p["edges"])
            return []
        if cypher.startswith("UNWIND $shares"):
            self.shares = list(p["shares"])
            return []
        if cypher.startswith("CREATE (:AmfSync"):
            self.marker = p["at"]
            return []
        if cypher.startswith("MATCH (m:AmfSync"):
            return [{"at": self.marker}] if self.marker else []
        if cypher.startswith("MATCH (a:Application) RETURN"):
            return [dict(a) for a in self.apps]
        if "COMMUNICATES_WITH]->(b:Application) RETURN" in cypher:
            return [{"source": e["source"], "target": e["target"], "weight": e["weight"], "ports": e["ports"],
                     "types": e["types"]} for e in self.edges]
        if "SHARES_DATABASE]->(b:Application) RETURN" in cypher:
            return [dict(s) for s in self.shares]
        if cypher.startswith("MATCH (n:Application) RETURN count"):
            return [{"c": len(self.apps)}]
        if cypher.startswith("MATCH ()-[r]->()"):
            return [{"type": "COMMUNICATES_WITH", "c": len(self.edges)}, {"type": "SHARES_DATABASE", "c": len(self.shares)}]
        raise AssertionError(f"unexpected statement: {cypher}")


@pytest.fixture
def fake(monkeypatch):
    f = FakeNeptune()
    monkeypatch.setattr(neptune, "query", f)
    monkeypatch.setattr(neptune.graph_dynamo, "build_topology", lambda: TOPO)
    monkeypatch.setattr(neptune.graph_dynamo, "build_wave_insights", lambda: {"shared_databases": SHARED})
    return f


def test_sync_projects_apps_edges_and_shared_databases(fake):
    counts = neptune.sync_from_dynamo()
    assert counts == {"applications": 2, "communicates_with": 1, "shares_database": 1}
    assert fake.edges[0]["ports"] == "443,5432"  # Neptune: no list-valued properties
    assert fake.statements[0] == "MATCH (n)" and fake.marker


def test_reads_sync_when_the_graph_is_empty_or_stale(fake, monkeypatch):
    monkeypatch.setattr(neptune.settings, "neptune_sync_ttl_seconds", 300)
    topo = neptune.build_topology()  # empty → syncs first
    assert topo["source"] == "neptune" and [n["id"] for n in topo["nodes"]] == ["APP-A", "APP-B"]
    assert topo["edges"][0]["ports"] == ["443", "5432"] and topo["nodes"][0]["outbound"] == 1
    n = sum(1 for s in fake.statements if s.startswith("UNWIND"))
    neptune.build_topology()  # fresh → no re-sync
    assert sum(1 for s in fake.statements if s.startswith("UNWIND")) == n
    fake.marker = int(time.time()) - 301  # stale → re-sync
    neptune.build_topology()
    assert sum(1 for s in fake.statements if s.startswith("UNWIND")) > n


def test_read_contracts_match_the_dynamodb_backend(fake):
    ins = neptune.build_wave_insights()
    assert {c["app_id"] for c in ins["complexity_scores"]} == {"APP-A", "APP-B"}
    a = next(c for c in ins["complexity_scores"] if c["app_id"] == "APP-A")
    assert a["complexity_score"] == round((1 + 1) * 3 * (1 + 4 / 100), 1)
    assert ins["shared_databases"][0]["engine"] == "postgresql 14" and ins["recommended_order"][0]["wave"] == 1


def test_missing_endpoint_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(neptune.settings, "neptune_endpoint", "")
    with pytest.raises(neptune.NeptuneError, match="NEPTUNE_ENDPOINT"):
        neptune.query("RETURN 1")


def test_graph_api_routes_to_neptune_when_configured(fake, monkeypatch):

    from backend.src.api.v1 import graph

    monkeypatch.setattr(graph.graph_dynamo, "build_topology", lambda: TOPO)
    from backend.src.config import settings

    monkeypatch.setattr(settings, "graph_backend", "neptune")
    resp = graph.get_graph_topology()
    assert resp.data["source"] == "neptune"
    monkeypatch.setattr(settings, "graph_backend", "dynamodb")
    assert graph._derived() is graph.graph_dynamo


def test_query_is_sigv4_signed_form_post(monkeypatch):
    import io
    import urllib.parse

    import botocore.credentials

    sent = {}

    def fake_urlopen(req, timeout):
        sent.update(url=req.full_url, data=req.data, headers={k.lower(): v for k, v in req.header_items()},
                    method=req.get_method())
        return io.BytesIO(b'{"results": [{"x": 1}]}')

    monkeypatch.setattr(neptune.settings, "neptune_endpoint", "g.cluster-abc.us-east-1.neptune.amazonaws.com")
    monkeypatch.setattr(neptune.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(neptune.boto3, "Session", lambda: type("S", (), {
        "get_credentials": lambda self: botocore.credentials.Credentials("AKIDEXAMPLE", "secret")})())
    assert neptune.query("MATCH (n) WHERE n.id = $id RETURN n", {"id": "a"}) == [{"x": 1}]
    assert sent["method"] == "POST"
    assert sent["url"] == "https://g.cluster-abc.us-east-1.neptune.amazonaws.com:8182/openCypher"
    auth = sent["headers"]["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256") and "/neptune-db/aws4_request" in auth
    body = urllib.parse.parse_qs(sent["data"].decode())
    assert body["query"] == ["MATCH (n) WHERE n.id = $id RETURN n"] and body["parameters"] == ['{"id": "a"}']


def test_neptune_module_needs_only_the_api_image_dependencies():
    """The hosted API image has no `requests`; the module must import without it."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(neptune.__file__).read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "requests" not in imported
