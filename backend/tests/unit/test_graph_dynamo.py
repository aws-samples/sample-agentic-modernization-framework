"""
Tests for the DynamoDB-backed graph derivation.

These assert two things that matter:
  1. the response shapes match the /graph API contract (shared with Neptune), so the
     frontend cannot tell which backend answered;
  2. app-level edges and shared-database coupling are derived correctly from
     the key layout that connectors/ingest.py actually writes — machines under
     PK=MACHINE#<id> with CONN#/SW# children, and app ownership expressed as
     machine_ids[] on the app META record.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

TABLE = "test_portfolio"


def _make_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return table


def _seed(table):
    """Two apps that talk to each other and share a postgres engine, plus an
    isolated third app that must not gain phantom edges."""
    apps = [
        ("APP-CATALOG", "Catalog Service", "tier_1", ["m-cat-1", "m-cat-2"]),
        ("APP-METADATA", "Metadata Service", "tier_2", ["m-meta-1"]),
        ("APP-LONELY", "Standalone", "tier_3", ["m-lone-1"]),
    ]
    for app_id, name, tier, machines in apps:
        table.put_item(
            Item={
                "PK": f"APP#{app_id}",
                "SK": "META",
                "GSI1PK": "TYPE#APP",
                "GSI1SK": app_id,
                "app_id": app_id,
                "app_name": name,
                "tier": tier,
                "business_unit": "Video",
                "migration_strategy": "replatform",
                "machine_ids": machines,
                "machine_count": len(machines),
            }
        )
    for mid in ["m-cat-1", "m-cat-2", "m-meta-1", "m-lone-1"]:
        table.put_item(
            Item={
                "PK": f"MACHINE#{mid}",
                "SK": "META",
                "GSI1PK": "TYPE#MACHINE",
                "GSI1SK": mid,
                "machine_id": mid,
            }
        )

    # Cross-app edge (catalog -> metadata), counted twice to test aggregation.
    for i, count in enumerate([5000, 120]):
        table.put_item(
            Item={
                "PK": "MACHINE#m-cat-1",
                "SK": f"CONN#conn{i}",
                "source_machine_id": "m-cat-1",
                "target_machine_id": "m-meta-1",
                "target_port": "8443" if i == 0 else "5432",
                "protocol": "https" if i == 0 else "postgres",
                "connection_count": count,
            }
        )
    # Internal edge inside APP-CATALOG — must be dropped, not reported.
    table.put_item(
        Item={
            "PK": "MACHINE#m-cat-1",
            "SK": "CONN#internal",
            "source_machine_id": "m-cat-1",
            "target_machine_id": "m-cat-2",
            "target_port": "9090",
            "connection_count": 999,
        }
    )
    # Shared database engine across the two apps.
    for mid, app_instance in [("m-cat-1", "catalog_db"), ("m-meta-1", "metadata_db")]:
        table.put_item(
            Item={
                "PK": f"MACHINE#{mid}",
                "SK": f"SW#postgresql#{app_instance}",
                "product": "postgresql",
                "version": "14",
                "category": "database",
                "instance_name": app_instance,
            }
        )


@pytest.fixture
def seeded(monkeypatch):
    with mock_aws():
        table = _make_table()
        monkeypatch.setenv("DYNAMODB_TABLE", TABLE)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        from backend.src.db import helpers

        monkeypatch.setattr(helpers, "get_table", lambda: table)
        from backend.src.db import accessors, graph_dynamo

        monkeypatch.setattr(accessors, "get_table", lambda: table)
        monkeypatch.setattr(graph_dynamo, "get_table", lambda: table)
        _seed(table)
        yield graph_dynamo


class TestTopology:
    def test_shape_matches_the_graph_api_contract(self, seeded):
        topo = seeded.build_topology()
        assert set(topo) >= {"nodes", "edges"}
        node = topo["nodes"][0]
        assert set(node) == {
            "id", "name", "tier", "strategy", "business_unit",
            "machines", "outbound", "inbound",
        }
        edge = topo["edges"][0]
        assert set(edge) == {"source", "target", "weight", "ports", "types"}

    def test_all_apps_present_as_nodes(self, seeded):
        ids = {n["id"] for n in seeded.build_topology()["nodes"]}
        assert ids == {"APP-CATALOG", "APP-METADATA", "APP-LONELY"}

    def test_cross_app_edge_derived_and_aggregated(self, seeded):
        edges = seeded.build_topology()["edges"]
        assert len(edges) == 1, "only the cross-app edge should appear"
        e = edges[0]
        assert (e["source"], e["target"]) == ("APP-CATALOG", "APP-METADATA")
        assert e["weight"] == 5120, "both connections should aggregate"
        assert set(e["ports"]) == {"8443", "5432"}

    def test_internal_edge_is_not_a_dependency(self, seeded):
        edges = seeded.build_topology()["edges"]
        assert all(e["source"] != e["target"] for e in edges)
        assert all(e["weight"] != 999 for e in edges), "intra-app traffic leaked"

    def test_isolated_app_has_no_degree(self, seeded):
        nodes = {n["id"]: n for n in seeded.build_topology()["nodes"]}
        assert nodes["APP-LONELY"]["outbound"] == 0
        assert nodes["APP-LONELY"]["inbound"] == 0
        assert nodes["APP-CATALOG"]["outbound"] == 1
        assert nodes["APP-METADATA"]["inbound"] == 1


class TestWaveInsights:
    def test_shape_matches_the_graph_api_contract(self, seeded):
        ins = seeded.build_wave_insights()
        assert set(ins) >= {"shared_databases", "complexity_scores", "recommended_order"}
        score = ins["complexity_scores"][0]
        assert set(score) == {
            "app_id", "app_name", "tier", "strategy", "machines",
            "outbound", "inbound", "complexity_score", "wave_recommendation",
        }

    def test_shared_database_coupling_found(self, seeded):
        shared = seeded.build_wave_insights()["shared_databases"]
        assert len(shared) == 1
        pair = {shared[0]["source"], shared[0]["target"]}
        assert pair == {"APP-CATALOG", "APP-METADATA"}
        assert shared[0]["engine"] == "postgresql 14"
        assert "postgresql 14" in shared[0]["constraint"]

    def test_tier_one_scores_higher_than_tier_three(self, seeded):
        scores = {s["app_id"]: s["complexity_score"] for s in seeded.build_wave_insights()["complexity_scores"]}
        assert scores["APP-CATALOG"] > scores["APP-LONELY"]

    def test_every_app_lands_in_a_wave(self, seeded):
        ins = seeded.build_wave_insights()
        planned = {a for w in ins["recommended_order"] for a in w["apps"]}
        assert planned == {"APP-CATALOG", "APP-METADATA", "APP-LONELY"}


def test_wave_planning_enabled_without_a_graph_store(monkeypatch):
    """The capability flag must be on by default, or the UI hides the page."""
    from backend.src.config import Settings

    # Test the code default, not whatever the local .env / shell sets.
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    assert Settings(_env_file=None).graph_backend == "dynamodb"
    assert Settings(_env_file=None).wave_planning_enabled is True
    assert Settings(_env_file=None, graph_backend="none").wave_planning_enabled is False
