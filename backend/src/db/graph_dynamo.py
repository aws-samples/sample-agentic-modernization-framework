"""
Graph derivation from the DynamoDB single table — no graph database required.

Wave planning and dependency analysis need no graph store: the connection data
needed to answer those questions is already in the portfolio table, written by
discovery ingestion:

    APP#<id>      SK=META           → app metadata + machine_ids[]
    MACHINE#<id>  SK=META           → machine metadata
    MACHINE#<src> SK=CONN#<hash>    → machine→machine network edge
    MACHINE#<id>  SK=SW#<key>       → installed software, category=database

Applications own machines via the `machine_ids` list on their META record
(machines themselves carry no app_id), so app-level edges are produced by
reverse-mapping machines to apps and aggregating the machine-level edges
between them.

Every function here returns exactly the shape its Neptune counterpart in
db/neptune.py returns, so callers and the frontend are unaffected by which
backend served the request.

Query cost is bounded: apps and machines come from GSI1 "list all of a type"
queries, and edges are read with one bounded PK query per app-owned machine —
never a full-table scan.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from boto3.dynamodb.conditions import Key as KeyCond

from backend.src.config import settings
from backend.src.db import accessors, keys
from backend.src.db.helpers import from_dynamo, get_table

logger = logging.getLogger(__name__)

# Ports that indicate a database relationship rather than ordinary app traffic.
DB_PORTS = {"1521", "3306", "5432", "1433", "27017", "5439"}

# Cap the machines we walk for edges so a large estate cannot make this
# unbounded. Ordered by app so the hero apps are always represented.
_MAX_MACHINES_SCANNED = settings.graph_max_machines


def wave_recommendation(score: float) -> str:
    """Bucket a dependency-complexity score into pilot / early / later."""
    if score <= settings.wave_pilot_max_score:
        return "pilot"
    return "early" if score <= settings.wave_early_max_score else "later"


def _tier_weight(tier: str | None) -> int:
    return 3 if tier == "tier_1" else (2 if tier == "tier_2" else 1)


def _machine_to_app(apps: list[dict]) -> dict[str, str]:
    """Reverse the app→machine_ids relation into machine_id→app_id."""
    mapping: dict[str, str] = {}
    for app in apps:
        app_id = app.get("app_id") or keys.app_id_from_pk(app.get("PK", ""))
        for machine_id in app.get("machine_ids") or []:
            mapping[str(machine_id)] = app_id
    return mapping


def _machine_children(machine_id: str, sk_prefix: str) -> list[dict]:
    """Bounded query for one machine's child records (CONN# or SW#)."""
    resp = get_table().query(
        KeyConditionExpression=KeyCond("PK").eq(f"MACHINE#{machine_id}")
        & KeyCond("SK").begins_with(sk_prefix)
    )
    return from_dynamo(resp.get("Items", []))


def _aggregate_app_edges(apps: list[dict]) -> dict[tuple[str, str], dict[str, Any]]:
    """Aggregate machine→machine connections into app→app edges.

    Self-edges (both machines in the same application) are internal traffic and
    are dropped — they are not migration dependencies.
    """
    m2a = _machine_to_app(apps)
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    scanned = 0

    for machine_id, source_app in m2a.items():
        if scanned >= _MAX_MACHINES_SCANNED:
            logger.warning("Edge derivation truncated at %d machines", _MAX_MACHINES_SCANNED)
            break
        scanned += 1
        for conn in _machine_children(machine_id, "CONN#"):
            target_app = m2a.get(str(conn.get("target_machine_id", "")))
            if not target_app or target_app == source_app:
                continue
            key = (source_app, target_app)
            edge = edges.setdefault(key, {"weight": 0, "ports": set(), "processes": set()})
            try:
                edge["weight"] += int(conn.get("connection_count") or 1)
            except (TypeError, ValueError):
                edge["weight"] += 1
            port = str(conn.get("target_port") or "").strip()
            if port:
                edge["ports"].add(port)
            proto = str(conn.get("protocol") or "").strip()
            if proto:
                edge["processes"].add(proto)
    _add_declared_edges(apps, edges)
    return edges


def _add_declared_edges(apps: list[dict], edges: dict[tuple[str, str], dict[str, Any]]) -> None:
    """Merge META depends_on (user-declared app→app links) into the edges.

    A declared link with no discovered traffic gets weight 1; every declared pair carries the
    "declared" type so the UI can draw it differently. Links to apps outside the estate are skipped."""
    known = {app.get("app_id") or keys.app_id_from_pk(app.get("PK", "")) for app in apps}
    for app in apps:
        source = app.get("app_id") or keys.app_id_from_pk(app.get("PK", ""))
        for target in app.get("depends_on") or []:
            target = str(target)
            if target == source or target not in known:
                continue
            edge = edges.setdefault((source, target), {"weight": 0, "ports": set(), "processes": set()})
            edge["weight"] = max(edge["weight"], 1)
            edge["processes"].add("declared")


def _node_degrees(
    edges: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    outbound: dict[str, int] = defaultdict(int)
    inbound: dict[str, int] = defaultdict(int)
    for source, target in edges:
        outbound[source] += 1
        inbound[target] += 1
    return outbound, inbound


def build_topology() -> dict[str, Any]:
    """App-to-app topology: the {nodes, edges} contract of /graph/topology."""
    apps = accessors.list_all_applications()
    edges_raw = _aggregate_app_edges(apps)
    outbound, inbound = _node_degrees(edges_raw)

    nodes = []
    for app in apps:
        app_id = app.get("app_id") or keys.app_id_from_pk(app.get("PK", ""))
        nodes.append(
            {
                "id": app_id,
                "name": app.get("app_name") or app_id,
                "tier": app.get("tier") or "tier_2",
                "strategy": app.get("migration_strategy") or "",
                "business_unit": app.get("business_unit") or "",
                "machines": app.get("machine_count") or len(app.get("machine_ids") or []),
                "outbound": outbound.get(app_id, 0),
                "inbound": inbound.get(app_id, 0),
            }
        )
    nodes.sort(key=lambda n: n["outbound"] + n["inbound"], reverse=True)

    edges = [
        {
            "source": source,
            "target": target,
            "weight": data["weight"],
            "ports": sorted(data["ports"]),
            "types": sorted(data["processes"]),
        }
        for (source, target), data in edges_raw.items()
    ]
    edges.sort(key=lambda e: e["weight"], reverse=True)

    return {"nodes": nodes, "edges": edges, "source": "dynamodb"}


def _shared_databases(apps: list[dict]) -> list[dict[str, Any]]:
    """Applications coupled by running the same database engine and version.

    Derived from SW# records rather than from ports, which is a stronger signal
    than port-based inference: two apps on `postgresql 14` share a platform
    dependency even when they never talk to each other directly.
    """
    m2a = _machine_to_app(apps)
    names = {
        (a.get("app_id") or keys.app_id_from_pk(a.get("PK", ""))): (a.get("app_name") or "")
        for a in apps
    }

    # engine+version → {app_id: [instance names]}
    by_engine: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for machine_id, app_id in m2a.items():
        for sw in _machine_children(machine_id, "SW#"):
            if (sw.get("category") or "") != "database":
                continue
            product = str(sw.get("product") or "").strip()
            if not product:
                continue
            version = str(sw.get("version") or "").strip()
            label = f"{product} {version}".strip()
            instance = str(sw.get("instance_name") or "").strip()
            by_engine[label][app_id].append(instance or product)

    shared = []
    for label, app_map in by_engine.items():
        if len(app_map) < 2:
            continue
        members = sorted(app_map)
        # Report as pairs so the shape matches the source/target contract.
        for i, source in enumerate(members):
            for target in members[i + 1 :]:
                shared.append(
                    {
                        "source": source,
                        "source_name": names.get(source, source),
                        "target": target,
                        "target_name": names.get(target, target),
                        "connections": len(app_map[source]) + len(app_map[target]),
                        "db_ports": [],
                        "engine": label,
                        "constraint": (
                            f"Both depend on {label} — migrate together or "
                            f"target the shared engine last"
                        ),
                    }
                )
    shared.sort(key=lambda s: s["connections"], reverse=True)
    return shared


def build_wave_insights() -> dict[str, Any]:
    """Wave insights in the /graph/wave-insights contract."""
    apps = accessors.list_all_applications()
    edges_raw = _aggregate_app_edges(apps)
    outbound, inbound = _node_degrees(edges_raw)

    complexity: list[dict[str, Any]] = []
    for app in apps:
        app_id = app.get("app_id") or keys.app_id_from_pk(app.get("PK", ""))
        tier = app.get("tier") or "tier_2"
        machines = app.get("machine_count") or len(app.get("machine_ids") or [])
        out_deg = outbound.get(app_id, 0)
        in_deg = inbound.get(app_id, 0)
        deps = out_deg + in_deg
        score = round((1 + deps) * _tier_weight(tier) * (1 + machines / 100), 1)
        complexity.append(
            {
                "app_id": app_id,
                "app_name": app.get("app_name") or app_id,
                "tier": tier,
                "strategy": app.get("migration_strategy") or "",
                "machines": machines,
                "outbound": out_deg,
                "inbound": in_deg,
                "complexity_score": score,
                "wave_recommendation": wave_recommendation(score),
            }
        )
    complexity.sort(key=lambda c: c["complexity_score"], reverse=True)

    recommended: list[dict[str, Any]] = []
    wave_num = 1
    bucket: list[str] = []
    for app in sorted(complexity, key=lambda x: x["complexity_score"]):
        bucket.append(app["app_id"])
        if len(bucket) >= 5:
            recommended.append({"wave": wave_num, "apps": bucket})
            bucket = []
            wave_num += 1
    if bucket:
        recommended.append({"wave": wave_num, "apps": bucket})

    return {
        "shared_databases": _shared_databases(apps),
        "complexity_scores": complexity,
        "recommended_order": recommended,
        "source": "dynamodb",
    }
