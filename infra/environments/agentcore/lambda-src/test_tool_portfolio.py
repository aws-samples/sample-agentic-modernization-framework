"""update_app_field is agent-callable: only allowlisted, descriptive fields."""
import ast
import os
from pathlib import Path

import pytest

os.environ.setdefault("DYNAMODB_TABLE", "test-table")
os.environ.setdefault("AWS_REGION", "us-east-1")

import tool_portfolio as tp  # noqa: E402


class _CondFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self, exists=True):
        self.exists, self.updates = exists, []

    def update_item(self, **kw):
        self.updates.append(kw)
        if not self.exists:
            raise _CondFailed()


@pytest.fixture
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(tp, "table", t)
    return t


@pytest.mark.parametrize("field,value", [
    ("status", "discovered"), ("status", "assessed"), ("status", "cutover_ready"), ("status", "completed"),
    ("exec_database_status", "done"), ("language", "java"), ("framework", "spring-boot"),
    ("build_tool", "maven"), ("compliance", "PCI"), ("rpo", "1h"), ("rto", "4h"), ("data_classification", "internal"),
])
def test_allowed_fields_are_written_only_to_an_existing_app(table, field, value):
    out = tp.update_app_field({"app_id": "APP-X", "field": field, "value": value})
    assert out == {"success": True, "app_id": "APP-X", "field": field}
    (u,) = table.updates
    assert u["Key"] == {"PK": "APP#APP-X", "SK": "META"}
    assert u["ConditionExpression"] == "attribute_exists(PK)"
    assert u["ExpressionAttributeNames"] == {"#f": field} and u["ExpressionAttributeValues"] == {":v": value}


@pytest.mark.parametrize("field", [
    "expose_publicly", "deploy_approved_commit", "deploy_approved_by", "deploy_approved_at", "exec_head_commit",
    "target_account", "vpc_cidr", "target_region", "migration_strategy", "strategy_confidence",
    "target_compute", "target_database", "cicd_target", "validation_write_tests", "validation_status",
    "machine_ids", "dynamodb_table_name", "deploy_branch", "risk_score", "risk_tier", "transformation_steps",
    "exec_infra_status", "exec_deploy_status", "design_status", "assessment_status",
    "PK", "SK", "GSI1PK", "GSI1SK", "", "Status",
])
def test_denied_fields_never_reach_dynamodb(table, field):
    out = tp.update_app_field({"app_id": "APP-X", "field": field, "value": "true"})
    assert "error" in out and "not agent-editable" in out["error"]
    assert table.updates == []


@pytest.mark.parametrize("field,value", [("status", "deployed"), ("status", "approved"),
                                         ("exec_database_status", "skipped"), ("status", None)])
def test_status_values_are_restricted(table, field, value):
    out = tp.update_app_field({"app_id": "APP-X", "field": field, "value": value})
    assert "must be one of" in out["error"] and table.updates == []


def test_missing_app_is_refused_not_created(monkeypatch):
    t = FakeTable(exists=False)
    monkeypatch.setattr(tp, "table", t)
    out = tp.update_app_field({"app_id": "APP-NOPE", "field": "language", "value": "java"})
    assert out == {"error": "Application APP-NOPE not found"}


def test_other_dynamodb_errors_propagate(monkeypatch):
    class Boom:
        def update_item(self, **_):
            raise RuntimeError("throttled")
    monkeypatch.setattr(tp, "table", Boom())
    with pytest.raises(RuntimeError):
        tp.update_app_field({"app_id": "APP-X", "field": "language", "value": "java"})


def test_allowlist_excludes_run_derived_fields_except_the_asl_database_status():
    src = Path(__file__).resolve().parents[4] / "backend/src/api/v1/workflow.py"
    tree = ast.parse(src.read_text())
    run_derived = next(
        ast.literal_eval(n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "RUN_DERIVED_META_FIELDS" for t in n.targets))
    assert set(run_derived) & tp.AGENT_EDITABLE_FIELDS == {"exec_database_status"}
