"""Assessment / design / execution-database consistency gates (tool_documents Lambda).

Journey audit (APP-TE / APP-CLI / APP-HE): the Harness ignores "MUST" prompt rules, so
the stored assessment-summary contradicted the chosen strategy (Fargate/Lambda/Aurora
for an EC2 rehost, Java/Spring upgrade steps for rehost/relocate), the design ignored
the approved selections (RDS + DMS for same_as_source, CodePipeline for github_actions,
Aurora for rds_postgresql, "Status: Approved" before the gate), and the
execution-database report picked Aurora for rds_postgresql and invented an RDS source.
These are now enforced / flagged deterministically in store_analysis_result and
store_design_document. Fixtures are trimmed copies of the audit dumps.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from backend.src.core import risk_scoring as _risk_scoring

_LAMBDA_SRC = Path(__file__).resolve().parents[3] / "infra" / "environments" / "agentcore" / "lambda-src"
if str(_LAMBDA_SRC) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_SRC))
sys.modules.setdefault("risk_scoring", _risk_scoring)
os.environ.setdefault("DYNAMODB_TABLE", "amf_portfolio")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import tool_documents  # noqa: E402

APP = "APP-TE"
RUN_ID = "20261001_141149_b6b122b9"


def _create_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(
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


REHOST_META = {
    "migration_strategy": "rehost", "target_compute": "ec2", "target_database": "same_as_source",
    "cicd_target": "github_actions", "target_account": "987654321981", "vpc_cidr": "192.168.0.0/16",
    "database": {"engine": "PostgreSQL", "version": "14", "size_gb": 150, "licensing": "open_source"},
}


@pytest.fixture
def table():
    with mock_aws():
        t = _create_table()
        with patch.object(tool_documents, "table", t), patch.object(tool_documents, "BUCKET", ""):
            yield t


def _put_meta(table, **fields):
    table.put_item(Item={"PK": f"APP#{APP}", "SK": "META", "GSI1PK": "TYPE#APP", "GSI1SK": APP,
                         "app_id": APP, "app_name": "TE Application", **fields})


def _meta(table):
    return table.get_item(Key={"PK": f"APP#{APP}", "SK": "META"})["Item"]


def _put_atx(table):
    table.put_item(Item={
        "PK": f"APP#{APP}", "SK": "ANALYSIS#comprehensive-codebase-analysis#2026-10-01T14:28:09.701003+00:00",
        "GSI1PK": "TYPE#ANALYSIS", "GSI1SK": f"{APP}#comprehensive-codebase-analysis",
        "app_id": APP, "analysis_type": "comprehensive-codebase-analysis", "run_id": RUN_ID,
        "timestamp": "2026-10-01T14:28:09.701003+00:00", "status": "completed",
        "s3_prefix": f"s3://bucket/applications/{APP}/analysis/comprehensive-codebase-analysis/{RUN_ID}/",
    })
    table.put_item(Item={
        "PK": "JOB#analysis-APP-TE-1a2b3c4d", "SK": "META", "GSI1PK": "TYPE#JOB",
        "GSI1SK": "analysis-APP-TE-1a2b3c4d", "job_id": "analysis-APP-TE-1a2b3c4d", "app_id": APP,
        "mode": "code-analysis", "status": "COMPLETED", "atx_run_id": RUN_ID, "created_at": "2026-10-01T14:05:00Z",
    })


def _te_assessment():
    """Trimmed APP-TE assessment-summary as the Harness stored it (rehost/ec2 app)."""
    return {
        "target_compute": "ec2",
        "cicd_target": "codepipeline",
        "atx_job_id": "analysis-APP-TE-b499549e",
        "atx_run_id": RUN_ID,
        "analysis_completed_at": "2026-09-16",
        "target_compute_rationale": (
            "Application is already containerized on Amazon ECS with 5 microservices. ECS Fargate eliminates "
            "EC2 cluster management overhead. This is confirmed as the target_compute for the replatform strategy."
        ),
        "cost_projection": {
            "current_annual_cost": {"value": 300000, "status": "provided", "source": "customer_input"},
            "target_ballpark_annual_cost": {
                "basis": "5 ECS Fargate services x 2 tasks + Aurora PostgreSQL db.r6g.large Multi-AZ + ALB",
                "source": "amf_estimate", "value": "~$96,000-$120,000/yr",
            },
        },
        "migration_order": [
            "Phase 1: AWS/java-version-upgrade (Java 8→17, all 11 pom.xml + 14 Dockerfiles)",
            "Phase 2: AWS/spring-boot-version-upgrade (1.5.2→3.2, javax→jakarta namespace)",
            "Phase 4: Infrastructure Fargate migration (provision_infrastructure → Terraform ECS Fargate)",
            "Phase 6: Security hardening (Spring Security, Actuator, HTTPS/TLS)",
        ],
        "recommendations": [
            "PRIORITY 1 — Run AWS/java-version-upgrade (Java 8→17) across all 11 pom.xml files",
            "SECURITY — Add HTTPS/TLS on ALB (ACM certificate) — HTTP only currently configured",
            "ARCHITECTURE — Replace Spotify Docker Maven Plugin 0.4.13 with Jib (engineer-authored gap)",
        ],
        "containerization_readiness": {"status": "ready", "evidence": ["14 Dockerfiles already present"]},
        "qualification_criteria": [
            {"criterion": "dependency_count", "value": "4", "threshold": "<=20", "pass": True},
            {"criterion": "runtime_support", "pass": True,
             "value": "Java 8 (EOL) → upgrading to Java 17 via AWS/java-version-upgrade (ATX GA catalog TD)"},
        ],
        "transformations": {"recommended": [{"td_name": "AWS/java-version-upgrade"}], "gaps": []},
    }


def _store_assessment(table, result):
    tool_documents.store_analysis_result({"app_id": APP, "analysis_type": "assessment-summary", "result": result})
    from boto3.dynamodb.conditions import Key

    items = table.query(
        KeyConditionExpression=Key("PK").eq(f"APP#{APP}") & Key("SK").begins_with("ANALYSIS#assessment-summary#")
    )["Items"]
    assert len(items) == 1
    return items[0]


# ── 1. Assessment-summary consistency ────────────────────────────────────────

class TestAssessmentConsistency:
    def test_replatform_does_not_add_the_security_fixer(self, table):
        """AMF ships no scanner, so the fixer (which needs findings) is no longer forced in."""
        _put_meta(table, **{**REHOST_META, "migration_strategy": "refactor", "target_compute": "lambda",
                            "target_database": "dynamodb"})
        _put_atx(table)
        result = _te_assessment()
        result["transformations"] = {"recommended": [{"td_name": "AWS/java-version-upgrade", "order": 1}],
                                     "gaps": []}
        rec = _store_assessment(table, result)["result"]["transformations"]["recommended"]
        assert [r["td_name"] for r in rec] == ["AWS/java-version-upgrade"]

    def test_rehost_strips_transformation_recommendations_into_deferred(self, table):
        _put_meta(table, **REHOST_META)
        _put_atx(table)
        item = _store_assessment(table, _te_assessment())
        result = item["result"]
        assert result["transformations"] == {"recommended": [], "gaps": []}
        assert result["target_compute"] == "ec2"
        assert result["migration_order"] == ["Phase 6: Security hardening (Spring Security, Actuator, HTTPS/TLS)"]
        assert result["recommendations"] == [
            "SECURITY — Add HTTPS/TLS on ALB (ACM certificate) — HTTP only currently configured"
        ]
        assert result["containerization_readiness"]["status"] == "not_applicable"
        runtime = next(r for r in result["qualification_criteria"] if r["criterion"] == "runtime_support")
        assert runtime["value"] == "Java 8 (EOL)"
        deferred = result["deferred_modernization"]
        assert "out of scope for rehost".lower() in deferred["label"].lower()
        fields = {d["field"] for d in deferred["items"]}
        assert {"migration_order", "recommendations", "containerization_readiness",
                "qualification_criteria.runtime_support"} <= fields
        assert all(d["reason"] == "out of scope for rehost" for d in deferred["items"])

    def test_rehost_flags_compute_cost_and_strategy_mismatch_without_rewriting_numbers(self, table):
        _put_meta(table, **REHOST_META)
        _put_atx(table)
        item = _store_assessment(table, _te_assessment())
        warnings = item["consistency_warnings"]
        joined = " | ".join(warnings)
        assert "target_compute_rationale names ECS Fargate" in joined
        assert "cost_projection.target_ballpark_annual_cost names ECS Fargate" in joined
        assert "prices Aurora" in joined
        assert "'replatform' strategy" in joined
        assert "cicd_target='codepipeline'" in joined
        # numbers we cannot compute are flagged, not rewritten
        ballpark = item["result"]["cost_projection"]["target_ballpark_annual_cost"]
        assert ballpark["value"] == "~$96,000-$120,000/yr"
        assert item["result"]["cost_projection"]["consistency_flag"]
        assert item["result"]["cicd_target"] == "github_actions"
        # mirrored onto META for the assessment_review decision_summary
        assert _meta(table)["assessment_warnings"] == warnings

    def test_atx_provenance_is_overwritten_from_real_job(self, table):
        _put_meta(table, **REHOST_META)
        _put_atx(table)
        result = _store_assessment(table, _te_assessment())["result"]
        assert result["atx_job_id"] == "analysis-APP-TE-1a2b3c4d"
        assert result["atx_run_id"] == RUN_ID
        assert result["analysis_completed_at"] == "2026-10-01T14:28:09.701003+00:00"
        assert result["atx_s3_prefix"].endswith(f"{RUN_ID}/")
        assert any("atx_job_id='analysis-APP-TE-b499549e'" in w for w in result["consistency_warnings"])
        assert any("analysis_completed_at='2026-09-16'" in w for w in result["consistency_warnings"])

    def test_no_atx_record_clears_invented_provenance(self, table):
        _put_meta(table, **REHOST_META)
        result = _store_assessment(table, _te_assessment())["result"]
        assert result["atx_job_id"] is None and result["atx_run_id"] is None
        assert any("No ATX code-analysis record" in w for w in result["consistency_warnings"])

    def test_report_md_is_markdown_not_json(self, table):
        _put_meta(table, **REHOST_META)
        _put_atx(table)
        item = _store_assessment(table, _te_assessment())
        md = item["data"]["report_md"]
        assert md.startswith("# Assessment Summary — ")
        assert not md.lstrip().startswith("{")
        assert "## Deferred Modernization" in md

    def test_agent_markdown_report_is_kept(self, table):
        _put_meta(table, **REHOST_META)
        _put_atx(table)
        result = _te_assessment()
        result["report_md"] = "# Assessment\n\nAll good."
        item = _store_assessment(table, result)
        assert item["data"]["report_md"] == "# Assessment\n\nAll good."
        assert "report_md" not in item["result"]

    def test_replatform_keeps_transformations_and_flags_alternative_compute(self, table):
        _put_meta(table, migration_strategy="replatform", target_compute="ecs_fargate",
                  target_database="rds_postgresql", cicd_target="codepipeline")
        _put_atx(table)
        result = {
            "target_compute": "eks",
            "target_compute_rationale": "Run on Amazon EKS with Karpenter for the microservices.",
            "transformations": {"recommended": [{"td_name": "AWS/java-version-upgrade"}], "gaps": []},
            "migration_order": ["Phase 1: AWS/java-version-upgrade"],
        }
        item = _store_assessment(table, result)
        rec = item["result"]["transformations"]["recommended"]
        assert [r["td_name"] for r in rec] == ["AWS/java-version-upgrade"]
        assert item["result"]["migration_order"] == ["Phase 1: AWS/java-version-upgrade"]
        assert item["result"]["target_compute"] == "ecs_fargate"
        assert "deferred_modernization" not in item["result"]
        joined = " | ".join(item["consistency_warnings"])
        assert "proposed target_compute='eks'" in joined
        assert "target_compute_rationale names EKS" in joined

    def test_negated_or_source_mentions_are_not_flagged(self, table):
        _put_meta(table, **REHOST_META)
        _put_atx(table)
        result = {
            "target_compute": "ec2",
            "target_compute_rationale": "Currently on ECS Fargate in the source DC; rehost moves the servers to EC2 as-is.",
            "cost_projection": {"target_ballpark_annual_cost": {"basis": "2 x m6i.large EC2 + EBS; no Aurora"}},
        }
        item = _store_assessment(table, result)
        assert not any("names" in w or "Aurora" in w for w in item["consistency_warnings"])


# ── 2. Design document consistency + META design_version/status ─────────────

TE_DESIGN = """# Migration Design Document — TE Application (APP-TE)
**Version:** V2 (Execution-Ready)
**Status:** Approved — Ready for Execution
**Strategy:** Rehost (Lift & Shift to EC2)

## Target Infrastructure Parameters

| Parameter | Value |
|-----------|-------|
| AWS Account ID | 987654321981 |
| VPC CIDR | 192.168.0.0/16 |
| Compute | Amazon EC2 (Rehost — lift & shift) |
| Database Platform | Amazon RDS PostgreSQL 14 (Multi-AZ) |
| CI/CD | AWS CodePipeline + CodeBuild + CodeCommit |

## 1. Executive Summary
Rehost of the application.
## 2. Current State
Runs on ECS-on-EC2 today.
## 3. Migration Strategy & Rationale
## 4. Assessment Findings
## 5. Target Architecture
### 5.3 Compute Strategy
**Target Compute: Amazon EC2 (Rehost — Lift & Shift)**
### 5.4 Database Strategy
2. **AWS Database Migration Service (DMS):** Full-load + Change Data Capture from source PostgreSQL to RDS
## 6. Migration Plan
## 8. AWS Services Summary
## 9. Cost Analysis
## 10. Code Transformations
## 11. CI/CD Pipeline
**CI/CD Target: AWS CodePipeline + CodeBuild + CodeCommit (confirmed)**
## 12. Risks & Mitigations
## 13. Deployment Readiness Checklist
"""

GOOD_REHOST_DESIGN = """# Migration Design Document — TE Application (APP-TE)
**Status:** Pending Review
**Strategy:** Rehost (Lift & Shift to EC2)

## Target Infrastructure Parameters

| Parameter | Value |
|---|---|
| AWS Account ID | 987654321981 |
| Target Region | us-east-1 |
| VPC CIDR | 192.168.0.0/16 |
| Target Compute | Amazon EC2 (m6i.xlarge, MGN) |
| Target Database | PostgreSQL 14 on EC2 — same as source, migrated as-is (no DMS) |
| CI/CD Target | GitHub Actions |
| Migration Strategy | Rehost |

## 1. Executive Summary
Today the app runs on ECS and Fargate is not used; rehost moves it to EC2.
## 2. Current State
## 3. Target State Architecture
## 4. Migration Approach
## 5. Testing & Validation
## 6. Operational Readiness
RPO 15 minutes / RTO 1 hour.
## 7. Timeline & Milestones
## 8. Risks & Mitigations
Day-2: consider moving to Aurora later (not in scope).
## 9. Cost Analysis
## 10. Code Transformations
Not applicable — rehost migrates the code as-is.
## 11. CI/CD Pipeline
## 12. Deployment Readiness Checklist
"""


def _design_item(table, version):
    return table.get_item(Key={"PK": f"APP#{APP}", "SK": f"DESIGN#{version}"})["Item"]


class TestDesignConsistency:
    def test_te_design_contradictions_are_detected(self):
        check = tool_documents._check_design_document(TE_DESIGN, REHOST_META)
        joined = " | ".join(check["contradictions"])
        assert "design moves the database to RDS" in joined
        assert "plans a DMS migration" in joined
        # Only CodePipeline is recognised now, so a design naming another CI tool is no longer flagged.
        assert not any(c.startswith("cicd_target") for c in check["contradictions"])
        assert not any(c.startswith("target_compute") for c in check["contradictions"])
        assert {"Testing & Validation", "Operational Readiness", "Timeline & Milestones"} <= set(check["missing_sections"])

    def test_good_rehost_design_has_no_contradictions(self):
        meta = dict(REHOST_META, rpo="15 minutes", rto="1 hour")
        check = tool_documents._check_design_document(GOOD_REHOST_DESIGN, meta)
        assert check["contradictions"] == []
        assert check["missing_sections"] == []
        assert check["warnings"] == []

    @pytest.mark.parametrize("compute,target_line,expect", [
        ("ec2", "| Target Compute | Amazon ECS Fargate |", "design proposes ECS Fargate"),
        ("ec2", "| Target Compute | AWS Lambda + API Gateway |", "design proposes Lambda"),
        ("ecs_fargate", "| Target Compute | Amazon EKS |", "design proposes EKS"),
        ("ecs_fargate", "| Target Compute | Amazon ECS |", "design never names ECS Fargate"),
    ])
    def test_compute_contradictions(self, compute, target_line, expect):
        meta = {"migration_strategy": "replatform", "target_compute": compute}
        doc = f"| Parameter | Value |\n|---|---|\n{target_line}\n"
        check = tool_documents._check_design_document(doc, meta)
        assert any(expect in c for c in check["contradictions"]), check["contradictions"]

    def test_source_lambda_mentions_do_not_contradict_ec2(self):
        """APP-CLI: relocate of a Lambda codebase — the doc describes the SOURCE Lambdas at length."""
        doc = (
            "**Strategy:** Relocate (Lift-and-Shift to EC2)\n"
            "| Parameter | Value |\n|---|---|\n"
            "| Compute Platform | Amazon EC2 |\n"
            "| Lambda Runtime (source) | Java 11 |\n"
            "| Database | Amazon DynamoDB PAY_PER_REQUEST (same as source — no migration required) |\n"
            "| CI/CD Target | GitLab CI/CD |\n"
            "The workload currently runs on AWS Lambda. **No DMS or data migration required.**\n"
            "**Target compute: Amazon EC2 (confirmed)**\n"
        )
        meta = {"migration_strategy": "relocate", "target_compute": "ec2", "target_database": "same_as_source",
                "cicd_target": "gitlab"}
        assert tool_documents._check_design_document(doc, meta)["contradictions"] == []

    # The user-selected compute is authoritative for every option (replatform/refactor:
    # ecs_fargate | ec2 | lambda) — a design that substitutes another one must fail.
    @pytest.mark.parametrize("compute,target_line,expect", [
        ("lambda", "| Target Compute | Amazon ECS |", "design proposes ECS"),
        ("lambda", "| Target Compute | Amazon EC2 Auto Scaling group |", "design proposes EC2"),
        ("lambda", "| Target Compute | Amazon ECS Fargate |", "design proposes ECS Fargate"),
        ("ecs_fargate", "| Target Compute | Amazon EC2 Auto Scaling group |", "design proposes EC2"),
        ("ecs_fargate", "| Target Compute | AWS Lambda + API Gateway |", "design proposes Lambda"),
        ("ec2", "| Target Compute | Amazon ECS Fargate |", "design proposes ECS Fargate"),
    ])
    def test_every_selectable_compute_is_enforced(self, compute, target_line, expect):
        meta = {"migration_strategy": "replatform", "target_compute": compute}
        doc = f"| Parameter | Value |\n|---|---|\n{target_line}\n"
        check = tool_documents._check_design_document(doc, meta)
        assert any(expect in c for c in check["contradictions"]), check["contradictions"]

    def test_compute_section_prose_override_is_a_contradiction(self):
        """design.md once told the Harness to swap Lambda for Fargate on high TPS."""
        doc = ("| Parameter | Value |\n|---|---|\n| Target Compute | AWS Lambda |\n"
               "### 3.3 Compute Strategy\n"
               "Peak TPS exceeds 1000, so we deploy on ECS Fargate instead.\n"
               "## 4. Data\n")
        meta = {"migration_strategy": "replatform", "target_compute": "lambda"}
        check = tool_documents._check_design_document(doc, meta)
        assert any("design proposes ECS Fargate" in c for c in check["contradictions"]), check["contradictions"]

    def test_approved_compute_named_only_outside_compute_statements_fails(self):
        doc = ("| Parameter | Value |\n|---|---|\n| Target Compute | Amazon ECS (managed containers) |\n"
               "## 12. Risks\nFargate pricing may change.\n")
        meta = {"migration_strategy": "replatform", "target_compute": "ecs_fargate"}
        check = tool_documents._check_design_document(doc, meta)
        assert any("design never names ECS Fargate" in c for c in check["contradictions"]), check["contradictions"]

    @pytest.mark.parametrize("compute,doc", [
        ("lambda", "| Parameter | Value |\n|---|---|\n| Target Compute | AWS Lambda + HTTP API Gateway |\n"
                   "### 3.3 Compute Strategy\nThe API runs on AWS Lambda; containers (ECS Fargate) were "
                   "considered and rejected.\nNo EC2 instances are managed.\n"),
        ("ecs_fargate", "| Parameter | Value |\n|---|---|\n| Target Compute | Amazon ECS on Fargate |\n"
                        "### 3.3 Compute Strategy\nThe service runs on ECS Fargate, which removes EC2 instance "
                        "management. Lambda is not used.\n"),
        ("ec2", "| Parameter | Value |\n|---|---|\n| Target Compute | Amazon EC2 (launch template + ASG) |\n"
                "### 3.3 Compute Strategy\nThe app moves to EC2 instances behind an internal ALB; the database "
                "moves to Aurora PostgreSQL.\n"),
    ])
    def test_clean_design_per_compute_has_no_compute_contradiction(self, compute, doc):
        meta = {"migration_strategy": "replatform", "target_compute": compute}
        check = tool_documents._check_design_document(doc, meta)
        assert not [c for c in check["contradictions"] if c.startswith("target_compute")], check["contradictions"]

    def test_retain_is_a_close_out_not_ec2(self):
        assert tool_documents._expected_compute("retain", {"target_compute": "ec2"}) == "none"

    def test_rds_postgresql_designed_as_aurora_is_a_contradiction(self):
        """APP-HE: target_database=rds_postgresql but the design targets Aurora."""
        doc = (
            "| Parameter | Value |\n|---|---|\n| Target Compute | Amazon ECS Fargate |\n"
            "| Target Database | Amazon Aurora PostgreSQL (db.r6g.large, Multi-AZ) |\n"
            "**Primary Database: PostgreSQL 13 → Amazon Aurora PostgreSQL**\n"
        )
        meta = {"migration_strategy": "replatform", "target_compute": "ecs_fargate", "target_database": "rds_postgresql"}
        check = tool_documents._check_design_document(doc, meta)
        assert any("design targets Aurora" in c for c in check["contradictions"])

    def test_no_aurora_negation_is_not_a_contradiction(self):
        doc = ("| Parameter | Value |\n|---|---|\n| Target Compute | Amazon ECS Fargate |\n"
               "| Target Database | Amazon RDS for PostgreSQL 16 (no Aurora) |\n")
        meta = {"migration_strategy": "replatform", "target_compute": "ecs_fargate", "target_database": "rds_postgresql"}
        assert tool_documents._check_design_document(doc, meta)["contradictions"] == []

    def test_account_cidr_and_strategy_mismatch(self):
        doc = ("**Strategy:** Replatform → ECS\n| Parameter | Value |\n|---|---|\n"
               "| AWS Account ID | 111111111111 |\n| VPC CIDR | 10.9.0.0/16 |\n| Compute | Amazon EC2 |\n")
        check = tool_documents._check_design_document(doc, REHOST_META)
        joined = " | ".join(check["contradictions"])
        assert "AWS Account ID 111111111111" in joined
        assert "VPC CIDR 10.9.0.0/16" in joined
        assert "strategy 'replatform'" in joined

    def test_premature_approved_status_is_normalized(self):
        content, original = tool_documents._normalize_design_status(TE_DESIGN)
        assert original == "Approved — Ready for Execution"
        assert "**Status:** Pending Review" in content
        assert "Approved — Ready for Execution" not in content
        same, none = tool_documents._normalize_design_status("**Status:** Pending Review\n## 1. Executive Summary\n")
        assert none is None

    def test_store_design_sets_meta_version_status_and_check(self, table):
        _put_meta(table, **REHOST_META)
        resp = tool_documents.store_design_document({"app_id": APP, "version": "v2", "content": TE_DESIGN})
        assert resp["design_status"] == "pending_review"
        assert resp["contradictions"], "agent must see the contradictions to self-correct"
        meta = _meta(table)
        assert meta["design_version"] == "v2"
        assert meta["design_status"] == "pending_review"
        assert meta["design_generated_at"]
        assert meta["design_contradictions"] == resp["contradictions"]
        item = _design_item(table, "v2")
        check = item["consistency_check"]
        assert check["status_normalized_from"] == "Approved — Ready for Execution"
        assert any("normalized to Pending Review" in w for w in check["warnings"])
        assert check["checked_against"]["target_database"] == "same_as_source"


# ── 5. Execution-database consistency stamp ──────────────────────────────────

HE_DB_REPORT = {
    "status": "required",
    "migration_method": "AWS DMS Full Load + CDC",
    "source_database": {"engine": "PostgreSQL", "version": "13", "size_gb": 100,
                        "service": "Amazon RDS", "ha_mode": "single-AZ"},
    "target_database": {"engine": "Aurora PostgreSQL", "service": "Amazon Aurora", "version": "15-compatible"},
}


class TestExecutionDatabase:
    def _store(self, table, result):
        tool_documents.store_analysis_result({"app_id": APP, "analysis_type": "execution-database", "result": result})
        from boto3.dynamodb.conditions import Key

        return table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{APP}") & Key("SK").begins_with("ANALYSIS#execution-database#")
        )["Items"][0]

    def test_aurora_for_rds_postgresql_and_invented_source_are_flagged(self, table):
        _put_meta(table, migration_strategy="replatform", target_database="rds_postgresql",
                  database={"engine": "PostgreSQL", "version": "13", "size_gb": 100, "licensing": "open_source"})
        item = self._store(table, dict(HE_DB_REPORT))
        result = item["result"]
        assert result["confirmed_target_database"] == "rds_postgresql"
        assert result["confirmed_target_service"] == "Amazon RDS for PostgreSQL"
        assert result["source_database_discovered"]["engine"] == "PostgreSQL"
        joined = " | ".join(item["consistency_warnings"])
        assert "Report targets Aurora" in joined
        assert "source_database.service='Amazon RDS' is not supported by discovery data" in joined
        assert _meta(table)["execution_database_warnings"] == item["consistency_warnings"]
        # the report body stays JSON for the UI's structured renderer
        assert item["data"]["report_md"].lstrip().startswith("{")

    def test_matching_report_has_no_warnings(self, table):
        _put_meta(table, migration_strategy="replatform", target_database="rds_postgresql",
                  database={"engine": "PostgreSQL", "version": "13"})
        report = {"status": "required", "source_database": {"engine": "PostgreSQL", "version": "13"},
                  "target_database": {"engine": "PostgreSQL 16", "service": "Amazon RDS for PostgreSQL"}}
        assert self._store(table, report)["consistency_warnings"] == []

    def test_same_as_source_keeps_not_required_gate(self, table):
        _put_meta(table, **REHOST_META)
        item = self._store(table, dict(HE_DB_REPORT))
        assert item["result"]["status"] == "not_required"
        assert item["consistency_warnings"] == []


# ── Design must not plan transformations the gate did not approve ────────────

_TD_DESIGN = """# Migration Design Document
| AWS Account ID | 111122223333 |
## 10. Code Transformations
### 10.1 ATX-Executable Transformations (Confirmed)
| 1 | `AWS/java-version-upgrade` | All modules | 3–6 days |
| 2 | `AWS/spring-boot-version-upgrade` | springboot | 3–5 days |
## 11. CI/CD Pipeline
Uses AWS/codepipeline-style stages.
"""


def _td_contradictions(meta):
    return [c for c in tool_documents._check_design_document(_TD_DESIGN, meta)["contradictions"]
            if c.startswith("transformation_steps")]


def test_design_planning_unapproved_transformations_is_a_contradiction():
    """APP-CLI 2026-10-19: approved with no transformations; §10 listed three as confirmed."""
    found = _td_contradictions({"migration_strategy": "refactor", "transformation_steps": []})
    assert found and "AWS/java-version-upgrade" in found[0] and "approved: none" in found[0]
    # Outside §10 (the CI/CD section) a TD-looking token is not a plan.
    assert "codepipeline" not in found[0]


def test_design_planning_only_approved_transformations_passes():
    meta = {"migration_strategy": "refactor",
            "transformation_steps": ["AWS/java-version-upgrade", "AWS/spring-boot-version-upgrade"]}
    assert _td_contradictions(meta) == []


def test_unapproved_transformation_marked_deferred_is_fine():
    doc = _TD_DESIGN.replace("| 2 | `AWS/spring-boot-version-upgrade` | springboot | 3–5 days |",
                             "Deferred (not approved): `AWS/spring-boot-version-upgrade`.")
    out = tool_documents._check_design_document(doc, {"transformation_steps": ["AWS/java-version-upgrade"]})
    assert not [c for c in out["contradictions"] if c.startswith("transformation_steps")]


def test_no_recorded_selection_skips_the_reverse_check():
    assert _td_contradictions({"migration_strategy": "refactor"}) == []


# ── Network exposure + TLS in the design (APP-TE / APP-HE 2026-10-23) ─────────
def test_design_with_internet_open_ingress_is_a_contradiction():
    doc = "| `APP-TE-alb-sg` | 0.0.0.0/0:443, 0.0.0.0/0:80 | EC2 app SG:8080 |\n"
    c = tool_documents._check_design_document(doc, {"migration_strategy": "relocate"})["contradictions"]
    assert any(x.startswith("network exposure") for x in c)


@pytest.mark.parametrize("line", [
    "| `app-sg` | egress | 0.0.0.0/0 on 443 |",
    "Private route table: 0.0.0.0/0 -> NAT gateway",
    "ALB ingress: TCP 80 from 203.0.113.10/32 and the VPC CIDR — never 0.0.0.0/0.",
])
def test_egress_routes_and_negated_mentions_are_fine(line):
    c = tool_documents._check_design_document(line + "\n", {"migration_strategy": "relocate"})["contradictions"]
    assert not any(x.startswith("network exposure") for x in c)


def test_acm_for_alb_dns_is_a_warning_unless_a_domain_is_set():
    doc = "- ACM certificate: auto-provisioned for ALB DNS name\n"
    w = tool_documents._check_design_document(doc, {})["warnings"]
    assert any(x.startswith("TLS") for x in w)
    w = tool_documents._check_design_document(doc, {"domain_name": "he.example.com"})["warnings"]
    assert not any(x.startswith("TLS") for x in w)


@pytest.mark.parametrize("line", [
    "No rule opens 0.0.0.0/0 to application compute. API Gateway invokes Lambda directly via the AWS service network",
    "There is nothing listening on 0.0.0.0/0 ingress; the API is IAM-authorised",
])
def test_statements_that_nothing_is_open_are_not_exposure(line):
    """APP-CLI 2026-10-27: these failed Design twice (false positive)."""
    c = tool_documents._check_design_document(line + "\n", {"migration_strategy": "replatform"})["contradictions"]
    assert not any(x.startswith("network exposure") for x in c)


def test_a_table_cell_saying_dms_is_not_required_is_not_a_dms_plan():
    doc = "## 3.4 Data Store Migration\n| DMS required | No — table already in AWS |\n| Target database | DynamoDB |\n"
    meta = {"migration_strategy": "replatform", "target_compute": "lambda", "target_database": "dynamodb"}
    c = tool_documents._check_design_document(doc, meta)["contradictions"]
    assert not any("DMS" in x for x in c), c
