"""Declared app dependencies (META depends_on)."""
from unittest.mock import MagicMock, patch

from backend.src.api.v1.portfolio import _validate_depends_on
from backend.src.db import graph_dynamo


def _table(existing: set[str]):
    t = MagicMock()
    t.get_item.side_effect = lambda Key, **_: {"Item": {"PK": Key["PK"]}} if Key["PK"][4:] in existing else {}
    return t


def test_valid_list_is_deduplicated_and_kept():
    deps, err = _validate_depends_on(_table({"APP-B", "APP-C"}), "APP-A", ["APP-B", " APP-C ", "APP-B"])
    assert err == "" and deps == ["APP-B", "APP-C"]


def test_rejects_self_unknown_bad_shape_and_cap(monkeypatch):
    t = _table({"APP-B"})
    assert "itself" in _validate_depends_on(t, "APP-A", ["APP-A"])[1]
    assert "APP-Z" in _validate_depends_on(t, "APP-A", ["APP-Z"])[1]
    assert "invalid app id" in _validate_depends_on(t, "APP-A", ["APP-B'}) MATCH (n) DETACH DELETE n //"])[1]
    assert "list" in _validate_depends_on(t, "APP-A", "APP-B")[1]
    from backend.src.config import settings
    monkeypatch.setattr(settings, "app_dependencies_max", 2)
    many = _table({f"APP-{i}" for i in range(5)})
    assert "at most 2" in _validate_depends_on(many, "APP-A", [f"APP-{i}" for i in range(3)])[1]


def test_empty_or_none_clears():
    assert _validate_depends_on(_table(set()), "APP-A", []) == ([], "")
    assert _validate_depends_on(_table(set()), "APP-A", None) == ([], "")


def test_declared_edges_merge_into_topology():
    apps = [
        {"app_id": "APP-A", "app_name": "A", "machine_ids": ["m1"], "depends_on": ["APP-B", "APP-C", "APP-GONE"]},
        {"app_id": "APP-B", "app_name": "B", "machine_ids": ["m2"]},
        {"app_id": "APP-C", "app_name": "C"},
    ]
    conns = {"m1": [{"target_machine_id": "m2", "connection_count": 7, "target_port": "443", "protocol": "tcp"}], "m2": []}
    with patch.object(graph_dynamo, "_machine_children", side_effect=lambda mid, _p: conns.get(mid, [])):
        edges = graph_dynamo._aggregate_app_edges(apps)
    assert edges[("APP-A", "APP-B")]["weight"] == 7                       # discovered weight kept
    assert {"tcp", "declared"} <= edges[("APP-A", "APP-B")]["processes"]  # merged with the declared link
    assert edges[("APP-A", "APP-C")]["weight"] == 1 and edges[("APP-A", "APP-C")]["processes"] == {"declared"}
    assert ("APP-A", "APP-GONE") not in edges                             # unknown target skipped
