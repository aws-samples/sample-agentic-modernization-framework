"""L2 enrichment + app-context fixes from the APP-CLI/HE/TE journey audit.

- POST /portfolio/apps/enrich used to drop ~20 L2 fields (extra="ignore") and never
  wrote app_name/description; L2 machine_ids must replace the ADS auto-grouping.
- tool_portfolio.get_app_context returned machines: [] because it queried
  APP#…/MACHINE# while machines are MACHINE#<id>/META items linked via META.machine_ids.
- Completeness (tool + core) must count OWNER#/REPO# sibling items.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_LAMBDA_SRC = Path(__file__).resolve().parents[3] / "infra" / "environments" / "agentcore" / "lambda-src"
if str(_LAMBDA_SRC) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_SRC))
os.environ.setdefault("DYNAMODB_TABLE", "amf_portfolio")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

APP = "APP-HE"
ADS_IDS = [f"M-{i:08X}" for i in range(34)]

# Trimmed copy of the APP-HE L2 record (/tmp/review/APP-HE/l2_app-data.json).
L2_HE = {
    "app_id": APP,
    "app_name": "HE Health Engine",
    "description": "HE Health Engine — Health Services monolith application (Java/Spring Boot 2.7).",
    "business_unit": "Health Services",
    "tier": "tier_1",
    "user_count": 20000,
    "compliance": ["HIPAA"],
    "data_classification": "restricted",
    "database": {"engine": "PostgreSQL", "version": "13", "size_gb": 100, "licensing": "open_source"},
    "language": "Java",
    "framework": "Spring Boot 2.7",
    "architecture": "monolith",
    "deployment_method": "manual",
    "current_compute": "on-prem VMware vSphere",
    "current_region": "on-prem DC (Dallas, TX)",
    "containerization": "none",
    "cicd_target": "AWS CodePipeline",
    "target_region": "us-east-1",
    "machine_ids": ["MC-HE-01", "MC-HE-02", "MC-HE-03", "MC-HE-04", "MC-HE-05", "MC-HE-06"],
    "machine_count": 6,
    "owners": [
        {"id": "own-grace-kim", "name": "Grace Kim", "email": "grace.kim@example.com", "role": "business_owner"},
        {"id": "own-omar-haddad", "name": "Omar Haddad", "email": "omar.haddad@example.com", "role": "tech_lead"},
    ],
    "repos": [{"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/java-on-aws", "branch": "main"}],
    "rpo": "15 minutes",
    "rto": "1 hour",
    "uptime_sla": "99.95%",
    "multi_region": "yes",
    "dr_regions": ["us-west-2"],
    "dependencies_upstream": ["APP-MCC-ORACLE", "APP-PDB"],
    "dependencies_downstream": ["APP-RDB", "APP-QIE"],
    "constraints": {"dc_lease_end": "2027-03-31", "eosl_date": "2027-11-30", "hard_deadline": "2027-01-31"},
    "change_freeze_windows": ["2026-11-20/2026-11-30 (holiday peak)"],
    "licensing": "open_source",
    "db_migration_method": "AWS DMS (homogeneous)",
    "target_db_engine": "Aurora PostgreSQL 15",
    "startup_shutdown_procedures": "Documented runbook: graceful drain via LB.",
    "owner_id": "own-grace-kim",
    "tech_lead_id": "own-omar-haddad",
    "dr_availability": {"rpo": "15 minutes", "rto": "1 hour", "multi_region": True, "dr_strategy": "pilot-light"},
    "hard_deadline": "2027-01-31",
    "blackout_periods": "2026-11-20/2026-11-30 (holiday peak)",
    "some_future_l2_field": {"kept": True},
    "PK": "APP#EVIL",  # reserved — must never be written
}


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName="amf_portfolio",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                              {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        t.put_item(Item={
            "PK": f"APP#{APP}", "SK": "META", "GSI1PK": "TYPE#APP", "GSI1SK": APP, "app_id": APP,
            "app_name": "HE Application", "description": "Auto-grouped from 34 server(s) — pattern: 'he'",
            "machine_ids": ADS_IDS, "machine_count": 34, "discovery_source": "aws_ads",
        })
        yield t


def _meta(table):
    return table.get_item(Key={"PK": f"APP#{APP}", "SK": "META"})["Item"]


async def _enrich(table, records):
    from backend.src.api.v1 import portfolio

    with patch.object(portfolio, "get_table", return_value=table):
        items = [portfolio.EnrichAppItem.model_validate(r) for r in records]
        return portfolio.bulk_enrich(items)


# ── 1. L2 enrichment persists every field ────────────────────────────────────

async def test_every_l2_field_is_persisted(table):
    resp = await _enrich(table, [L2_HE])
    assert resp.data["updated"] == 1, resp.data
    meta = _meta(table)
    for f in ("rpo", "rto", "uptime_sla", "multi_region", "dr_regions", "dr_availability", "target_region",
              "current_compute", "current_region", "containerization", "licensing", "db_migration_method",
              "target_db_engine", "startup_shutdown_procedures", "change_freeze_windows", "blackout_periods",
              "dependencies_upstream", "dependencies_downstream", "hard_deadline", "owner_id", "tech_lead_id"):
        assert meta.get(f) not in (None, "", []), f"{f} was dropped"
    assert meta["some_future_l2_field"] == {"kept": True}, "unknown L2 fields must be kept, not dropped"
    assert meta["PK"] == f"APP#{APP}"


async def test_app_name_and_description_written(table):
    await _enrich(table, [L2_HE])
    meta = _meta(table)
    assert meta["app_name"] == "HE Health Engine"
    assert meta["description"].startswith("HE Health Engine")


async def test_only_provided_fields_are_updated(table):
    await _enrich(table, [{"app_id": APP, "tier": "tier_2", "app_name": "", "rpo": None}])
    meta = _meta(table)
    assert meta["tier"] == "tier_2"
    assert meta["app_name"] == "HE Application", "blank app_name must not overwrite"
    assert "rpo" not in meta


async def test_l2_machine_ids_replace_ads_grouping(table):
    await _enrich(table, [L2_HE])
    meta = _meta(table)
    assert meta["machine_ids"] == L2_HE["machine_ids"]
    assert meta["machine_count"] == 6


async def test_machine_count_alone_does_not_desync(table):
    await _enrich(table, [{"app_id": APP, "machine_count": 6}])
    meta = _meta(table)
    assert meta["machine_count"] == 34 and len(meta["machine_ids"]) == 34


async def test_cicd_preference_kept_verbatim_and_normalised(table):
    await _enrich(table, [L2_HE])
    meta = _meta(table)
    assert meta["cicd_target_preference"] == "AWS CodePipeline"
    assert meta["cicd_target"] == "codepipeline", "must be a valid strategy_options enum"
    assert meta["l2_preferences"] == {"cicd_target": "codepipeline"}


async def test_unrecognised_cicd_preference_not_written_as_enum(table):
    await _enrich(table, [{"app_id": APP, "cicd_target": "Bamboo"}])
    meta = _meta(table)
    assert meta["cicd_target_preference"] == "Bamboo"
    assert "cicd_target" not in meta


async def test_dr_availability_backfills_flat_fields(table):
    await _enrich(table, [{"app_id": APP, "dr_availability": {"rpo": "5 minutes", "rto": "30 minutes"}}])
    meta = _meta(table)
    assert meta["rpo"] == "5 minutes" and meta["rto"] == "30 minutes"


async def test_owners_and_repos_are_sibling_items(table):
    await _enrich(table, [L2_HE])
    meta = _meta(table)
    assert "owners" not in meta and "repos" not in meta
    from boto3.dynamodb.conditions import Key

    sk = [i["SK"] for i in table.query(KeyConditionExpression=Key("PK").eq(f"APP#{APP}"))["Items"]]
    assert "OWNER#grace.kim@example.com" in sk and "REPO#java-on-aws" in sk


# ── 2. get_app_context resolves machines from META.machine_ids ───────────────

@pytest.fixture
def portfolio_tool(table, monkeypatch):
    import tool_portfolio

    monkeypatch.setattr(tool_portfolio, "table", table)
    return tool_portfolio


def test_app_context_resolves_machine_entities(table, portfolio_tool):
    for mid in ADS_IDS[:3]:
        table.put_item(Item={"PK": f"MACHINE#{mid}", "SK": "META", "machine_id": mid, "hostname": mid.lower()})
    out = portfolio_tool.get_app_context({"app_id": APP})
    assert {m["machine_id"] for m in out["machines"]} == set(ADS_IDS[:3])
    assert all("PK" not in m for m in out["machines"])
    assert len(out["unresolved_machine_ids"]) == 31


def test_app_context_batches_over_100_ids(table, portfolio_tool):
    ids = [f"M-{i:04d}" for i in range(150)]
    table.update_item(Key={"PK": f"APP#{APP}", "SK": "META"}, UpdateExpression="SET machine_ids = :m",
                      ExpressionAttributeValues={":m": ids})
    with table.batch_writer() as b:
        for mid in ids:
            b.put_item(Item={"PK": f"MACHINE#{mid}", "SK": "META", "machine_id": mid})
    out = portfolio_tool.get_app_context({"app_id": APP})
    assert len(out["machines"]) == 150 and out["unresolved_machine_ids"] == []


def test_app_context_falls_back_to_sibling_query(table, portfolio_tool):
    table.update_item(Key={"PK": f"APP#{APP}", "SK": "META"}, UpdateExpression="REMOVE machine_ids")
    table.put_item(Item={"PK": f"APP#{APP}", "SK": "MACHINE#legacy-1", "machine_id": "legacy-1"})
    out = portfolio_tool.get_app_context({"app_id": APP})
    assert [m["machine_id"] for m in out["machines"]] == ["legacy-1"]


# ── 3. Completeness counts OWNER#/REPO# siblings; gates agree with core ─────

@pytest.fixture
def strategy_tool(table, monkeypatch):
    import tool_strategy

    monkeypatch.setattr(tool_strategy, "table", table)
    return tool_strategy


async def test_tool_completeness_counts_sibling_owners_and_repos(table, strategy_tool):
    await _enrich(table, [L2_HE])
    out = strategy_tool.calculate_completeness({"app_id": APP})
    assert "owners" not in out["missing_fields"]
    assert "repos" not in out["missing_fields"]
    assert out["categories"]["ownership"]["score"] == 100


async def test_tool_and_core_report_the_same_missing_fields_and_gates(table, strategy_tool):
    from backend.src.api.v1.portfolio import _read_full_app
    from backend.src.core.completeness import PHASE_GATES, _field_value, _is_filled, calculate_completeness
    from backend.src.db.helpers import from_dynamo

    await _enrich(table, [{**L2_HE, "compliance": [], "rto": ""}])
    tool = strategy_tool.calculate_completeness({"app_id": APP})
    app = from_dynamo(_read_full_app(table, APP))
    core = calculate_completeness(app)
    gate_missing = {
        g: [f for f in gate["mandatory"] if not _is_filled(f, _field_value(app, f))] for g, gate in PHASE_GATES.items()
    }

    assert tool["missing_fields"] == [m["field"] for m in core["missing_fields"]]
    assert tool["score_percent"] == core["percentage"]
    for gate, missing in gate_missing.items():
        assert tool["phase_gates"][gate] == (not missing), gate
        assert tool["phase_gate_missing"][gate] == missing, gate
    assert tool["phase_gate_missing"]["assessment"] == ["compliance"]


def test_core_completeness_accepts_sibling_counts():
    from backend.src.core.completeness import calculate_completeness

    missing = {m["field"] for m in calculate_completeness({"owner_count": 2, "repo_count": 1})["missing_fields"]}
    assert "owners" not in missing and "repos" not in missing


def test_lambda_completeness_tables_match_core(strategy_tool):
    from backend.src.core import completeness as core

    assert {k: [f for f, _ in v["fields"]] for k, v in core.COMPLETENESS_FIELDS.items()} == \
        {k: v["fields"] for k, v in strategy_tool.COMPLETENESS_FIELDS.items()}
    assert {k: v["weight"] for k, v in core.COMPLETENESS_FIELDS.items()} == \
        {k: v["weight"] for k, v in strategy_tool.COMPLETENESS_FIELDS.items()}
    assert {k: v["mandatory"] for k, v in core.PHASE_GATES.items()} == strategy_tool.PHASE_GATES
