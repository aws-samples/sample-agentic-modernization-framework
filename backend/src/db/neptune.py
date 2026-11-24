"""
Amazon Neptune graph backend (graph_backend="neptune") — openCypher over HTTPS, IAM auth.

DynamoDB stays the source of truth (discovery writes apps, machines and machine→machine
connections there); Neptune holds a projection of it at app level:

    (:Application {app_id, app_name, tier, strategy, business_unit, machines})
    (:Application)-[:COMMUNICATES_WITH {connection_count, ports, types}]->(:Application)
    (:Application)-[:SHARES_DATABASE {engine, connections}]->(:Application)
    (:AmfSync {key: "graph", synced_at})             ← freshness marker

Neptune openCypher has no list-valued properties, so `ports`/`types` are stored as
comma-separated strings and split on read. Degrees, complexity scores and wave order
are computed here, so every read returns exactly the contract graph_dynamo returns
(the frontend can't tell the backends apart), with "source": "neptune".

Freshness: a read re-syncs from DynamoDB when the marker is missing or older than
settings.neptune_sync_ttl_seconds; POST /graph/sync forces one.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from backend.src.config import settings
from backend.src.db import graph_dynamo
from backend.src.db.graph_dynamo import wave_recommendation

logger = logging.getLogger(__name__)

_SYNC_KEY = "graph"


class NeptuneError(RuntimeError):
    pass


# ── transport ────────────────────────────────────────────────────────────────

_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _url() -> str:
    """https://<endpoint>:<port>/openCypher — scheme fixed, host validated (SSRF guard)."""
    host = (settings.neptune_endpoint or "").strip().lower()
    if not host:
        raise NeptuneError("graph_backend=neptune but NEPTUNE_ENDPOINT is not set")
    suffix = (settings.neptune_allowed_host_suffix or "").lower()
    if not _HOSTNAME_RE.match(host) or (suffix and not host.endswith(suffix)):
        raise NeptuneError(f"NEPTUNE_ENDPOINT must be a bare *{suffix} hostname")
    return f"https://{host}:{int(settings.neptune_port)}/openCypher"


def query(cypher: str, parameters: dict[str, Any] | None = None) -> list[dict]:
    """Run one openCypher statement; returns the result rows."""
    url = _url()
    body = {"query": cypher}
    if parameters:
        body["parameters"] = json.dumps(parameters)
    req = AWSRequest(method="POST", url=url, data=urllib.parse.urlencode(body),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    creds = boto3.Session().get_credentials()
    if creds is None:
        raise NeptuneError("no AWS credentials for Neptune IAM auth")
    SigV4Auth(creds.get_frozen_credentials(), "neptune-db", settings.aws_region).add_auth(req)
    prepared = req.prepare()
    body_bytes = prepared.body if isinstance(prepared.body, bytes) else str(prepared.body or "").encode()
    # stdlib HTTP: `requests` is not in the API image (2026-10-27: "No module named 'requests'").
    http_req = urllib.request.Request(prepared.url, data=body_bytes, method="POST", headers=dict(prepared.headers))
    try:
        # URL is built only by _url(): fixed https scheme + hostname validated against neptune_allowed_host_suffix.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(http_req, timeout=settings.neptune_query_timeout_seconds) as resp:  # noqa: S310  # nosec B310 — https + allowlisted host (_url)
            return json.loads(resp.read() or b"{}").get("results") or []
    except urllib.error.HTTPError as e:
        raise NeptuneError(f"Neptune {e.code}: {e.read()[:300]!r}") from e


# ── sync (DynamoDB → Neptune) ────────────────────────────────────────────────

def _csv(values) -> str:
    return ",".join(str(v) for v in sorted(values or []))


def _split(value) -> list[str]:
    return [v for v in str(value or "").split(",") if v]


def sync_from_dynamo() -> dict[str, int]:
    """Replace the Neptune projection with the current DynamoDB graph (bounded estate)."""
    topo = graph_dynamo.build_topology()
    shared = graph_dynamo.build_wave_insights()["shared_databases"]
    apps = [{"app_id": n["id"], "app_name": n["name"], "tier": n["tier"], "strategy": n["strategy"],
             "business_unit": n["business_unit"], "machines": int(n["machines"] or 0)} for n in topo["nodes"]]
    edges = [{"source": e["source"], "target": e["target"], "weight": int(e["weight"] or 1),
              "ports": _csv(e["ports"]), "types": _csv(e["types"])} for e in topo["edges"]]
    shares = [{"source": s["source"], "target": s["target"], "engine": s.get("engine") or "",
               "connections": int(s.get("connections") or 0)} for s in shared]

    query("MATCH (n) DETACH DELETE n")
    if apps:
        query("UNWIND $apps AS a CREATE (:Application {app_id: a.app_id, app_name: a.app_name, tier: a.tier, "
              "strategy: a.strategy, business_unit: a.business_unit, machines: a.machines})", {"apps": apps})
    if edges:
        query("UNWIND $edges AS e MATCH (a:Application {app_id: e.source}), (b:Application {app_id: e.target}) "
              "CREATE (a)-[:COMMUNICATES_WITH {connection_count: e.weight, ports: e.ports, types: e.types}]->(b)",
              {"edges": edges})
    if shares:
        query("UNWIND $shares AS s MATCH (a:Application {app_id: s.source}), (b:Application {app_id: s.target}) "
              "CREATE (a)-[:SHARES_DATABASE {engine: s.engine, connections: s.connections}]->(b)",
              {"shares": shares})
    query("CREATE (:AmfSync {key: $key, synced_at: $at})", {"key": _SYNC_KEY, "at": int(time.time())})
    counts = {"applications": len(apps), "communicates_with": len(edges), "shares_database": len(shares)}
    logger.info("Neptune graph synced from DynamoDB: %s", counts)
    return counts


def _ensure_fresh() -> None:
    rows = query("MATCH (m:AmfSync {key: $key}) RETURN m.synced_at AS at", {"key": _SYNC_KEY})
    at = max((int(r.get("at") or 0) for r in rows), default=0)
    if time.time() - at > settings.neptune_sync_ttl_seconds:
        sync_from_dynamo()


# ── reads (graph_dynamo contracts) ───────────────────────────────────────────

def _load() -> tuple[list[dict], list[dict]]:
    _ensure_fresh()
    apps = query("MATCH (a:Application) RETURN a.app_id AS app_id, a.app_name AS app_name, a.tier AS tier, "
                 "a.strategy AS strategy, a.business_unit AS business_unit, a.machines AS machines")
    edges = query("MATCH (a:Application)-[r:COMMUNICATES_WITH]->(b:Application) RETURN a.app_id AS source, "
                  "b.app_id AS target, r.connection_count AS weight, r.ports AS ports, r.types AS types")
    return apps, edges


def _degrees(edges: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    outbound: dict[str, int] = defaultdict(int)
    inbound: dict[str, int] = defaultdict(int)
    for e in edges:
        outbound[e["source"]] += 1
        inbound[e["target"]] += 1
    return outbound, inbound


def build_topology() -> dict[str, Any]:
    apps, edges = _load()
    outbound, inbound = _degrees(edges)
    nodes = [{"id": a["app_id"], "name": a.get("app_name") or a["app_id"], "tier": a.get("tier") or "tier_2",
              "strategy": a.get("strategy") or "", "business_unit": a.get("business_unit") or "",
              "machines": a.get("machines") or 0, "outbound": outbound.get(a["app_id"], 0),
              "inbound": inbound.get(a["app_id"], 0)} for a in apps]
    nodes.sort(key=lambda n: n["outbound"] + n["inbound"], reverse=True)
    out_edges = [{"source": e["source"], "target": e["target"], "weight": e.get("weight") or 1,
                  "ports": _split(e.get("ports")), "types": _split(e.get("types"))} for e in edges]
    out_edges.sort(key=lambda e: e["weight"], reverse=True)
    return {"nodes": nodes, "edges": out_edges, "source": "neptune"}


def build_wave_insights() -> dict[str, Any]:
    apps, edges = _load()
    outbound, inbound = _degrees(edges)
    names = {a["app_id"]: a.get("app_name") or a["app_id"] for a in apps}
    complexity = []
    for a in apps:
        tier = a.get("tier") or "tier_2"
        machines = a.get("machines") or 0
        out_deg, in_deg = outbound.get(a["app_id"], 0), inbound.get(a["app_id"], 0)
        score = round((1 + out_deg + in_deg) * graph_dynamo._tier_weight(tier) * (1 + machines / 100), 1)
        complexity.append({"app_id": a["app_id"], "app_name": names[a["app_id"]], "tier": tier,
                           "strategy": a.get("strategy") or "", "machines": machines, "outbound": out_deg,
                           "inbound": in_deg, "complexity_score": score,
                           "wave_recommendation": wave_recommendation(score)})
    complexity.sort(key=lambda c: c["complexity_score"], reverse=True)

    recommended, bucket, wave = [], [], 1
    for c in sorted(complexity, key=lambda x: x["complexity_score"]):
        bucket.append(c["app_id"])
        if len(bucket) >= 5:
            recommended.append({"wave": wave, "apps": bucket})
            bucket, wave = [], wave + 1
    if bucket:
        recommended.append({"wave": wave, "apps": bucket})

    shares = query("MATCH (a:Application)-[s:SHARES_DATABASE]->(b:Application) RETURN a.app_id AS source, "
                   "b.app_id AS target, s.engine AS engine, s.connections AS connections")
    shared = [{"source": s["source"], "source_name": names.get(s["source"], s["source"]), "target": s["target"],
               "target_name": names.get(s["target"], s["target"]), "connections": s.get("connections") or 0,
               "db_ports": [], "engine": s.get("engine") or "",
               "constraint": f"Both depend on {s.get('engine')} — migrate together or target the shared engine last"}
              for s in shares]
    shared.sort(key=lambda s: s["connections"], reverse=True)
    return {"shared_databases": shared, "complexity_scores": complexity, "recommended_order": recommended,
            "source": "neptune"}
