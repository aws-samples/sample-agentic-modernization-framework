"""Regressions from the UAT APP-CLI / APP-HE / APP-TE review (2026-10-09).

Root cause: the tool Lambda role lacked dynamodb:BatchGetItem, so get_app_context
failed in every phase and the Harness wrote assessments / close-out documents
blind — APP-CLI's refactor assessment was written for rehost/EC2 and cited an ATX
run that never happened; APP-HE's repurchase close-out listed 34 servers (6 on
record), "no owners / RPO / dependencies" (all set) and dates past the hard
deadline; APP-TE's retain close-out had the wrong risk score, a past-dated
re-evaluation and "no deadline". Compounding it: a bare repo name cloned nothing
yet ATX reported success, and the close-out had no post-check or human gate.
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

from backend.src.core import risk_scoring as _risk_scoring

_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_SRC = _ROOT / "infra" / "environments" / "agentcore" / "lambda-src"
if str(_LAMBDA_SRC) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_SRC))
sys.modules.setdefault("risk_scoring", _risk_scoring)
os.environ.setdefault("DYNAMODB_TABLE", "amf_portfolio")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import invoke_runtime  # noqa: E402
import tool_documents  # noqa: E402
import tool_portfolio  # noqa: E402

APP = "APP-HE"
RUN_START = "2026-10-09T07:53:35Z"
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
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch.object(invoke_runtime, "ddb", _Ddb(t)), patch.object(tool_documents, "table", t), \
                patch.object(tool_documents, "BUCKET", ""), patch.object(tool_portfolio, "table", t):
            yield t


def _put(table, sk, **fields):
    table.put_item(Item={"PK": f"APP#{APP}", "SK": sk, "app_id": APP, **fields})


HE_META = {
    "app_name": "HE Health Engine", "migration_strategy": "repurchase", "architecture": "monolith",
    "deployment_method": "manual", "machine_count": 6, "machine_ids": [f"MC-HE-0{i}" for i in range(1, 7)],
    "rpo": "15 minutes", "rto": "1 hour", "risk_score": 50, "risk_tier": "Medium",
    "dependencies_upstream": ["APP-MCC-ORACLE", "APP-PDB"], "dependencies_downstream": ["APP-RDB", "APP-QIE"],
    "current_cost": {"monthly_infra": "22000", "annual_license": "0"},
    "database": {"engine": "PostgreSQL", "version": "13"},
    "constraints": {"hard_deadline": "2027-01-31", "eosl_date": "2027-11-30", "dc_lease_end": "2027-03-31"},
    "hard_deadline": "2027-01-31", "compliance": ["HIPAA"],
    "integrations": ["identity-provider (SAML)", "central-logging"],
    "change_freeze_windows": ["2026-12-24/2027-01-02 (year-end)"],
}
HE_OWNERS = [{"name": "Grace Kim", "role": "business_owner"}, {"name": "Omar Haddad", "role": "tech_lead"}]

# Trimmed from the stored APP-HE close-out (applications/APP-HE/design/final.md).
HE_BLIND_DOC = """# APP-HE — Close-Out Design Document
**Strategy:** Repurchase (SaaS Replacement)

## 1. Executive Summary
- 34 servers on on-premises VMware vSphere (dc_lease_end: 2027-03-31)
- Prior human-confirmed strategy: Retire (overridden by active-traffic finding)

## 2. Current State
- Missing critical fields: RPO, RTO, owners, dependency map
- Monthly infrastructure cost: **not on file**
- EOSL status: not confirmed on record

## 4. SaaS Replacement Plan
| Production cutover | 2027-02-06 | All Teams |
> **Recommended for HIPAA (if applicable):** confirm BAA.
"""

HE_GOOD_DOC = """# APP-HE — Close-Out Document
**Strategy:** Repurchase

## Executive Summary
HE Health Engine runs on 6 machines (MC-HE-01..06). Owners: Grace Kim, Omar Haddad.
Risk score 50/100 (Medium). RPO 15 minutes, RTO 1 hour. HIPAA applies. Hard deadline 2027-01-31.

## Decision Rationale
Repurchase confirmed at the discovery gate.

## SaaS Replacement Plan
| Production cutover | 2027-01-20 | All Teams |
Upstream APP-MCC-ORACLE, APP-PDB; downstream APP-RDB, APP-QIE. Freeze 2026-12-24 avoided.
"""


# ── 1. get_app_context survives a machine-lookup failure ─────────────────────

class TestGetAppContext:
    def test_machine_lookup_error_still_returns_meta(self, table):
        _put(table, "META", **HE_META)
        _put(table, "OWNER#grace", name="Grace Kim")
        with patch.object(tool_portfolio, "_machines_for", side_effect=RuntimeError("AccessDenied BatchGetItem")):
            out = tool_portfolio.get_app_context({"app_id": APP})
        assert out["meta"]["migration_strategy"] == "repurchase"
        assert out["owners"] and out["machines"] == []
        assert out["unresolved_machine_ids"] == HE_META["machine_ids"]
        assert "AccessDenied" in out["machines_error"]

    def test_tool_role_allows_batch_get_item(self):
        main_tf = (_ROOT / "infra" / "environments" / "agentcore" / "main.tf").read_text()
        block = main_tf.split('resource "aws_iam_role_policy" "tool_lambda_policy"', 1)[1].split("resource ", 1)[0]
        assert '"dynamodb:BatchGetItem"' in block


# ── 2. Assessment written for another strategy is a hard contradiction ──────

class TestAssessmentContradictions:
    CLI_BLIND = {
        "migration_strategy": "rehost", "target_compute": "ec2",
        "target_compute_rationale": "Strategy is rehost (lift-and-shift). Target compute is EC2.",
        "deferred_modernization": [{"item": "Containerisation", "label": "out of scope for rehost"}],
    }

    def test_store_flags_and_overwrites_wrong_strategy(self, table):
        _put(table, "META", migration_strategy="refactor")
        out = tool_documents.store_analysis_result(
            {"app_id": APP, "analysis_type": "assessment-summary", "result": dict(self.CLI_BLIND)})
        joined = " | ".join(out["contradictions"])
        assert "written for 'rehost'" in joined and "never the confirmed 'refactor'" in joined
        assert "out of scope for 'rehost'" in joined
        item = next(i for i in table.scan()["Items"] if i["SK"].startswith("ANALYSIS#assessment-summary#"))
        assert item["result"]["migration_strategy"] == "refactor"
        assert item["consistency_contradictions"] == out["contradictions"]

    def test_invented_account_and_cidr_are_dropped(self, table):
        _put(table, "META", migration_strategy="relocate")
        tool_documents.store_analysis_result({"app_id": APP, "analysis_type": "assessment-summary", "result": {
            "migration_strategy": "relocate", "target_account": "123456789879", "vpc_cidr": "10.0.0.0/16"}})
        item = next(i for i in table.scan()["Items"] if i["SK"].startswith("ANALYSIS#assessment-summary#"))
        assert "target_account" not in item["result"] and "vpc_cidr" not in item["result"]
        assert any("target_account='123456789879'" in w for w in item["consistency_warnings"])

    def test_consistent_refactor_summary_has_none(self, table):
        _put(table, "META", migration_strategy="refactor")
        result = {"migration_strategy": "refactor", "target_compute": "ecs_fargate",
                  "target_compute_rationale": "Refactor to ECS Fargate services (rather than rehost on EC2)."}
        out = tool_documents.store_analysis_result(
            {"app_id": APP, "analysis_type": "assessment-summary", "result": result})
        assert "contradictions" not in out

    def test_validate_phase_blocks_on_contradictions(self, table):
        _put(table, "META", migration_strategy="refactor")
        _put(table, "ANALYSIS#assessment-summary#2026-10-09T08:03:58Z", analysis_type="assessment-summary",
             updated_at="2026-10-09T08:03:58Z", consistency_contradictions=["migration_strategy: written for 'rehost'"])
        out = invoke_runtime.handler({"mode": "validate_phase", "phase": "assessment", "app_id": APP,
                                      "run_started_at": RUN_START})
        assert out["valid"] is False
        assert out["missing"] == ["assessment_contradicts_confirmed_strategy"]
        assert out["blockers"] == ["migration_strategy: written for 'rehost'"]


# ── 3. Close-out documents are checked against the recorded facts ────────────

class TestCloseOutCheck:
    def test_blind_he_document_is_contradicted(self):
        check = tool_documents._check_closeout_document(HE_BLIND_DOC, HE_META, HE_OWNERS, today="2026-10-09")
        joined = " | ".join(check["contradictions"])
        for expected in ("'retire' was human-confirmed", "names 34 server(s) but the app has 6",
                         "owners: document says", "RPO/RTO: document says", "current cost: document says",
                         "EOSL: document says", "compliance: document says", "hard_deadline"):
            assert expected in joined, expected
        assert check["checked_against"] == {"migration_strategy": "repurchase", "close_out": True}

    def test_fact_faithful_document_passes(self):
        check = tool_documents._check_closeout_document(HE_GOOD_DOC, HE_META, HE_OWNERS, today="2026-10-09")
        assert check["contradictions"] == []

    def test_retain_doc_flags_risk_deploy_deadline_and_past_dates(self):
        meta = {**HE_META, "migration_strategy": "retain", "architecture": "microservices",
                "deployment_method": "CI/CD (Jenkins)", "risk_score": 57}
        doc = ("## Executive Summary\nStrategy: Retain. Risk: Low (23/100). Deployment method (manual).\n"
               "No active migration deadline. Assessed as monolith.\n"
               "| DC lease re-evaluation trigger | 2026-09-30 | ⬜ Scheduled |\n")
        joined = " | ".join(tool_documents._check_closeout_document(doc, meta, HE_OWNERS, today="2026-10-09")
                            ["contradictions"])
        for expected in ("states 23/100 but the recorded risk score is 57/100", "deployment is manual",
                         "claims there is no deadline", "dated before today 2026-10-09",
                         "describes a monolith"):
            assert expected in joined, expected

    def test_wrong_fleet_total_is_flagged_even_when_the_right_count_appears(self):
        """Second UAT run (memory recall): '6 servers (MC-HE-01..06), 34-server cluster total'."""
        doc = HE_GOOD_DOC + "\n**Total cluster:** 34 on-premises VMware vSphere servers.\n"
        joined = " | ".join(tool_documents._check_closeout_document(doc, HE_META, HE_OWNERS, today="2026-10-09")
                            ["contradictions"])
        assert "names 34 server(s) but the app has 6" in joined

    def test_risk_rows_and_reference_dates_are_not_false_positives(self):
        """Second UAT run: a correct APP-TE retire close-out was flagged for risk-table prose
        and for prose citing the DC lease end."""
        doc = HE_GOOD_DOC + (
            "| Downstream consumer (APP-RDB) not ready to decouple | High | Medium | Escalate if decoupling plan "
            "not confirmed by 2026-10-30 | Ravi Menon |\n"
            "| api-gateway removal breaks undocumented consumers | High — unknown consumers not captured in "
            "dependency mapping | Low | Traffic analysis | Hannah Weber |\n"
            "Retire (Decommission) by the hard deadline; DC lease end 2027-03-31.\n"
            "| DC Lease End | 2027-03-31 | — |\n")
        assert tool_documents._check_closeout_document(doc, HE_META, HE_OWNERS, today="2026-10-09")["contradictions"] == []

    def test_store_routes_close_out_strategies_to_fact_check(self, table):
        _put(table, "META", **{**HE_META, "migration_strategy": "retain", "cicd_target": "codepipeline"})
        out = tool_documents.store_design_document({"app_id": APP, "version": "final", "content": HE_GOOD_DOC})
        # no AWS-target false positives (APP-TE: "design never names AWS CodePipeline") for a retain close-out
        assert not any("cicd_target" in c or "target_compute" in c for c in out["contradictions"])
        item = table.get_item(Key={"PK": f"APP#{APP}", "SK": "DESIGN#final"})["Item"]
        assert item["consistency_check"]["checked_against"]["close_out"] is True

    def test_validate_closeout_uses_the_design_contract(self, table):
        _put(table, "META", **HE_META)
        for o in HE_OWNERS:
            _put(table, f"OWNER#{o['name']}", **o)
        tool_documents.store_design_document({"app_id": APP, "version": "final", "content": HE_BLIND_DOC})
        out = invoke_runtime.handler({"mode": "validate_phase", "phase": "closeout", "app_id": APP})
        assert out["valid"] is False and out["blockers"]
        tool_documents.store_design_document({"app_id": APP, "version": "final", "content": HE_GOOD_DOC})
        assert invoke_runtime.handler({"mode": "validate_phase", "phase": "closeout", "app_id": APP})["valid"] is True

    def test_handover_review_decision_summary_lists_contradictions(self, table):
        _put(table, "META", **HE_META)
        tool_documents.store_design_document({"app_id": APP, "version": "final", "content": HE_BLIND_DOC})
        summary = invoke_runtime._build_decision_summary("handover_review", APP, table)
        assert summary["contradictions"] and "repurchase" in summary["what_changed"]


# ── 4. Runtime: facts for prompts, repo-url pre-hook, completion state ───────

class TestRuntime:
    def test_resolve_strategy_returns_authoritative_facts(self, table):
        _put(table, "META", **HE_META)
        _put(table, "OWNER#grace", name="Grace Kim", role="business_owner")
        out = invoke_runtime.handler({"mode": "resolve_strategy", "app_id": APP})
        assert out["branch"] == "close_out"
        facts = out["app_facts"]
        for expected in ("confirmed migration_strategy=repurchase", "machines=6", "Grace Kim (business_owner)",
                         "rpo=15 minutes", "hard_deadline=2027-01-31", "risk_score=50/100 (Medium)"):
            assert expected in facts, expected

    def test_app_facts_list_dr_targets_once(self, table):
        dr = {"rpo": "4 hours", "rto": "8 hours", "uptime_sla": "99.9%", "multi_region": False,
              "dr_strategy": "backup & restore (single-region)", "availability_zones": 2,
              "source": "migration-engineer (not auto-discovered)"}
        meta = {"rpo": "4 hours", "rto": "8 hours", "uptime_sla": "99.9%", "dr_availability": dr}
        facts = invoke_runtime._app_facts(table, APP, meta, "refactor")
        assert facts.count("rpo=") == 1 and facts.count("rto=") == 1 and facts.count("uptime_sla=") == 1
        assert "dr=multi_region=False, dr_strategy=backup & restore (single-region), availability_zones=2" in facts
        assert "source=" not in facts

    def test_assessment_prehook_blocks_bare_repo_name(self, table):
        _put(table, "META", migration_strategy="refactor")
        _put(table, "REPO#serverless-java-frameworks-samples", name="serverless-java-frameworks-samples",
             url="serverless-java-frameworks-samples")
        out = invoke_runtime.handler({"mode": "prehook_phase", "phase": "assessment", "app_id": APP})
        assert out["ready"] is False
        assert "repo_url_invalid:serverless-java-frameworks-samples" in out["blockers"]

    def test_assessment_prehook_tolerates_whitespace_around_url(self, table):
        _put(table, "META", migration_strategy="refactor")
        _put(table, "REPO#svc", name="svc", url="\t https://git-codecommit.us-east-1.amazonaws.com/v1/repos/svc")
        out = invoke_runtime.handler({"mode": "prehook_phase", "phase": "assessment", "app_id": APP})
        assert not any(b.startswith("repo_url_invalid") for b in out["blockers"])

    def test_mark_completed_clears_running_step(self, table):
        _put(table, "META", **HE_META)
        _put(table, "WORKFLOW#ACTIVE", status="RUNNING", current_phase="assessment", current_step="assessment_running")
        invoke_runtime.handler({"mode": "mark_completed", "app_id": APP})
        wf = table.get_item(Key={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE"})["Item"]
        assert (wf["status"], wf["current_phase"], wf["current_step"]) == ("COMPLETED", "completed", "completed")


# ── 5. ASL wiring ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def states():
    return json.loads(re.sub(r"\$\{(\w+)\}", r"placeholder-\1", ASL_PATH.read_text().replace("${ApprovalTimeoutSeconds}", "604800")))["States"]


class TestAsl:
    def test_close_out_is_validated_and_gated_before_completion(self, states):
        assert states["Handover_CloseOut"]["Next"] == "Validate_CloseOut"
        assert states["Validate_CloseOut"]["Arguments"]["Payload"]["phase"] == "closeout"
        nexts = [c["Next"] for c in states["Check_CloseOut"]["Choices"]]
        assert nexts == ["Approval_CloseOut", "Handover_CloseOut", "Approval_CloseOut"]
        appr = states["Approval_CloseOut"]
        assert appr["Arguments"]["Payload"]["step"] == "handover_review"
        assert appr["Next"] == "MarkCompleted"
        assert appr["Catch"][0] == {**appr["Catch"][0], "ErrorEquals": ["StepRejected"], "Next": "Handover_CloseOut"}

    def test_prompts_carry_confirmed_strategy_and_facts(self, states):
        assert "app_facts" in states["ResolveStrategy"]["Output"]
        for name in ("Assessment", "Handover_CloseOut"):
            text = states[name]["Arguments"]["Messages"][0]["Content"][0]["Text"]
            assert "$states.input.migration_strategy" in text and "AUTHORITATIVE APP FACTS" in text, name
        close = states["Handover_CloseOut"]["Arguments"]["Messages"][0]["Content"][0]["Text"]
        assert "closeout_blockers" in close and "hard_deadline" in close

    def test_assessment_reruns_once_on_contradictions(self, states):
        ca = states["Check_Assessment"]
        assert [c["Next"] for c in ca["Choices"]] == ["Approval_Assessment", "Assessment"]
        assert "assessment_attempt < 2" in ca["Choices"][1]["Condition"]
        assert ca["Default"] == "MarkFailed"
        assert "assessment_blockers" in states["Assessment"]["Arguments"]["Messages"][0]["Content"][0]["Text"]
        assert "assessment_attempt" in states["Assessment"]["Output"]


    def test_reviewer_retry_comment_reaches_the_rerun(self, states):
        """APP-CLI design gate: the Retry comment was dropped by the StepRejected catch."""
        for gate, phase in (("Approval_Assessment", "Assessment"), ("Approval_Design", "Design"),
                            ("Approval_CloseOut", "Handover_CloseOut")):
            catch = states[gate]["Catch"][0]
            # Assessment goes through Route_AssessmentRetry (Default = the agent phase).
            nxt = catch["Next"] if catch["Next"] != "Route_AssessmentRetry" else states[catch["Next"]]["Default"]
            assert catch["ErrorEquals"] == ["StepRejected"] and nxt == phase
            assert "$parse($states.errorOutput.Cause).comment" in catch["Output"]["reviewer_comment"]
            text = states[phase]["Arguments"]["Messages"][0]["Content"][0]["Text"]
            assert "REVIEWER FEEDBACK" in text and "$states.input.reviewer_comment" in text, phase
            # carried across the bounded post-check re-run
            assert "reviewer_comment" in states[phase]["Output"], phase
        # an approval does not leak a previous gate's comment into the next phase
        assert "reviewer_comment" not in states["Approval_Assessment"]["Output"]


    def test_infra_and_cicd_tool_errors_fail_at_their_own_step(self, states):
        """APP-CLI: provision_infrastructure returned status=error (missing templates), the
        workflow carried on and failed one step later with TerraformStageMissing."""
        for name, nxt in (("InfrastructureProvisioning", "InfrastructureValidate"), ("CICDSetup", "Approval_Execution")):
            assert states[name]["Next"] == f"Check_{name}"
            chk = states[f"Check_{name}"]
            assert chk["Choices"][0]["Condition"] == "{% $states.input.step_status = 'error' %}"
            assert chk["Choices"][0]["Next"] == f"Failed_{name}" and chk["Default"] == nxt
            fail = states[f"Failed_{name}"]
            assert fail["Type"] == "Fail" and "step_error" in fail["Cause"]


# ── 6. ATX worker fails when linked repos analyse nothing ────────────────────

class TestWorker:
    def test_clone_failure_fails_the_callback(self):
        from backend.src.worker import code_analysis

        app = {"repos": [{"url": "serverless-java-frameworks-samples", "branch": "atx-run1"}]}
        with patch.object(code_analysis, "read_full_app", return_value=app), \
                patch.object(code_analysis, "run_atx_analysis",
                             return_value={"status": "no_repos", "message": "No repositories were cloned", "file_count": 0}), \
                patch.object(code_analysis, "_store_analysis_record"), \
                patch.object(code_analysis, "_finalize_job_once", return_value=True) as fin, \
                patch.object(code_analysis, "_succeed") as ok, patch.object(code_analysis, "_fail") as fail, \
                pytest.raises(SystemExit):
            code_analysis.run_code_analysis()
        ok.assert_not_called()
        fail.assert_called_once()
        assert "serverless-java-frameworks-samples" in fail.call_args.args[2]
        assert fin.call_args.args[0] == "FAILED"


def test_dynamodb_target_rejects_aurora_or_dms_plan():
    """APP-CLI design v1 (2026-10-12): approved target_database=dynamodb, design kept the
    monolith data on Aurora MySQL via DMS full-load + CDC."""
    meta = {"migration_strategy": "replatform", "target_compute": "ecs_fargate", "target_database": "dynamodb",
            "cicd_target": "codepipeline"}
    base = ("## Target State Architecture\nTarget Compute: Amazon ECS on AWS Fargate. CI/CD: AWS CodePipeline.\n"
            "Target Database: Amazon DynamoDB (unicorns, baskets).\n")
    bad = base + ("- **Aurora MySQL sizing:** db.r6g.large Multi-AZ\n"
                  "- **Data migration:** AWS DMS full-load + CDC from the source MySQL to Aurora MySQL\n")
    joined = " | ".join(tool_documents._check_design_document(bad, meta)["contradictions"])
    assert "provisions Aurora/RDS" in joined and "plans a DMS migration" in joined
    ok = ("## Current State\nMonolith on MySQL 8.0; CloudFormation defines RDS, Aurora and DMS.\n" + base
          + "No Aurora and no DMS. The assessment's aurora_mysql value is stale.\n")
    assert tool_documents._check_design_document(ok, meta)["contradictions"] == []


def test_cost_reconciliation_against_assessment_estimate_is_not_a_db_plan():
    """APP-TE design (2026-10-13) failed twice: §9 reconciled the assessment ballpark, which had
    assumed Aurora, against the DynamoDB design."""
    meta = {"migration_strategy": "replatform", "target_compute": "ecs_fargate", "target_database": "dynamodb",
            "cicd_target": "codepipeline"}
    doc = ("## Target State Architecture\nTarget Compute: Amazon ECS on AWS Fargate. CI/CD: AWS CodePipeline.\n"
           "Target Database: Amazon DynamoDB.\n## 9. Cost Analysis\n"
           "The assessment-phase preliminary figure was **~$96,000–$120,000/yr**, which included an Aurora "
           "PostgreSQL component (~$18,000/yr at db.r6g.xlarge Multi-AZ). This design uses **Amazon DynamoDB** "
           "(~$6,500/yr) in place of that relational component.\n")
    assert tool_documents._check_design_document(doc, meta)["contradictions"] == []
    # ...but a real Aurora sizing line in the cost table still fails.
    bad = doc + "| Aurora PostgreSQL | db.r6g.xlarge Multi-AZ | ~$18,000/yr |\n"
    assert any("provisions Aurora/RDS" in c for c in tool_documents._check_design_document(bad, meta)["contradictions"])


def test_fallback_options_and_risks_are_not_a_db_plan():
    """APP-TE design (2026-10-13, 2nd run): a risks-table fallback row was read as the plan."""
    meta = {"migration_strategy": "replatform", "target_compute": "ecs_fargate", "target_database": "dynamodb",
            "cicd_target": "codepipeline"}
    base = ("## Target State Architecture\nTarget Compute: Amazon ECS on AWS Fargate. CI/CD: AWS CodePipeline.\n"
            "Target Database: Amazon DynamoDB.\n")
    risks = ("## 8. Risks and Mitigations\n| Risk | Impact | Mitigation | Owner |\n"
             "| DynamoDB redesign slips | High | Revert to a managed relational database path (lower risk, "
             "DMS-based migration) | Ravi Menon |\n")
    option = "## 4. Migration Approach\nOption B (not chosen): keep PostgreSQL on Aurora via AWS DMS.\n"
    for doc in (base + risks, base + option):
        assert tool_documents._check_design_document(doc, meta)["contradictions"] == [], doc
    # The plan itself still can't move the data with DMS into a relational target.
    plan = base + "## 4. Migration Approach\nMigrate the data with AWS DMS full-load + CDC into Aurora PostgreSQL.\n"
    joined = " | ".join(tool_documents._check_design_document(plan, meta)["contradictions"])
    assert "plans a DMS migration" in joined and "provisions Aurora/RDS" in joined


def test_design_cannot_dismiss_approved_selection_or_drop_transformations():
    """APP-CLI design (2026-10-12, 2nd run): 'the META field `dynamodb` is a stale template default'
    → planned same_as_source, and never mentioned the approved Graviton transformation."""
    meta = {"migration_strategy": "replatform", "target_compute": "ecs_fargate", "target_database": "dynamodb",
            "cicd_target": "codepipeline", "transformation_steps": ["AWS/early-access-java-x86-to-graviton"]}
    head = "## Target State Architecture\n| Target Compute | Amazon ECS on AWS Fargate |\n| CI/CD Target | AWS CodePipeline |\n"
    bad = head + ("| Target Database | same_as_source (the META field `dynamodb` is a stale template default "
                  "superseded by the assessment) |\n")
    joined = " | ".join(tool_documents._check_design_document(bad, meta)["contradictions"])
    assert "dismisses the approved target_database 'dynamodb'" in joined
    assert "never plans the approved transformation(s) AWS/early-access-java-x86-to-graviton" in joined
    good = head + ("| Target Database | Amazon DynamoDB (confirmed in META; the assessment's same-as-source value is "
                   "superseded) |\n## 10. Code Transformations\nAWS/early-access-java-x86-to-graviton\n")
    assert tool_documents._check_design_document(good, meta)["contradictions"] == []



def test_execution_database_not_required_for_a_replacing_target_is_flagged():
    """APP-CLI execution-database (2026-10-12): status=not_required, 'source and target are both
    DynamoDB', while the report's data_access_layer_change rewrites MyBatis/MySQL; plus a
    recalled confirmed_done_at=2026-10-05."""
    result = {"status": "not_required", "assessed_at": "2026-10-12", "confirmed_done_at": "2026-10-05",
              "migration_method": "not_applicable — source and target are both DynamoDB",
              "data_access_layer_change": {"description": "Replace MyBatis/MySQL data-access layer with DynamoDB"}}
    joined = " | ".join(tool_documents._stamp_execution_database(result, {"target_database": "dynamodb"}))
    assert "no database migration is required, but target_database='dynamodb'" in joined
    assert "MySQL/relational source" in joined
    assert "confirmed_done_at='2026-10-05' predates this report" in joined
    assert tool_documents._stamp_execution_database(
        {"status": "planned", "assessed_at": "2026-10-12", "target_service": "Amazon DynamoDB"},
        {"target_database": "dynamodb"}) == []
