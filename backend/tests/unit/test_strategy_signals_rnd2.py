"""Replatform signals + DB-report false positive.

APP-MUSIC / APP-MOVIES (2026-10-21) were recommended rehost ("insufficient data"):
"partial (Docker)" and a serverless handler counted for nothing, an EOL runtime
wasn't a signal, and the execution gate warned that dynamodb "replaces" a DynamoDB
source.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from backend.src.core import risk_scoring as _risk_scoring

_LAMBDA_SRC = Path(__file__).resolve().parents[3] / "infra" / "environments" / "agentcore" / "lambda-src"
if str(_LAMBDA_SRC) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_SRC))
os.environ.setdefault("DYNAMODB_TABLE", "amf_portfolio")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.modules.setdefault("risk_scoring", _risk_scoring)  # tool_documents' zip bundles it

import tool_documents  # noqa: E402
import tool_strategy  # noqa: E402

MUSIC = {
    "app_name": "Music API (Flask)", "language": "Python", "framework": "Flask", "architecture": "monolith",
    "deployment_method": "manual", "containerization": "partial (Docker)", "tier": "tier_3",
    "current_compute": "on-prem Docker host (single VM)",
    "description": "Flask REST API on a single on-prem Docker host, Python 3.8 (EOL), DynamoDB-backed.",
    "database": {"engine": "DynamoDB", "size_gb": 1, "licensing": "managed"},
}
MOVIES = {
    "app_name": "Movies Ingest API (Lambda)", "language": "Python", "framework": "AWS Lambda handler",
    "architecture": "serverless", "deployment_method": "manual", "containerization": "none", "tier": "tier_3",
    "current_compute": "on-prem VM (cron + HTTP shim)",
    "description": "Ingest function (index.handler) writing movie records to DynamoDB.",
    "database": {"engine": "DynamoDB", "size_gb": 1, "licensing": "managed"},
}


@pytest.mark.parametrize("meta", [MUSIC, MOVIES], ids=["APP-MUSIC", "APP-MOVIES"])
def test_replatform_shaped_apps_recommend_replatform(meta):
    best, candidates = tool_strategy._evaluate_rules(meta)
    assert best["strategy"] == "replatform", best
    assert best["confidence"] >= 70
    top = next(c for c in candidates if c["strategy"] == "replatform")
    assert len(top["matched"]) >= 2  # still a compound rule


def test_signals_explain_themselves():
    music = next(c for c in tool_strategy._signal_candidates(MUSIC, "") if c["strategy"] == "replatform")["matched"]
    assert any("partially containerized" in m for m in music)
    assert any("end-of-life runtime" in m and "3.8" in m for m in music)
    assert any("managed database source" in m for m in music)
    movies = next(c for c in tool_strategy._signal_candidates(MOVIES, "") if c["strategy"] == "replatform")["matched"]
    assert any("function-shaped app" in m for m in movies)


def test_already_on_lambda_is_not_a_function_signal():
    meta = dict(MOVIES, current_compute="AWS Lambda (us-east-1)")
    rp = [c for c in tool_strategy._signal_candidates(meta, "") if c["strategy"] == "replatform"]
    assert not any("function-shaped" in m for c in rp for m in c["matched"])


def test_orchestrated_docker_is_not_partial():
    meta = dict(MUSIC, containerization="docker on ECS", database={"engine": "Oracle"}, description="")
    rp = [c for c in tool_strategy._signal_candidates(meta, "") if c["strategy"] == "replatform"]
    assert not any("containerized" in m for c in rp for m in c["matched"])


@pytest.mark.parametrize("text,eol", [
    ("Python 3.8", True), ("python 2.7", True), ("Python 3.12", False), ("Python 3.10", False),
    ("Java 8", True), ("JDK 1.8", True), ("Java 17", False), ("Node 16", True), ("Node.js 20", False),
])
def test_eol_runtime_detection(text, eol):
    assert bool(tool_strategy._eol_runtime({"runtime_version": text})) is eol


def test_db_report_same_family_source_is_not_flagged():
    result = {"status": "not_required", "assessed_at": "2026-10-21",
              "migration_method": "not_applicable — the source is already DynamoDB"}
    warnings = tool_documents._stamp_execution_database(
        result, {"target_database": "dynamodb", "database": {"engine": "DynamoDB"}})
    assert not any("no database migration is required" in w for w in warnings)


def test_db_report_same_family_postgres_is_not_flagged():
    result = {"status": "not_required", "assessed_at": "2026-10-21", "notes": "PostgreSQL stays PostgreSQL on RDS"}
    warnings = tool_documents._stamp_execution_database(
        result, {"target_database": "rds_postgresql", "database": {"engine": "PostgreSQL"}})
    assert not any("no database migration is required" in w for w in warnings)


def test_db_report_other_family_or_contradicted_source_still_flagged():
    # Discovered MySQL → dynamodb really replaces the datastore.
    w = tool_documents._stamp_execution_database(
        {"status": "not_required", "assessed_at": "2026-10-21"},
        {"target_database": "dynamodb", "database": {"engine": "MySQL"}})
    assert any("no database migration is required" in x for x in w)
    # Discovery says DynamoDB but the report rewrites a MySQL/MyBatis layer (APP-CLI).
    w = tool_documents._stamp_execution_database(
        {"status": "not_required", "assessed_at": "2026-10-21",
         "data_access_layer_change": {"description": "Replace MyBatis/MySQL data-access layer with DynamoDB"}},
        {"target_database": "dynamodb", "database": {"engine": "DynamoDB"}})
    assert any("no database migration is required" in x and "MySQL/relational source" in x for x in w)
