"""No service open/exposed + deterministic validation, for every application.

UAT APP-CLI (2026-10-12): the generated ECS stack had an internet-facing ALB with
80/443 open to 0.0.0.0/0, the Validation phase was an LLM prompt that reported
tests it never ran, and an invalid `terraform validate` reached the Execution
gate. These pin the deterministic replacements.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.src.core.exposure_policy import evaluate
from backend.tests.unit.test_journey_exec_infra import FakeCodeCommit, staged, te  # noqa: F401 — fixture

_ROOT = Path(__file__).resolve().parents[3]
ASL = _ROOT / "infra" / "step-functions" / "migration-workflow.v2.asl.json"


def _rc(t, after, address=None):
    return {"address": address or f"{t}.x", "type": t, "change": {"actions": ["create"], "after": after}}


# ── exposure policy ──────────────────────────────────────────────────────────

def test_public_alb_and_open_ingress_are_findings():
    plan = {"resource_changes": [
        _rc("aws_lb", {"internal": False}),
        _rc("aws_security_group", {"ingress": [{"from_port": 80, "to_port": 80, "protocol": "tcp",
                                                "cidr_blocks": ["0.0.0.0/0"]}]}),
        _rc("aws_ecs_service", {"network_configuration": [{"assign_public_ip": True}]}),
        _rc("aws_db_instance", {"publicly_accessible": True}),
        _rc("aws_s3_bucket_public_access_block", {"block_public_acls": False, "block_public_policy": True,
                                                  "ignore_public_acls": True, "restrict_public_buckets": True}),
        _rc("aws_apigatewayv2_route", {"route_key": "ANY /{proxy+}", "authorization_type": "NONE"}),
    ]}
    rules = sorted(f["rule"] for f in evaluate(plan))
    assert rules == ["open-ingress", "public-bucket", "public-database", "public-ip", "public-load-balancer",
                     "unauthenticated-endpoint"]


def test_internal_stack_with_open_egress_is_clean():
    plan = {"resource_changes": [
        _rc("aws_lb", {"internal": True}),
        _rc("aws_security_group", {"ingress": [{"from_port": 80, "to_port": 80, "protocol": "tcp",
                                                "cidr_blocks": ["10.20.0.0/16"]}],
                                   "egress": [{"cidr_blocks": ["0.0.0.0/0"]}]}),
        _rc("aws_ecs_service", {"network_configuration": [{"assign_public_ip": False}]}),
        {"address": "aws_lb.old", "type": "aws_lb", "change": {"actions": ["delete"], "after": None}},
    ]}
    assert evaluate(plan) == []


def test_runner_status_fails_on_unwaived_exposure():
    from backend.src.worker.terraform_runner import _status
    f = [{"address": "a", "detail": "d"}]
    assert _status(True, 0) == "validated"
    assert _status(True, 0, f) == "exposed"
    assert _status(True, 0, f, waived=True) == "validated"
    assert _status(False, 0, f) == "invalid" and _status(True, 1) == "plan_failed"


# ── generated IaC: not exposed by default, ARM64 for Graviton, deploy branch ──

def test_ecs_root_is_internal_vpc_only_and_arm64_for_graviton(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", transformation_steps=["AWS/early-access-java-x86-to-graviton"],
             deploy_branch="migration/app-he")
    mod.provision_infrastructure({"app_id": "APP-HE"})
    tfvars = staged(env, "APP-HE", "infrastructure", "terraform.tfvars")
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    assert re.search(r"^internal_alb\s*= true$", tfvars, re.M)
    assert re.search(r'^allowed_ingress_cidrs\s*= \["172\.16\.0\.0/16"\]$', tfvars, re.M)
    assert re.search(r'^cpu_architecture\s*= "ARM64"$', tfvars, re.M)
    assert re.search(r'^build_arch\s*= "ARM64"$', tfvars, re.M)
    assert re.search(r'^branch\s*= "migration/app-he"$', tfvars, re.M)
    assert "allowed_ingress_cidrs = var.allowed_ingress_cidrs" in main


def test_open_cidrs_cannot_be_injected(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", allowed_ingress_cidrs=["0.0.0.0/0", "10.9.0.0/16"])
    mod.provision_infrastructure({"app_id": "APP-HE"})
    tfvars = staged(env, "APP-HE", "infrastructure", "terraform.tfvars")
    assert "0.0.0.0/0" not in tfvars and "10.9.0.0/16" in tfvars


def test_ecs_module_never_opens_the_alb_to_the_internet():
    tf = (_ROOT / "backend/resources/templates/terraform/modules/ecs-fargate/main.tf").read_text()
    assert "internal                   = var.internal_alb" in tf
    alb_sg = tf.split('resource "aws_security_group" "alb"', 1)[1].split("\nresource ", 1)[0]
    assert not re.search(r'cidr_blocks\s*=\s*\[[^\]]*"0\.0\.0\.0/0"', alb_sg)
    assert "cpu_architecture        = var.cpu_architecture" in tf


# ── CI/CD: build tool from the repo, Dockerfile when missing, coverage opt-out ─

class _CC(FakeCodeCommit):
    def get_file(self, repositoryName, filePath, commitSpecifier):  # noqa: N803
        return {"fileContent": self.files[filePath].encode()}


def test_gradle_module_gets_gradle_buildspec_dockerfile_and_recorded_opt_out(te, monkeypatch):  # noqa: F811
    mod, env = te
    cc = _CC({"main": "a" * 40})
    cc.folders = {"/MonoToMicroLegacy": {"files": [{"relativePath": "build.gradle"}, {"relativePath": "gradlew"}],
                                         "subFolders": []}}
    cc.files = {"MonoToMicroLegacy/build.gradle": "plugins { id 'java' }\nsourceCompatibility = 21\n"}
    monkeypatch.setattr(mod, "codecommit", cc)
    env.seed("APP-HE", build_dir="MonoToMicroLegacy", coverage_threshold=0,
             coverage_threshold_reason="Workshop app has no unit tests (approved 2026-10-12)",
             health_check_path="/health/ishealthy")
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert out["status"] == "staged" and "Dockerfile" in out["files"]
    bs = staged(env, "APP-HE", "cicd", "buildspec.yml")
    assert "gradlew" in bs and "mvn " not in bs
    assert "java: corretto21" in bs
    assert 'COVERAGE_THRESHOLD: "0"' in bs and "Workshop app has no unit tests" in bs
    assert "semgrep" not in bs and "trivy" not in bs
    df = staged(env, "APP-HE", "cicd", "Dockerfile")
    assert "amazoncorretto:21-al2023" in df and "USER 10001:10001" in df and "/health/ishealthy" in df
    paths = [p["filePath"] for c in cc.commits for p in c.get("putFiles", [])]
    assert "MonoToMicroLegacy/Dockerfile" in paths and "cicd/buildspec.yml" in paths


def test_existing_dockerfile_is_not_overwritten(te, monkeypatch):  # noqa: F811
    mod, env = te
    cc = _CC({"main": "a" * 40})
    cc.folders = {"/": {"files": [{"relativePath": "pom.xml"}, {"relativePath": "Dockerfile"}], "subFolders": []}}
    cc.files = {"pom.xml": "<project><properties><java.version>17</java.version></properties></project>"}
    monkeypatch.setattr(mod, "codecommit", cc)
    env.seed("APP-HE")
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert "Dockerfile" not in out["files"]
    assert "mvn -B" in staged(env, "APP-HE", "cicd", "buildspec.yml")


# ── Validation phase: deterministic checks ───────────────────────────────────

def test_validate_deployment_fails_on_exposure_and_missing_gates(te, monkeypatch):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", exec_cicd_status="committed", coverage_threshold=0)
    env.table.put_item({"PK": "APP#APP-HE", "SK": "EXECUTION#infra-validate#2026-10-12T00:00:00Z",
                        "status": "exposed", "exposure_findings": [{"address": "module.compute.aws_lb.main",
                                                                   "detail": "load balancer is internet-facing"}]})
    env.s3.put_object(Bucket="b", Key="applications/APP-HE/execution/cicd/buildspec.yml", Body="build")

    class NoEcs:
        def describe_services(self, **_):
            raise RuntimeError("ClusterNotFoundException")

    monkeypatch.setattr(mod, "ecs", NoEcs())
    out = mod.validate_deployment({"app_id": "APP-HE"})
    assert out["verdict"] == "fail"
    assert {"iac_exposure_policy", "cicd_coverage_gate"} <= set(out["failed"])
    assert not any(c["check"] == "cicd_security_gates" for c in out["checks"])
    assert "live_deployment" in out["warnings"]  # not deployed yet is a warning, not a failure


def test_validate_deployment_passes_clean_undeployed_app_with_warning(te, monkeypatch):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", exec_cicd_status="committed")
    env.table.put_item({"PK": "APP#APP-HE", "SK": "EXECUTION#iac#2026-10-12T00:00:00Z",
                        "target_compute": "ecs_fargate", "modules": ["vpc", "ecs-fargate", "rds"]})
    env.table.put_item({"PK": "APP#APP-HE", "SK": "EXECUTION#infra-validate#2026-10-12T00:00:00Z",
                        "status": "validated", "exposure_findings": []})
    env.s3.put_object(Bucket="b", Key="applications/APP-HE/execution/cicd/buildspec.yml",
                      Body="build\n")

    class NoEcs:
        def describe_services(self, **_):
            return {"services": []}

    monkeypatch.setattr(mod, "ecs", NoEcs())
    out = mod.validate_deployment({"app_id": "APP-HE"})
    assert out["verdict"] == "pass_with_warnings" and out["failed"] == []


# ── ASL wiring ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def states():
    return json.loads(re.sub(r"\$\{(\w+)\}", r"placeholder-\1", ASL.read_text().replace("${ApprovalTimeoutSeconds}", "604800")))["States"]


def test_infra_validate_gates_execution(states):
    assert states["InfrastructureValidate"]["Next"] == "Check_InfrastructureValidate"
    chk = states["Check_InfrastructureValidate"]
    assert chk["Choices"][0]["Condition"] == "{% $states.input.infra_status = 'validated' %}"
    assert chk["Choices"][0]["Next"] == "FixCIGaps" and chk["Default"] == "Failed_InfrastructureValidate"
    assert "exposure" in states["Failed_InfrastructureValidate"]["Cause"]


def test_validation_is_deterministic(states):
    v = states["Validation"]
    assert v["Resource"] == "arn:aws:states:::lambda:invoke"
    assert v["Arguments"]["Payload"]["tool_name"] == "validate_deployment"
    chk = states["Check_Validation"]
    assert chk["Choices"][0]["Next"] == "Approval_Validation" and chk["Default"] == "Failed_Validation"


def test_bucket_names_are_lowercased():
    """APP-CLI apply: 'APP-CLI-pipeline-artifacts-…' → InvalidBucketName (S3 names must be lowercase)."""
    for tf in (_ROOT / "backend/resources/templates/terraform/modules").glob("*/main.tf"):
        for m in re.finditer(r'^\s*bucket\s*=\s*(.+)$', tf.read_text(), re.M):
            expr = m.group(1)
            if expr.startswith('"') and "${" in expr:
                pytest.fail(f"{tf.parent.name}: bucket name not lowercased: {expr}")


def test_lambda_template_routes_are_never_anonymous():
    """APP-CLI 2026-10-19: the generated HTTP API's $default route had no authorizer,
    so every Lambda target failed InfrastructureValidate (status=exposed)."""
    tf = (Path(__file__).resolve().parents[3]
          / "backend/resources/templates/terraform/modules/lambda/main.tf").read_text()
    routes = re.findall(r'resource "aws_apigatewayv2_route"[^{]*\{(.*?)\n\}', tf, re.S)
    assert routes and all("authorization_type" in r for r in routes)
    assert '"AWS_IAM"' in tf and '"NONE"' not in tf


def test_deploy_gate_asl():
    """FixCIGaps is advisory before CI/CD; approval = Deploy; a failed deploy
    returns to the gate (≤3 attempts); reject regenerates Execution."""
    import json
    import re
    from pathlib import Path
    asl = Path(__file__).resolve().parents[3] / "infra" / "step-functions" / "migration-workflow.v2.asl.json"
    S = json.loads(re.sub(r"\$\{(\w+)\}", "X", asl.read_text().replace("${ApprovalTimeoutSeconds}", "604800")))["States"]
    assert S["FixCIGaps"]["Next"] == "CICDSetup" and S["FixCIGaps"]["Catch"][0]["Next"] == "CICDSetup"
    assert S["FixCIGaps"]["Arguments"]["Payload"]["tool_name"] == "run_ci_fix"
    gate = S["Approval_Execution"]
    assert gate["Next"] == "Deploy"
    assert next(c for c in gate["Catch"] if c["ErrorEquals"] == ["StepRejected"])["Next"] == "InfrastructureProvisioning"
    assert S["Deploy"]["Arguments"]["Payload"]["tool_name"] == "deploy_infrastructure"
    assert S["Deploy"]["Resource"].endswith("waitForTaskToken")
    nxt = {c["Next"] for c in S["Check_Deploy"]["Choices"]}
    assert nxt == {"Validation", "Approval_Execution"} and S["Check_Deploy"]["Default"] == "Failed_Deploy"
