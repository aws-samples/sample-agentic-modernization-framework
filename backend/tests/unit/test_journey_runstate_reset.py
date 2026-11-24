"""New-run state + approval-audit fixes from the APP-CLI/HE/TE journey audit.

- _reset_app_for_new_run left exec_*/target_*/strategy_*/risk_*/completed_at from
  the previous run on META.
- APPROVAL_AUDIT items stored only approved/comment — now they carry the run
  (execution_arn) and the inputs persisted at the gate.
- tool_strategy._human_has_approved read approvals across ALL runs, so a previous
  run's discovery approval locked the strategy for the next run.
- WORKFLOW#ACTIVE.strategy went stale (retire) vs META (replatform).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

_LAMBDA_SRC = Path(__file__).resolve().parents[3] / "infra" / "environments" / "agentcore" / "lambda-src"
if str(_LAMBDA_SRC) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_SRC))
os.environ.setdefault("DYNAMODB_TABLE", "amf_portfolio")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

APP = "APP-HE"
RUN_1 = "arn:aws:states:us-east-1:000000000000:execution:amf-agentcore-workflow:APP-HE-20261001-132958"
RUN_2 = "arn:aws:states:us-east-1:000000000000:execution:amf-agentcore-workflow:APP-HE-20261006-090000"

# META after a completed run (fields from /tmp/review/APP-HE/ddb/META.json).
PRIOR_RUN_META = {
    "app_id": APP, "app_name": "HE Health Engine", "tier": "tier_1", "rpo": "15 minutes", "rto": "1 hour",
    "compliance": ["HIPAA"], "risks": ["Compliance scope: HIPAA"],
    "migration_strategy": "replatform", "strategy_confidence": "60",
    "strategy_rationale": "Default: insufficient data for a confident recommendation",
    "status": "assessed", "completed_at": "2026-10-01T10:54:37Z",
    "exec_cicd_status": "committed", "exec_database_status": "done", "exec_infra_status": "validated",
    "exec_infra_validated_at": "2026-10-05T14:12:30Z",
    "target_account": "987654321912", "target_compute": "ecs_fargate", "target_database": "rds_postgresql",
    "cicd_target": "codepipeline", "vpc_cidr": "172.16.0.0/16",
    "transformation_steps": ["AWS/java-version-upgrade", "AWS/java-aws-sdk-v1-to-v2"],
    "risk_score": 46, "risk_tier": "Medium", "risk_scored_at": "2026-10-01T13:30:29Z",
    "design_status": "complete", "assessment_status": "complete",
}


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName="amf_portfolio",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


def _put_meta(table, **fields):
    table.put_item(Item={"PK": f"APP#{APP}", "SK": "META", **fields})


def _meta(table):
    return table.get_item(Key={"PK": f"APP#{APP}", "SK": "META"})["Item"]


def _aws_client():
    client = MagicMock()
    client.list_objects_v2.return_value = {"Contents": [], "IsTruncated": False}
    return client


def _reset(table):
    from backend.src.api.v1 import workflow

    with patch.object(workflow.boto3, "client", return_value=_aws_client()):
        workflow._reset_app_for_new_run(table, APP)


# ── 4. Reset clears every run-derived field, keeps human/L2 inputs ───────────

def test_reset_clears_run_derived_fields(table):
    _put_meta(table, **PRIOR_RUN_META)
    _reset(table)
    meta = _meta(table)
    for f in ("exec_database_status", "exec_infra_status", "exec_cicd_status", "exec_infra_validated_at",
              "transformation_steps", "target_database", "target_account", "vpc_cidr", "target_compute",
              "cicd_target", "strategy_confidence", "strategy_rationale", "risk_score", "risk_tier",
              "risk_scored_at", "completed_at", "design_status", "assessment_status"):
        assert f not in meta, f"{f} leaked into the new run"
    assert meta["status"] == "discovered"


def test_reset_keeps_strategy_and_l2_business_fields(table):
    _put_meta(table, **PRIOR_RUN_META)
    _reset(table)
    meta = _meta(table)
    assert meta["migration_strategy"] == "replatform"
    for f in ("app_name", "tier", "rpo", "rto", "compliance", "risks"):
        assert meta[f] == PRIOR_RUN_META[f]


def test_reset_keeps_spec_seeded_transformation_steps(table):
    spec = [{"td_name": "AWS/java-version-upgrade", "additional_context": "Java 21"}]
    _put_meta(table, **{**PRIOR_RUN_META, "transformation_steps": spec})
    _reset(table)
    assert _meta(table)["transformation_steps"] == spec


def test_reset_restores_l2_preferences(table):
    _put_meta(table, **{**PRIOR_RUN_META, "cicd_target": "jenkins", "l2_preferences": {"cicd_target": "codepipeline"}})
    _reset(table)
    meta = _meta(table)
    assert meta["cicd_target"] == "codepipeline"
    assert "target_compute" not in meta


def test_reset_restores_l2_sizing_preferences(table):
    """ami_id / db_instance_class from L2 survive a new run (2026-10-27)."""
    from backend.src.api.v1.portfolio import L2_PREFERENCE_FIELDS

    assert {"ami_id", "db_instance_class"} <= set(L2_PREFERENCE_FIELDS)
    _put_meta(table, **{**PRIOR_RUN_META, "ami_id": "ami-0123456789abcdef0", "db_instance_class": "db.r6g.large",
                        "l2_preferences": {"ami_id": "al2023", "db_instance_class": "db.t4g.medium"}})
    _reset(table)
    meta = _meta(table)
    assert meta["ami_id"] == "al2023" and meta["db_instance_class"] == "db.t4g.medium"


# ── 5. Approval audit records the run + inputs ───────────────────────────────

async def _approve(table, step, inputs=None):
    from backend.src.api.v1 import workflow

    table.put_item(Item={"PK": f"APP#{APP}", "SK": f"TASK_TOKEN#{step}", "task_token": "tok"})
    req = workflow.ApprovalRequest(app_id=APP, step=step, approved=True, inputs=inputs)
    request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(email="rev@example.com")))
    with patch.object(workflow, "get_table", return_value=table), \
            patch.object(workflow.boto3, "client", return_value=_aws_client()):
        return workflow.approve_step(req, request)


def _audits(table):
    return table.query(
        KeyConditionExpression=Key("PK").eq(f"APP#{APP}") & Key("SK").begins_with("APPROVAL_AUDIT#")
    )["Items"]


def _start_run(table, arn, strategy="retire"):
    table.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE", "status": "RUNNING",
                         "execution_arn": arn, "strategy": strategy})


async def test_assessment_audit_records_run_and_inputs(table):
    _put_meta(table, **PRIOR_RUN_META)
    _start_run(table, RUN_1)
    resp = await _approve(table, "assessment_review", inputs={"target_compute": "ecs_fargate"})
    assert resp.status == "success", resp
    (audit,) = _audits(table)
    assert audit["execution_arn"] == RUN_1
    assert audit["execution_name"] == "APP-HE-20261001-132958"
    assert audit["inputs"] == {
        "target_compute": "ecs_fargate", "target_database": "rds_postgresql", "cicd_target": "codepipeline",
        "target_account": "987654321912", "vpc_cidr": "172.16.0.0/16",
        "transformation_steps": ["AWS/java-version-upgrade", "AWS/java-aws-sdk-v1-to-v2"],
    }
    assert audit["submitted_inputs"] == {"target_compute": "ecs_fargate"}


async def test_discovery_audit_records_strategy_and_syncs_workflow_snapshot(table):
    _put_meta(table, **PRIOR_RUN_META)
    _start_run(table, RUN_1, strategy="retire")
    await _approve(table, "discovery_review")
    (audit,) = _audits(table)
    assert audit["inputs"] == {"migration_strategy": "replatform"}
    wf = table.get_item(Key={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE"})["Item"]
    assert wf["strategy"] == "replatform", "WORKFLOW snapshot must follow the confirmed strategy"


async def test_discovery_gate_override_rewrites_strategy_explanation(table):
    """APP-CLI: reviewer picked relocate; META kept rehost's 45% + default rationale."""
    _put_meta(table, app_id=APP, migration_strategy="relocate", strategy_confidence="45",
              strategy_rationale="Default: insufficient data for a confident recommendation",
              recommended_strategy="rehost", recommended_confidence="45",
              recommended_rationale="Default: insufficient data for a confident recommendation",
              strategy_scores={"rehost": "45"})
    _start_run(table, RUN_1)
    await _approve(table, "discovery_review")
    meta = _meta(table)
    assert meta["strategy_rationale"].startswith("Human-confirmed relocate; engine recommended rehost (45%)")
    assert meta["strategy_confidence"] == "40", "no engine evidence for relocate → floor, not rehost's score"


@pytest.mark.parametrize("strategy,phase", [("repurchase", "handover"), ("retain", "handover"),
                                             ("refactor", "assessment")])
async def test_discovery_approval_moves_to_the_branch_the_workflow_takes(table, strategy, phase):
    """APP-HE/TE (close-out) showed assessment_running until completion and traced
    their close-out tool calls under phase=assessment."""
    _put_meta(table, app_id=APP, migration_strategy=strategy)
    _start_run(table, RUN_1)
    await _approve(table, "discovery_review")
    wf = table.get_item(Key={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE"})["Item"]
    assert (wf["current_phase"], wf["current_step"]) == (phase, f"{phase}_running")


# ── 5. _human_has_approved is scoped to the current run ─────────────────────

@pytest.fixture
def strategy_tool(table, monkeypatch):
    import tool_strategy

    monkeypatch.setattr(tool_strategy, "table", table)
    return tool_strategy


def _audit(table, ts, arn=None, step="discovery_review", approved=True):
    item = {"PK": f"APP#{APP}", "SK": f"APPROVAL_AUDIT#{ts}", "type": "APPROVAL_AUDIT",
            "step": step, "approved": approved}
    if arn:
        item["execution_arn"] = arn
    table.put_item(Item=item)


def test_previous_run_approval_does_not_lock_new_run(table, strategy_tool):
    _put_meta(table, app_id=APP, migration_strategy="retire")
    _audit(table, "2026-10-01T10:00:00Z", arn=RUN_1)
    _audit(table, "2026-09-01T10:00:00Z")  # legacy item, no execution_arn
    _start_run(table, RUN_2)
    assert strategy_tool._human_has_approved(APP, _meta(table)) is False


def test_current_run_approval_locks_strategy(table, strategy_tool):
    _put_meta(table, app_id=APP, migration_strategy="refactor")
    _audit(table, "2026-10-06T10:00:00Z", arn=RUN_2)
    _start_run(table, RUN_2)
    assert strategy_tool._human_has_approved(APP, _meta(table)) is True


def test_current_run_rejection_is_not_a_sign_off(table, strategy_tool):
    _put_meta(table, app_id=APP, migration_strategy="refactor")
    _audit(table, "2026-10-06T10:00:00Z", arn=RUN_2, approved=False)
    _start_run(table, RUN_2)
    assert strategy_tool._human_has_approved(APP, _meta(table)) is False


def test_no_active_run_keeps_legacy_semantics(table, strategy_tool):
    _put_meta(table, app_id=APP, migration_strategy="refactor")
    _audit(table, "2026-09-01T10:00:00Z")
    assert strategy_tool._human_has_approved(APP, _meta(table)) is True


def test_new_run_re_decides_strategy(table, strategy_tool):
    """End to end: a previous run's approval no longer blocks recommend_strategy."""
    _put_meta(table, app_id=APP, migration_strategy="retire", description="a simple lift-and-shift candidate")
    _audit(table, "2026-10-01T10:00:00Z", arn=RUN_1)
    _start_run(table, RUN_2)
    out = strategy_tool.recommend_strategy({"app_id": APP})
    assert out["persisted"] is True
    assert _meta(table)["migration_strategy"] == "rehost"


@pytest.mark.parametrize("live,started", [("FAILED", True), ("RUNNING", False)])
async def test_start_trusts_step_functions_over_the_cached_status(table, live, started):
    """APP-CLI: a run that ended in MarkFailed kept WORKFLOW#ACTIVE=RUNNING and /start refused."""
    from backend.src.api.v1 import workflow

    _put_meta(table, app_id=APP, migration_strategy="replatform")
    _start_run(table, RUN_1, strategy="replatform")
    client = _aws_client()
    client.describe_execution.return_value = {"status": live}
    client.start_execution.return_value = {"executionArn": RUN_1 + "-new"}
    with patch.object(workflow, "get_table", return_value=table), \
            patch.object(workflow.boto3, "client", return_value=client), \
            patch.object(workflow, "_get_sfn_arn", return_value="arn:sm"):
        resp = workflow.start_workflow(workflow.StartWorkflowRequest(app_id=APP))
    assert (getattr(resp, "status", None) == "success") is started, resp
    assert client.start_execution.called is started


@pytest.mark.parametrize("live, cancelled", [("RUNNING", False), ("ABORTED", True)])
def test_cancel_only_succeeds_when_the_execution_is_stopped(table, live, cancelled):
    """APP-CLI 2026-10-13: StopExecution was AccessDenied, cancel still reported success and
    /start launched a second run alongside the one still waiting at design_review."""

    from botocore.exceptions import ClientError

    from backend.src.api.v1 import workflow

    table.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE", "status": "RUNNING", "execution_arn": RUN_1})
    client = _aws_client()
    client.stop_execution.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "StopExecution")
    client.describe_execution.return_value = {"status": live}
    with patch.object(workflow, "get_table", return_value=table), \
            patch.object(workflow.boto3, "client", return_value=client), \
            patch.object(workflow, "_stop_running_worker_tasks"):
        workflow.cancel_workflow(workflow.CancelWorkflowRequest(app_id=APP))
    status = table.get_item(Key={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE"})["Item"]["status"]
    assert (status == "CANCELLED") is cancelled


def test_harness_has_no_long_term_memory():
    """A new run must not inherit facts the reset can't clear (APP-CLI, 2026-10-19).

    Without an explicit memory block AgentCore attaches a managed SEMANTIC memory
    shared by every run and app; the Assessment and Design then reported the
    previous run's Terraform commit and execution steps as done.
    """
    import re

    tf = (_LAMBDA_SRC.parent / "harness.tf").read_text()
    block = tf[tf.index('resource "aws_bedrockagentcore_harness"'):]
    assert re.search(r"memory\s*\{\s*disabled\s*\{\s*\}\s*\}", block)
