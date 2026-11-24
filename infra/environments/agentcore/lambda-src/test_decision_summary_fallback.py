"""Tests for the deterministic decision_summary fallback on approval gates.

Pins a real architectural gap found 2026-10-05: the ASL's Approval_* Task
states (Approval_Discovery, Approval_Assessment, Approval_Design,
Approval_Execution, Approval_Validation, Approval_Cutover) hardcode their
store_token Arguments.Payload to {app_id, mode, step, token}. The Harness
agent's invokeHarness Task has already completed and returned by the time
these separate Approval_* states run, so there was never any mechanism for
the agent to inject a decision_summary into that payload -- despite every
phase skill (discovery.md, assessment.md, design.md, execution.md,
validation.md) documenting a "Gate decision-summary" contract instructing
the agent to attach one. decision_summary was absent from every real
TASK_TOKEN# record on every gate since the feature was speced, confirmed via
GET /workflow/pending/APP-CATALOG on 2026-10-04/05 (no decision_summary key
present on either discovery_review or assessment_review gates in production).

Fix: _store_task_token now falls back to _build_decision_summary, which
reads the same persisted DynamoDB records _validate_phase already reads
and computes decision_summary deterministically -- no agent dependency,
mirrors the pattern already used for validation.

    DYNAMODB_TABLE=test_table AWS_DEFAULT_REGION=us-east-1 pytest test_decision_summary_fallback.py -q
"""
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DYNAMODB_TABLE", "test_table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("HARNESS_ARN", "arn:aws:bedrock-agentcore:us-east-1:000000000000:harness/test")

TABLE_NAME = os.environ["DYNAMODB_TABLE"]
APP = "APP-CATALOG"


@pytest.fixture
def mod(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                       {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table(TABLE_NAME)
        import invoke_runtime as ir

        class _FixedDdb:
            """Stand-in for the module's ddb resource whose .Table() always
            returns this fixture's own mocked table, regardless of which
            mock_aws() context (if any) is active by the time the code under
            test calls ddb.Table(DDB_TABLE) -- matches the pattern already
            proven in test_tool_strategy_guard.py (patch a concrete Table,
            not the resource), which is what keeps this test file isolated
            from import-order effects when run alongside other test files
            in the same pytest process."""

            def Table(self, _name):
                return table

        monkeypatch.setattr(ir, "ddb", _FixedDdb(), raising=False)
        yield ir


def _table(mod):
    return mod.ddb.Table(TABLE_NAME)


def _seed_meta(mod, **fields):
    _table(mod).put_item(Item={"PK": f"APP#{APP}", "SK": "META", "app_id": APP, **fields})


def _get_token_item(mod, step):
    return _table(mod).get_item(Key={"PK": f"APP#{APP}", "SK": f"TASK_TOKEN#{step}"}).get("Item")


# ── _build_decision_summary: per-gate deterministic computation ────────────

class TestDiscoveryGate:
    def test_computes_from_strategy_fields(self, mod):
        _seed_meta(mod, migration_strategy="refactor", strategy_confidence="60",
                   strategy_rationale="Kept the human-confirmed strategy 'refactor'.")
        summary = mod._build_decision_summary("discovery_review", APP, _table(mod))
        assert "REFACTOR" in summary["what_changed"]
        assert "60" in summary["what_changed"]
        assert summary["confidence_or_risk"] == "Kept the human-confirmed strategy 'refactor'."
        assert "if_approve" in summary and "if_reject" in summary and "if_retry" in summary

    def test_missing_fields_do_not_crash(self, mod):
        _seed_meta(mod)
        summary = mod._build_decision_summary("discovery_review", APP, _table(mod))
        assert "unknown" in summary["what_changed"].lower() or "UNKNOWN" in summary["what_changed"]


class TestAssessmentGate:
    def test_computes_from_analysis_record(self, mod):
        _seed_meta(mod, migration_strategy="refactor", strategy_confidence="60")
        _table(mod).put_item(Item={
            "PK": f"APP#{APP}", "SK": "ANALYSIS#assessment-summary#2026-10-05T00:00:00Z",
            "app_id": APP,
            "data": {"result": {
                "transformations": {"recommended": [], "gaps": [{"requirement": "x"}, {"requirement": "y"}]},
                "cost_projection": {"current_annual_cost": {"status": "missing"}},
                "qualification_criteria": [{"criterion": "a", "pass": True}, {"criterion": "b", "pass": False}],  # nosec B105 — test fixture (fake task token), not a credential
            }},
        })
        summary = mod._build_decision_summary("assessment_review", APP, _table(mod))
        assert "0 ATX-automatable" in summary["what_changed"]
        assert "2 custom-authoring gap" in summary["what_changed"]
        assert "status=missing" in summary["confidence_or_risk"]
        assert "1 of 2 checks failed" in summary["confidence_or_risk"]

    def test_no_analysis_record_does_not_crash(self, mod):
        _seed_meta(mod, migration_strategy="refactor")
        summary = mod._build_decision_summary("assessment_review", APP, _table(mod))
        assert "0 ATX-automatable" in summary["what_changed"]
        assert "0 custom-authoring gap" in summary["what_changed"]

    def test_picks_latest_analysis_record_by_sk(self, mod):
        """Two ANALYSIS#assessment-summary records (e.g. a re-run) — must use the newest."""
        _seed_meta(mod, migration_strategy="refactor")
        _table(mod).put_item(Item={
            "PK": f"APP#{APP}", "SK": "ANALYSIS#assessment-summary#2026-10-01T00:00:00Z",
            "app_id": APP, "data": {"result": {"transformations": {"recommended": [{"a": 1}], "gaps": []}}},
        })
        _table(mod).put_item(Item={
            "PK": f"APP#{APP}", "SK": "ANALYSIS#assessment-summary#2026-10-05T00:00:00Z",
            "app_id": APP, "data": {"result": {"transformations": {"recommended": [], "gaps": [{"a": 1}, {"b": 2}, {"c": 3}]}}},
        })
        summary = mod._build_decision_summary("assessment_review", APP, _table(mod))
        assert "0 ATX-automatable" in summary["what_changed"]
        assert "3 custom-authoring gap" in summary["what_changed"]


class TestDesignExecutionValidationCutoverGates:
    def test_design_gate(self, mod):
        _seed_meta(mod, design_version="v1", target_compute="ecs_fargate", design_status="generated")
        summary = mod._build_decision_summary("design_review", APP, _table(mod))
        assert "v1" in summary["what_changed"]
        assert "ecs_fargate" in summary["what_changed"]

    def test_execution_gate(self, mod):
        _seed_meta(mod, exec_infra_status="done", exec_cicd_status="skipped", exec_database_status="done")
        summary = mod._build_decision_summary("execution_review", APP, _table(mod))
        assert "infra=done" in summary["what_changed"]
        assert "cicd=skipped" in summary["what_changed"]

    def test_validation_gate(self, mod):
        _seed_meta(mod, validation_status="passed")
        summary = mod._build_decision_summary("validation_review", APP, _table(mod))
        assert "passed" in summary["what_changed"]

    def test_cutover_gate_has_minimal_fallback(self, mod):
        """No dedicated cutover skill contract exists yet — must still return
        something usable, not crash or return an empty dict for a known step."""
        _seed_meta(mod, status="validated")
        summary = mod._build_decision_summary("cutover_review", APP, _table(mod))
        assert summary != {}
        assert "what_changed" in summary

    def test_unknown_step_returns_empty_dict(self, mod):
        summary = mod._build_decision_summary("not_a_real_step", APP, _table(mod))
        assert summary == {}


# ── _store_task_token: end-to-end fallback wiring ───────────────────────────

class TestStoreTaskTokenFallback:
    def test_fallback_fires_when_caller_omits_decision_summary(self, mod):
        """The real-world case: every ASL store_token call site omits this field."""
        _seed_meta(mod, migration_strategy="refactor", strategy_confidence="60")
        result = mod._store_task_token({"app_id": APP, "step": "discovery_review", "token": "tok-123"})  # nosec B105 — test fixture (fake task token), not a credential
        assert result["status"] == "token_stored"

        item = _get_token_item(mod, "discovery_review")
        assert "decision_summary" in item, "fallback must populate decision_summary when the caller omits it"
        assert "REFACTOR" in item["decision_summary"]["what_changed"]

    def test_caller_provided_decision_summary_is_not_overwritten(self, mod):
        """If a future ASL change DOES thread one through, it must win over the fallback."""
        _seed_meta(mod, migration_strategy="refactor", strategy_confidence="60")
        explicit = {"what_changed": "explicit value", "confidence_or_risk": "x", "if_approve": "y",
                    "if_reject": "z", "if_retry": "w"}
        result = mod._store_task_token({
            "app_id": APP, "step": "discovery_review", "token": "tok-123",  # nosec B105 — test fixture (fake task token), not a credential
            "decision_summary": explicit,
        })
        assert result["status"] == "token_stored"
        item = _get_token_item(mod, "discovery_review")
        assert item["decision_summary"] == explicit

    def test_fallback_computation_failure_does_not_block_the_gate(self, mod, monkeypatch):
        """decision_summary is advisory UI content -- a computation bug in the
        fallback must never prevent the task token (the real gate mechanism)
        from being stored."""
        _seed_meta(mod, migration_strategy="refactor")
        monkeypatch.setattr(mod, "_build_decision_summary", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

        result = mod._store_task_token({"app_id": APP, "step": "discovery_review", "token": "tok-456"})  # nosec B105 — test fixture (fake task token), not a credential
        assert result["status"] == "token_stored", "the gate must still succeed even if the fallback computation raises"

        item = _get_token_item(mod, "discovery_review")
        assert "decision_summary" not in item, "a failed computation must omit the field, not store partial/garbage data"

    def test_fallback_still_requires_app_id_step_token(self, mod):
        result = mod._store_task_token({"app_id": "", "step": "discovery_review", "token": "x"})  # nosec B105 — test fixture (fake task token), not a credential
        assert result["status"] == "error"
