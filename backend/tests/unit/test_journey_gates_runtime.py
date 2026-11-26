"""Pre-hook / post-check / decision-summary contracts (invoke_runtime Lambda) and the
Validate_Design ASL wiring, from the APP-CLI/HE/TE journey audit.

- validate_phase(assessment) passed on ANY ANALYSIS# — Discovery's risk-score record
  satisfied it. It now needs THIS run's assessment-summary.
- prehook(design) likewise needs a current-run assessment-summary.
- validate_phase(design) looked only for DESIGN#v1 (APP-TE stored v2) and never checked
  the doc against the approved selections; it now reads the latest stored version and
  enforces its store-time consistency_check.
- prehook(execution) now requires vpc_cidr / target_database / cicd_target valid per strategy.
- decision summaries surface the new warnings.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from backend.src.core import completeness as _completeness
from backend.src.core import risk_scoring as _risk_scoring
from backend.src.core import strategy_options as _strategy_options

_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_SRC = _ROOT / "infra" / "environments" / "agentcore" / "lambda-src"
if str(_LAMBDA_SRC) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_SRC))
sys.modules.setdefault("risk_scoring", _risk_scoring)
os.environ.setdefault("DYNAMODB_TABLE", "amf_portfolio")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import invoke_runtime  # noqa: E402
import tool_documents  # noqa: E402

APP = "APP-TE"
RUN_START = "2026-10-01T13:30:00.312Z"
ASL_PATH = _ROOT / "infra" / "step-functions" / "migration-workflow.v2.asl.json"


class _Ddb:
    def __init__(self, table):
        self._table = table

    def Table(self, _name):  # noqa: N802 — mirrors boto3
        return self._table


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
        with patch.object(invoke_runtime, "ddb", _Ddb(t)), patch.object(tool_documents, "table", t), \
                patch.object(tool_documents, "BUCKET", ""):
            yield t


REHOST_META = {
    "migration_strategy": "rehost", "target_compute": "ec2", "target_database": "same_as_source",
    "cicd_target": "codepipeline", "target_account": "987654321981", "vpc_cidr": "192.168.0.0/16",
    "ami_id": "ami-0123456789abcdef0", "strategy_confidence": 80,
}


def _put(table, sk, **fields):
    table.put_item(Item={"PK": f"APP#{APP}", "SK": sk, "app_id": APP, **fields})


def _put_meta(table, **fields):
    _put(table, "META", GSI1PK="TYPE#APP", GSI1SK=APP, app_name="TE Application", **fields)


def _risk_score(table, ts="2026-10-01T13:30:35Z"):
    _put(table, f"ANALYSIS#risk-score#{ts}", analysis_type="risk-score", updated_at=ts)


def _summary(table, ts="2026-10-01T14:29:27Z", **fields):
    _put(table, f"ANALYSIS#assessment-summary#{ts}", analysis_type="assessment-summary", updated_at=ts,
         result={"transformations": {"recommended": [], "gaps": []}}, **fields)


def _prehook(phase, **extra):
    return invoke_runtime.handler({"mode": "prehook_phase", "phase": phase, "app_id": APP, **extra})


def _validate(phase, **extra):
    return invoke_runtime.handler({"mode": "validate_phase", "phase": phase, "app_id": APP, **extra})


# ── 3. Assessment post-check + design pre-hook need THIS run's assessment-summary ──

class TestAssessmentGates:
    def test_risk_score_record_no_longer_satisfies_assessment(self, table):
        _put_meta(table, **REHOST_META)
        _risk_score(table)
        out = _validate("assessment", run_started_at=RUN_START)
        assert out["valid"] is False
        assert out["missing"] == ["assessment_summary"]

    def test_current_run_summary_passes_and_surfaces_warnings(self, table):
        _put_meta(table, **REHOST_META)
        _summary(table, consistency_warnings=["target_compute_rationale names ECS Fargate"])
        out = _validate("assessment", run_started_at=RUN_START)
        assert out["valid"] is True
        assert out["warnings"] == ["target_compute_rationale names ECS Fargate"]

    def test_summary_from_a_previous_run_is_stale(self, table):
        _put_meta(table, **REHOST_META)
        _summary(table, ts="2026-09-20T10:00:00Z")
        out = _validate("assessment", run_started_at=RUN_START)
        assert out["valid"] is False and out["missing"] == ["assessment_summary_stale"]

    def test_falls_back_to_workflow_active_start(self, table):
        _put_meta(table, **REHOST_META)
        _put(table, "WORKFLOW#ACTIVE", started_at="2026-10-01T13:30:00.312709+00:00")
        _summary(table, ts="2026-09-20T10:00:00Z")
        assert _validate("assessment")["missing"] == ["assessment_summary_stale"]

    def test_discovery_prehook_runs_without_l2_repo(self, table):
        # L1-only app (machines, no L2 repos) must still start Discovery.
        _put_meta(table)
        out = _prehook("discovery")
        assert out["ready"] is True and out["warnings"] == ["no_repo_linked"]
        # ...but Assessment still refuses to clone nothing.
        _put_meta(table, migration_strategy="rehost")
        assert _prehook("assessment")["blockers"] == ["no_repo_linked"]

    def test_execution_prehook_rejects_host_bit_cidr(self, table):
        assert invoke_runtime._is_network_address("10.0.0.0/24")
        assert not invoke_runtime._is_network_address("10.0.0.2/24")

    def test_discovery_prehook_requires_app_name(self, table):
        _put(table, "META", GSI1PK="TYPE#APP", GSI1SK=APP)
        assert _prehook("discovery")["blockers"] == ["app_name"]

    def test_design_prehook_requires_assessment_summary(self, table):
        _put_meta(table, **REHOST_META)
        _risk_score(table)
        out = _prehook("design", run_started_at=RUN_START)
        assert out["ready"] is False and out["blockers"] == ["assessment_summary"]
        _summary(table)
        assert _prehook("design", run_started_at=RUN_START)["ready"] is True

    def test_design_prehook_requires_the_approved_selections(self, table):
        """Design builds the approved compute/DB/CI-CD — it may not start without them."""
        _put_meta(table, migration_strategy="replatform", target_compute="eks", target_database="aurora_postgresql")
        _summary(table)
        out = _prehook("design", run_started_at=RUN_START)
        assert out["ready"] is False
        assert set(out["blockers"]) == {"target_compute_invalid_for_replatform", "cicd_target"}

    def test_assessment_prehook_completeness_is_advisory(self, table):
        _put_meta(table, **REHOST_META)
        _put(table, "REPO#r1", url="https://git-codecommit.us-east-1.amazonaws.com/v1/repos/r")
        out = _prehook("assessment")
        assert out["ready"] is True
        assert out["warnings"] and "rpo" in out["warnings"][0] and "target_region" in out["warnings"][0]

    def test_completeness_gate_mirror_is_in_sync(self):
        for gate, fields in invoke_runtime._COMPLETENESS_GATES.items():
            assert fields == _completeness.PHASE_GATES[gate]["mandatory"], f"{gate} gate drifted from completeness.py"


# ── 3. Execution pre-hook: every approved selection, valid per strategy ─────

class TestExecutionPrehook:
    def test_valid_rehost_selection_is_ready(self, table):
        _put_meta(table, **REHOST_META)
        _put(table, "REPO#r1", url="https://git-codecommit.us-east-1.amazonaws.com/v1/repos/r")
        out = _prehook("execution")
        assert out["ready"] is True, out

    def test_missing_selections_block(self, table):
        _put_meta(table, migration_strategy="rehost", target_compute="ec2", target_account="987654321981")
        _put(table, "REPO#r1", url="https://git-codecommit.us-east-1.amazonaws.com/v1/repos/r")
        out = _prehook("execution")
        # ec2 also needs its AMI (assessment-approval sizing input)
        assert set(out["blockers"]) == {"target_database", "cicd_target", "vpc_cidr", "ami_id"}

    def test_selections_invalid_for_strategy_block(self, table):
        _put_meta(table, **dict(REHOST_META, target_compute="ecs_fargate", target_database="aurora_postgresql",
                                target_account="12345", vpc_cidr="8.8.0.0/16"))
        _put(table, "REPO#r1", url="https://git-codecommit.us-east-1.amazonaws.com/v1/repos/r")
        out = _prehook("execution")
        assert set(out["blockers"]) == {"target_compute_invalid_for_rehost", "target_database_invalid_for_rehost",
                                        "target_account_invalid", "vpc_cidr_invalid",
                                        "db_instance_class"}  # managed engine needs a class

    @pytest.mark.parametrize("strategy", ["rehost", "relocate", "retain", "replatform", "refactor", "retire", "repurchase"])
    def test_strategy_rules_mirror_strategy_options(self, strategy):
        assert invoke_runtime._compute_options_for(strategy) == _strategy_options.compute_options_for(strategy)
        assert invoke_runtime._database_options_for(strategy) == _strategy_options.database_options_for(strategy)
        assert invoke_runtime._cicd_options_for(strategy) == _strategy_options.cicd_options_for(strategy)
        assert invoke_runtime._VPC_CIDR_RE.pattern == _strategy_options.VPC_CIDR_RE.pattern
        assert (strategy in tool_documents._TRANSFORM_STRATEGIES) == _strategy_options.transforms_allowed_for(strategy)


# ── 2. Validate_Design post-check ────────────────────────────────────────────

BAD_DOC = """# Design
**Status:** Approved
| Parameter | Value |
|---|---|
| Target Compute | Amazon EC2 |
| Database Platform | Amazon RDS PostgreSQL 14 |
| CI/CD | GitHub Actions |
## 1. Executive Summary
"""


class TestValidateDesign:
    def test_latest_stored_version_is_used_not_v1(self, table):
        """APP-TE only had DESIGN#v2 while the old check looked for DESIGN#v1."""
        _put_meta(table, **REHOST_META)
        _put(table, "DESIGN#v2", version="v2", updated_at="2026-10-05T09:17:07Z")
        out = _validate("design", run_started_at=RUN_START)
        assert out["valid"] is True, out
        assert out["design_version"] == "v2"
        assert any("older tool version" in w for w in out["warnings"])

    def test_missing_design_fails(self, table):
        _put_meta(table, **REHOST_META)
        out = _validate("design", run_started_at=RUN_START)
        assert out["valid"] is False and out["missing"] == ["design_document"]

    def test_contradictions_fail_with_blockers(self, table):
        _put_meta(table, **REHOST_META)
        tool_documents.store_design_document({"app_id": APP, "version": "v1", "content": BAD_DOC})
        out = _validate("design", run_started_at=RUN_START, attempt=1)
        assert out["valid"] is False
        assert "design_contradicts_approved_selections" in out["missing"]
        joined = " | ".join(out["blockers"])
        assert "same_as_source" in joined and "codepipeline" in joined
        assert any("Missing design.md section" in w for w in out["warnings"])

    def test_clean_design_passes_with_section_warnings_only(self, table):
        _put_meta(table, **REHOST_META)
        doc = BAD_DOC.replace("Amazon RDS PostgreSQL 14", "PostgreSQL 14 as-is").replace(
            "GitHub Actions", "AWS CodePipeline") + "\n| AWS Account ID | 987654321981 |\n| VPC CIDR | 192.168.0.0/16 |\nRehost.\n"
        tool_documents.store_design_document({"app_id": APP, "version": "v1", "content": doc})
        out = _validate("design", run_started_at=RUN_START)
        assert out["valid"] is True, out
        assert out["blockers"] == []
        assert any("normalized to Pending Review" in w for w in out["warnings"])

    def test_design_from_previous_run_is_stale(self, table):
        _put_meta(table, **REHOST_META)
        _put(table, "DESIGN#v1", version="v1", updated_at="2026-09-01T00:00:00Z")
        out = _validate("design", run_started_at=RUN_START)
        assert out["missing"] == ["design_document_stale"]

    def test_selection_changed_after_check_is_a_contradiction(self, table):
        _put_meta(table, **REHOST_META)
        doc = BAD_DOC.replace("Amazon RDS PostgreSQL 14", "as-is").replace("GitHub Actions", "AWS CodePipeline")
        tool_documents.store_design_document({"app_id": APP, "version": "v1", "content": doc})
        table.update_item(Key={"PK": f"APP#{APP}", "SK": "META"}, UpdateExpression="SET target_account = :c",
                          ExpressionAttributeValues={":c": "111122223333"})
        out = _validate("design", run_started_at=RUN_START)
        assert out["valid"] is False
        assert any("target_account: design was checked against '987654321981'" in b for b in out["blockers"])


# ── 4. Decision summaries carry the new warnings ─────────────────────────────

class TestDecisionSummaries:
    def test_assessment_review_includes_consistency_and_completeness(self, table):
        _put_meta(table, **REHOST_META)
        _summary(table, consistency_warnings=["cost_projection basis prices Aurora"])
        ds = invoke_runtime._build_decision_summary("assessment_review", APP, table)
        assert "cost_projection basis prices Aurora" in ds["warnings"]
        assert any(w.startswith("completeness gate (assessment) missing") for w in ds["warnings"])
        assert "1 consistency warning(s)" in ds["confidence_or_risk"]

    def test_design_review_reports_version_contradictions_and_warnings(self, table):
        _put_meta(table, **REHOST_META)
        tool_documents.store_design_document({"app_id": APP, "version": "v2", "content": BAD_DOC})
        ds = invoke_runtime._build_decision_summary("design_review", APP, table)
        assert "Design v2" in ds["what_changed"]
        assert "design_status=pending_review" in ds["confidence_or_risk"]
        assert ds["contradictions"] and ds["warnings"]

    def test_execution_review_includes_db_warnings(self, table):
        _put_meta(table, **REHOST_META, execution_database_warnings=["Report targets Aurora"])
        ds = invoke_runtime._build_decision_summary("execution_review", APP, table)
        assert ds["warnings"] == ["Report targets Aurora"]

    def test_execution_review_shows_compute_modules_and_notes(self, table):
        _put_meta(table, **dict(REHOST_META, migration_strategy="replatform", target_compute="lambda",
                                target_database="none"))
        _put(table, "EXECUTION#iac#2026-10-01T15:00:00Z", target_compute="lambda",
             modules=["vpc", "lambda", "cicd-role"], notes=["lambda_handler is a placeholder"])
        _put(table, "EXECUTION#cicd#2026-10-01T15:01:00Z", notes=["CI-only pipeline"],
             warnings=["provider GitHub Actions will not run on a codecommit repo"])
        ds = invoke_runtime._build_decision_summary("execution_review", APP, table)
        assert "target_compute=lambda" in ds["what_changed"] and "modules=vpc, lambda, cicd-role" in ds["what_changed"]
        assert "IaC: lambda_handler is a placeholder" in ds["notes"]
        assert ds["warnings"] == ["CI/CD: provider GitHub Actions will not run on a codecommit repo"]

    def test_execution_review_flags_iac_built_for_another_compute(self, table):
        _put_meta(table, **dict(REHOST_META, migration_strategy="replatform", target_compute="lambda"))
        _put(table, "EXECUTION#iac#2026-10-01T15:00:00Z", target_compute="ecs_fargate", modules=["vpc", "ecs-fargate"])
        ds = invoke_runtime._build_decision_summary("execution_review", APP, table)
        assert ds["warnings"][0].startswith("IaC was generated for target_compute=ecs_fargate")

    def test_store_token_persists_summary_with_warnings(self, table):
        _put_meta(table, **REHOST_META)
        _summary(table, consistency_warnings=["x"])
        invoke_runtime.handler({"mode": "store_token", "app_id": APP, "step": "assessment_review", "token": "t"})
        item = table.get_item(Key={"PK": f"APP#{APP}", "SK": "TASK_TOKEN#assessment_review"})["Item"]
        assert "x" in item["decision_summary"]["warnings"]


# ── ASL: Validate_Design between Design and Approval_Design ──────────────────

@pytest.fixture(scope="module")
def states():
    content = re.sub(r"\$\{(\w+)\}", r"placeholder-\1", ASL_PATH.read_text().replace("${ApprovalTimeoutSeconds}", "604800"))
    return json.loads(content)["States"]


class TestValidateDesignAsl:
    def test_design_routes_through_validate_design(self, states):
        assert states["Design"]["Next"] == "Validate_Design"
        vd = states["Validate_Design"]
        payload = vd["Arguments"]["Payload"]
        assert payload["mode"] == "validate_phase" and payload["phase"] == "design"
        assert "Execution.StartTime" in payload["run_started_at"]
        assert vd["Next"] == "Check_Design"
        assert vd["Catch"][0]["Next"] == "MarkFailed"
        assert vd["Retry"]

    def test_check_design_bounded_rerun_then_fail(self, states):
        cd = states["Check_Design"]
        nexts = [c["Next"] for c in cd["Choices"]]
        assert nexts == ["Approval_Design", "MarkFailed"]
        assert "design_attempt >= 2" in cd["Choices"][1]["Condition"]
        assert cd["Default"] == "Design"
        assert "design_attempt" in states["Design"]["Output"]
        assert "design_blockers" in states["Design"]["Arguments"]["Messages"][0]["Content"][0]["Text"]

    def test_assessment_and_design_gates_pass_run_start(self, states):
        for name in ("Validate_Assessment", "PreHook_Design"):
            assert "Execution.StartTime" in states[name]["Arguments"]["Payload"]["run_started_at"], name

    def test_design_prompt_lists_design_md_sections(self, states):
        text = states["Design"]["Arguments"]["Messages"][0]["Content"][0]["Text"]
        for name, _pat in tool_documents._DESIGN_SECTIONS:
            assert name in text, f"Design prompt is missing design.md section {name!r}"
        skill = (_ROOT / "backend" / "resources" / "skills" / "design.md").read_text()
        for name, _pat in tool_documents._DESIGN_SECTIONS:
            assert re.search(rf"^### \d+\. {re.escape(name)}", skill, re.M), f"design.md lacks section {name!r}"
        assert "### RELOCATE" in skill and "### REHOST" in skill


def test_rerun_on_reject_map_matches_the_asl():
    """/approve flips current_step to <phase>_running on reject only for gates whose
    StepRejected Catch re-runs a phase — keep that map in lockstep with the ASL."""
    from backend.src.api.v1.workflow import _RERUN_ON_REJECT

    asl = json.loads((Path(__file__).resolve().parents[3] / "infra/step-functions/migration-workflow.v2.asl.json").read_text().replace("${ApprovalTimeoutSeconds}", "604800"))
    rerun = {}
    for name, st in asl["States"].items():
        if not name.startswith("Approval_"):
            continue
        nxt = next(c["Next"] for c in st.get("Catch", []) if "StepRejected" in c["ErrorEquals"])
        if nxt != "MarkFailed":
            rerun[st["Arguments"]["Payload"]["step"]] = nxt
    assert set(rerun) == set(_RERUN_ON_REJECT)


def test_harness_session_id_meets_the_33_char_minimum():
    sid = invoke_runtime._harness_session_id("chat-APP-CLI")
    assert len(sid) >= 33 and sid.startswith("chat-APP-CLI-")
    assert sid == invoke_runtime._harness_session_id("chat-APP-CLI")  # stable → same Harness session
    long = "x" * 40
    assert invoke_runtime._harness_session_id(long) == long
