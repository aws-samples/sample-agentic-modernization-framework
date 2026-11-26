"""The user-selected target_compute is what Execution builds — for every strategy.

Pins: each selectable (strategy, compute) pair gets its own Terraform compute module,
a CodePipeline wired to that compute and the
matching CD template (never another compute's); unsupported selections (eks,
replatform on ec2, a non-CodePipeline CI/CD, an unset compute on replatform) fail fast instead of "staged" output;
Validation fails when the generated IaC lacks the approved compute.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
import yaml

from backend.tests.unit.test_journey_exec_infra import staged, te  # noqa: F401 — fixture

# Every selectable pair Execution runs for (retain/retire/repurchase close out).
# Scope: EC2 for lift only; ECS Fargate / Lambda for replatform/refactor.
PAIRS = [("rehost", "ec2"), ("relocate", "ec2"),
         ("replatform", "ecs_fargate"), ("replatform", "lambda"),
         ("refactor", "ecs_fargate"), ("refactor", "lambda")]
MODULE = {"ecs_fargate": "ecs-fargate", "ec2": "ec2", "lambda": "lambda"}
DEPLOY = {"ecs_fargate": "ecs", "ec2": "ec2", "lambda": "lambda"}


def _seed(env, strategy, compute, cicd="codepipeline", **extra):
    lift = strategy in ("rehost", "relocate")
    meta = dict(migration_strategy=strategy, target_compute=compute, cicd_target=cicd,
                target_database="same_as_source" if lift else "rds_postgresql",
                db_instance_class="db.r6g.large", ami_id="ami-0123456789abcdef0",
                transformation_steps=[] if lift else ["AWS/java-version-upgrade"])
    return env.seed("APP-HE", **{**meta, **extra})


@pytest.mark.parametrize("strategy,compute", PAIRS)
def test_iac_builds_exactly_the_approved_compute(te, strategy, compute):  # noqa: F811
    mod, env = te
    _seed(env, strategy, compute)
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    assert out["status"] == "staged" and out["target_compute"] == compute
    assert MODULE[compute] in out["modules"]
    assert not ({"ecs-fargate", "ec2", "lambda"} - {MODULE[compute]}) & set(out["modules"])
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    assert f'source             = "./modules/{MODULE[compute]}"' in main


@pytest.mark.parametrize("strategy,compute", PAIRS)
def test_codepipeline_is_wired_to_the_approved_compute(te, strategy, compute):  # noqa: F811
    mod, env = te
    _seed(env, strategy, compute)
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    assert "codepipeline" in out["modules"]
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    assert f'deploy_target    = "{DEPLOY[compute]}"' in main
    wiring = {"ecs_fargate": "ecs_service_name = module.compute.service_name",
              "ec2": "asg_name            = module.compute.asg_name",
              "lambda": "function_name       = module.compute.function_name"}[compute]
    assert wiring in main


@pytest.mark.parametrize("strategy,compute,cicd,cd_file", [
    ("replatform", "ecs_fargate", "codepipeline", "CODEPIPELINE_DEPLOY_NOTE.md"),
    ("rehost", "ec2", "codepipeline", "CODEPIPELINE_DEPLOY_NOTE.md"),
    ("replatform", "lambda", "codepipeline", "CODEPIPELINE_DEPLOY_NOTE.md"),
])
def test_cd_targets_the_approved_compute_only(te, strategy, compute, cicd, cd_file):  # noqa: F811
    mod, env = te
    _seed(env, strategy, compute, cicd=cicd)
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert out["status"] == "staged" and cd_file in out["files"]
    cd = staged(env, "APP-HE", "cicd", cd_file)
    other = {"ecs_fargate": ("update-function-code", "CodeDeploy in-place"),
             "ec2": ("update-function-code", "ECS deploy action"),
             "lambda": ("ecs update-service", "describe-task-definition", "ECS deploy action")}[compute]
    for marker in other:
        assert marker not in cd, (compute, cicd, marker)
    if compute == "lambda":
        assert "update-function-code" in cd and "APP-HE-function" in cd


def test_lambda_codepipeline_buildspec_deploys_and_smoke_tests(te):  # noqa: F811
    mod, env = te
    _seed(env, "replatform", "lambda")
    mod.generate_cicd({"app_id": "APP-HE"})
    spec = yaml.safe_load(staged(env, "APP-HE", "cicd", "buildspec.yml"))
    post = "\n".join(spec["phases"]["post_build"]["commands"])
    assert "update-function-code" in post and "--publish" in post and "aws lambda invoke" in post
    assert "docker" not in post
    assert spec["artifacts"]["files"] == ["deploy.json"]


def test_ec2_codepipeline_ships_a_codedeploy_revision(te):  # noqa: F811
    mod, env = te
    _seed(env, "rehost", "ec2", service_name="health-engine", install_dir="/srv/he")
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert {"appspec.yml", "application_start.sh", "application_stop.sh", "validate_service.sh"} <= set(out["files"])
    spec = yaml.safe_load(staged(env, "APP-HE", "cicd", "buildspec.yml"))
    assert spec["artifacts"]["base-directory"] == "revision"
    appspec = yaml.safe_load(staged(env, "APP-HE", "cicd", "appspec.yml"))
    assert appspec["files"][0]["destination"] == "/srv/he"
    assert 'SERVICE="health-engine"' in staged(env, "APP-HE", "cicd", "application_start.sh")
    paths = {f["filePath"] for c in env.cc.commits for f in c["putFiles"]}
    assert {"cicd/codedeploy/appspec.yml", "cicd/codedeploy/scripts/application_start.sh",
            "cicd/buildspec.yml"} <= paths


def test_unsafe_service_name_falls_back_to_default(te):  # noqa: F811
    mod, env = te
    _seed(env, "rehost", "ec2", service_name="x; rm -rf /")
    mod.generate_cicd({"app_id": "APP-HE"})
    assert 'SERVICE="app-he"' in staged(env, "APP-HE", "cicd", "application_start.sh")


@pytest.mark.parametrize("framework,expected", [
    ("Spring Boot 3.2", "com.amazonaws.serverless.proxy.spring.SpringDelegatingLambdaContainerHandler"),
    ("Quarkus", "example.Handler::handleRequest"),
])
def test_lambda_handler_is_wired_from_meta(te, framework, expected):  # noqa: F811
    mod, env = te
    _seed(env, "replatform", "lambda", framework=framework)
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    tfvars = staged(env, "APP-HE", "infrastructure", "terraform.tfvars")
    assert re.search(r'^lambda_handler\s+= "' + re.escape(expected) + '"$', tfvars, re.M)
    assert "handler            = var.lambda_handler" in staged(env, "APP-HE", "infrastructure", "main.tf")
    assert any("lambda_handler" in n for n in out["notes"])
    _seed(env, "replatform", "lambda", lambda_handler="com.acme.Fn::handle")
    mod.provision_infrastructure({"app_id": "APP-HE"})
    assert "com.acme.Fn::handle" in staged(env, "APP-HE", "infrastructure", "terraform.tfvars")


@pytest.mark.parametrize("meta,fragment", [
    ({"target_compute": "eks"}, "not valid for strategy='replatform'"),
    ({"migration_strategy": "", "target_compute": "eks"}, "no standardized Terraform module"),
    ({"migration_strategy": "rehost", "target_compute": "lambda"}, "not valid for strategy='rehost'"),
    ({"target_compute": ""}, "target_compute is not set"),
])
def test_undeliverable_compute_fails_fast(te, meta, fragment):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", **meta)
    for fn, status_field in ((mod.provision_infrastructure, "exec_infra_status"),
                             (mod.generate_cicd, "exec_cicd_status")):
        out = fn({"app_id": "APP-HE"})
        assert out["status"] == "error" and fragment in out["message"], (fn.__name__, out)
        assert env.table.items[("APP#APP-HE", "META")][status_field] == "error"


def test_lift_strategy_with_unset_compute_resolves_to_ec2(te):  # noqa: F811
    mod, env = te
    env.seed("APP-TE", target_compute="", ami_id="ami-0123456789abcdef0")
    out = mod.provision_infrastructure({"app_id": "APP-TE"})
    assert out["status"] == "staged" and out["target_compute"] == "ec2" and "ec2" in out["modules"]


@pytest.mark.parametrize("cicd", ["jenkins", "github_actions", "gitlab"])
def test_unsupported_cicd_is_rejected(te, cicd):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", cicd_target=cicd)
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert out["status"] == "error" and cicd in out["message"]
    assert not [k for k in env.s3.objects if "/execution/cicd/" in k]


def test_ci_for_another_language_is_refused(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", language="Go", build_tool="")
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert out["status"] == "error" and "language='go'" in out["message"]


def test_validation_fails_when_iac_lacks_the_approved_compute(te, monkeypatch):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", exec_cicd_status="committed", target_compute="lambda")
    env.table.put_item({"PK": "APP#APP-HE", "SK": "EXECUTION#iac#2026-10-12T00:00:00Z",
                        "target_compute": "ecs_fargate", "modules": ["vpc", "ecs-fargate"]})
    env.table.put_item({"PK": "APP#APP-HE", "SK": "EXECUTION#infra-validate#2026-10-12T00:00:00Z",
                        "status": "validated", "exposure_findings": []})

    class NoLambda:
        def get_function(self, **_):
            raise RuntimeError("ResourceNotFoundException")

    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: NoLambda())
    out = mod.validate_deployment({"app_id": "APP-HE"})
    assert out["verdict"] == "fail" and "compute_module_present" in out["failed"]
    assert "live_deployment" in out["warnings"]  # Lambda checks ran (not ECS) and found nothing deployed


def test_validation_reads_the_providers_own_ci_file(te, monkeypatch):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", exec_cicd_status="committed")
    env.s3.put_object(Bucket="b", Key="applications/APP-HE/execution/cicd/buildspec.yml", Body="build")

    class NoEcs:
        def describe_services(self, **_):
            return {"services": []}

    monkeypatch.setattr(mod, "ecs", NoEcs())
    out = mod.validate_deployment({"app_id": "APP-HE"})
    # The staged buildspec was read: its coverage gate is judged (no file -> no CI checks at all).
    assert any(c["check"] == "cicd_coverage_gate" for c in out["checks"])


@pytest.mark.skipif(not (os.environ.get("AMF_TF_VALIDATE") and shutil.which("terraform")),
                    reason="set AMF_TF_VALIDATE=1 (needs terraform + provider download)")
@pytest.mark.parametrize("strategy,compute,db", [("replatform", "lambda", "rds_postgresql"),
                                                 ("rehost", "ec2", "same_as_source"), ("replatform", "lambda", "dynamodb"),
                                                 ("replatform", "ecs_fargate", "dynamodb"),
                                                 ("relocate", "ec2", "dynamodb"), ("replatform", "ecs_fargate", "rds_postgresql")])
def test_rendered_root_with_codepipeline_validates(te, strategy, compute, db, tmp_path):  # noqa: F811
    mod, env = te
    extra = {"ami_id": "al2023"} if db == "dynamodb" and compute == "ec2" else {}
    _seed(env, strategy, compute, target_database=db, dynamodb_hash_key="id", db_instance_class="db.t4g.micro", **extra)
    mod.provision_infrastructure({"app_id": "APP-HE"})
    prefix = "applications/APP-HE/execution/infrastructure/"
    for key, body in env.s3.objects.items():
        if key.startswith(prefix):
            dest = tmp_path / key[len(prefix):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
    (tmp_path / "_amf_local_backend_override.tf").write_text('terraform {\n  backend "local" {}\n}\n')
    init = subprocess.run(["terraform", f"-chdir={tmp_path}", "init", "-backend=false", "-input=false", "-no-color"],
                          capture_output=True, text=True)
    assert init.returncode == 0, init.stdout + init.stderr
    val = subprocess.run(["terraform", f"-chdir={tmp_path}", "validate", "-no-color"], capture_output=True, text=True)
    assert val.returncode == 0, val.stdout + val.stderr


@pytest.mark.parametrize("strategy", ["rehost", "relocate", "replatform", "refactor"])
def test_execution_compute_rules_mirror_strategy_options(te, strategy):  # noqa: F811
    from backend.src.core.strategy_options import compute_options_for
    mod, _ = te
    assert set(mod._COMPUTE_FOR_STRATEGY[strategy]) == compute_options_for(strategy)
    assert set(mod._COMPUTE_FOR_STRATEGY[strategy]) <= set(mod._COMPUTE_MODULES)


def test_strategy_change_clears_selections_the_new_strategy_forbids():
    """A Discovery strategy override must not leave e.g. ecs_fargate on a rehost app."""
    from unittest.mock import patch

    import boto3
    from moto import mock_aws

    from backend.src.api.v1 import portfolio

    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="amf_portfolio", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={"PK": "APP#APP-X", "SK": "META", "app_id": "APP-X", "migration_strategy": "replatform",
                         "target_compute": "ecs_fargate", "target_database": "aurora_postgresql",
                         "cicd_target": "codepipeline"})
        with patch.object(portfolio, "get_table", return_value=t):
            portfolio.update_app("APP-X", {"migration_strategy": "rehost"})
        meta = t.get_item(Key={"PK": "APP#APP-X", "SK": "META"})["Item"]
        assert meta["migration_strategy"] == "rehost"
        assert "target_compute" not in meta and "target_database" not in meta
        assert meta["cicd_target"] == "codepipeline"  # still valid for rehost


@pytest.mark.parametrize("compute,module", [("ecs_fargate", "ecs-fargate"), ("lambda", "lambda")])
def test_dynamodb_table_reaches_the_app(te, compute, module):  # noqa: F811
    mod, env = te
    _seed(env, "replatform", compute, target_database="dynamodb",
          dynamodb_table_name="musicTable", dynamodb_hash_key="artist")
    mod.provision_infrastructure({"app_id": "APP-HE"})
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    tfvars = staged(env, "APP-HE", "infrastructure", "terraform.tfvars")
    assert "TABLE_NAME = module.database.table_name" in main
    assert "dynamodb_table_arns = [module.database.table_arn]" in main
    assert re.search(r'^dynamodb_table_name\s+= "musicTable"$', tfvars, re.M)
    assert re.search(r'^dynamodb_hash_key\s+= "artist"$', tfvars, re.M)
    assert re.search(r'^dynamodb_range_key\s+= ""$', tfvars, re.M)


# ── multi-function Lambda apps (SAM/CDK → Terraform) ─────────

SPRING_FUNCTIONS = [  # serverless-java-frameworks-samples/springboot template.yaml, as the repo scan reports it
    {"name": "GetProductByIdFunction", "handler": "org.springframework.cloud.function.adapter.aws.FunctionInvoker::handleRequest",
     "routes": ["GET /products/{id}"], "env": {"SPRING_CLOUD_FUNCTION_DEFINITION": "getProductByIdFunction"}, "memory": 1024, "timeout": 30},
    {"name": "GetProductsFunction", "handler": "org.springframework.cloud.function.adapter.aws.FunctionInvoker::handleRequest",
     "routes": ["GET /products"], "env": {"SPRING_CLOUD_FUNCTION_DEFINITION": "getAllProductsFunction"}, "memory": 1024, "timeout": 30},
    {"name": "PutProductFunction", "handler": "org.springframework.cloud.function.adapter.aws.FunctionInvoker::handleRequest",
     "routes": ["PUT /products/{id}"], "env": {"SPRING_CLOUD_FUNCTION_DEFINITION": "createProductFunction"}, "memory": 1024, "timeout": 30},
    {"name": "DeleteProductFunction", "handler": "org.springframework.cloud.function.adapter.aws.FunctionInvoker::handleRequest",
     "routes": ["DELETE /products/{id}"], "env": {"SPRING_CLOUD_FUNCTION_DEFINITION": "deleteProductFunction"}, "memory": 1024, "timeout": 30},
]


def test_multi_function_app_gets_one_function_and_route_each(te):  # noqa: F811
    mod, env = te
    _seed(env, "replatform", "lambda", target_database="dynamodb", dynamodb_hash_key="PK",
          detected_lambda_functions=SPRING_FUNCTIONS)
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    for key in ("getproductbyidfunction", "getproductsfunction", "putproductfunction", "deleteproductfunction"):
        assert key in main
    assert '"PUT /products/{id}"' in main and "SPRING_CLOUD_FUNCTION_DEFINITION" in main
    assert 'payload_format = "1.0"' in main  # SAM `Api` events (REST v1 handlers)
    assert "function_names      = module.compute.function_names" in main
    assert any("4 Lambda functions" in n for n in out["notes"])


def test_single_function_app_keeps_the_default_function(te):  # noqa: F811
    mod, env = te
    _seed(env, "replatform", "lambda", detected_lambda_functions=SPRING_FUNCTIONS[:1])
    mod.provision_infrastructure({"app_id": "APP-HE"})
    assert "functions = " not in staged(env, "APP-HE", "infrastructure", "main.tf")


def test_multi_function_smoke_targets_a_parameter_free_get(te):  # noqa: F811
    mod, env = te
    _seed(env, "replatform", "lambda", detected_lambda_functions=SPRING_FUNCTIONS)
    mod.generate_cicd({"app_id": "APP-HE"})
    spec = staged(env, "APP-HE", "cicd", "buildspec.yml")
    # REST (SAM `Api`) routes → a payload-1.0 smoke event (APIGatewayProxyRequestEvent handlers).
    assert 'APP-HE-getproductsfunction:${VERSION}' in spec and '"path":"/products","httpMethod":"GET"' in spec
    assert "for FN in ${FUNCTION_NAMES:-$FUNCTION_NAME}" in spec


@pytest.mark.skipif(not (os.environ.get("AMF_TF_VALIDATE") and shutil.which("terraform")),
                    reason="set AMF_TF_VALIDATE=1 (needs terraform + provider download)")
def test_multi_function_root_validates(te, tmp_path):  # noqa: F811
    mod, env = te
    _seed(env, "replatform", "lambda", target_database="dynamodb", dynamodb_hash_key="PK",
          detected_lambda_functions=SPRING_FUNCTIONS)
    mod.provision_infrastructure({"app_id": "APP-HE"})
    prefix = "applications/APP-HE/execution/infrastructure/"
    for key, body in env.s3.objects.items():
        if key.startswith(prefix):
            dest = tmp_path / key[len(prefix):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
    (tmp_path / "_amf_local_backend_override.tf").write_text('terraform {\n  backend "local" {}\n}\n')
    assert subprocess.run(["terraform", f"-chdir={tmp_path}", "init", "-backend=false", "-input=false", "-no-color"],
                          capture_output=True, text=True).returncode == 0
    val = subprocess.run(["terraform", f"-chdir={tmp_path}", "validate", "-no-color"], capture_output=True, text=True)
    assert val.returncode == 0, val.stdout + val.stderr


def test_teardown_requires_typed_confirmation_and_invokes_destroy(monkeypatch):
    """tear down is explicit (Admin, typed app id) and goes through destroy_infrastructure."""
    import io
    import json
    from unittest.mock import MagicMock, patch

    from backend.src.api.v1.workflow import TeardownRequest, teardown_app
    from backend.src.config import settings

    monkeypatch.setattr(settings, "deploy_enabled", True)

    lam = MagicMock()
    lam.invoke.return_value = {"Payload": io.BytesIO(json.dumps({"status": "launched", "job_id": "destroy-x"}).encode())}
    with patch("backend.src.api.v1.workflow.boto3.client", return_value=lam):
        refused = teardown_app(TeardownRequest(app_id="APP-X", confirm="app-x"), user=None)
        assert lam.invoke.call_count == 0 and b"TEARDOWN_NOT_CONFIRMED" in refused.body
        ok = teardown_app(TeardownRequest(app_id="APP-X", confirm="APP-X"), user=None)
    payload = json.loads(lam.invoke.call_args.kwargs["Payload"])
    assert payload == {"tool_name": "destroy_infrastructure", "arguments": {"app_id": "APP-X", "confirm": "APP-X"}}
    assert ok.data["status"] == "destroying"


def test_lift_to_ec2_with_dynamodb_wires_the_table(te):  # noqa: F811
    """relocate + EC2 + DynamoDB (2026-10-23): a new table, CRUD on the instance role, and the
    table name reaches the app's systemd unit as env — no code transforms."""
    mod, env = te
    _seed(env, "relocate", "ec2", target_database="dynamodb", service_name="te-app")
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    assert out["status"] == "staged" and "dynamodb" in out["modules"] and "rds" not in out["modules"]
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    compute = main[main.index('module "compute"'):]
    compute = compute[:compute.index("\n}")]
    assert "dynamodb_table_arns = [module.database.table_arn]" in compute
    assert "TABLE_NAME = module.database.table_name" in compute
    assert 'service_name       = "te-app"' in compute


def test_ec2_without_a_source_image_bootstraps_al2023(te):  # noqa: F811
    """ami_id=al2023: no MGN image — the module installs Corretto + the systemd unit the hooks restart."""
    mod, env = te
    _seed(env, "relocate", "ec2", ami_id="al2023", language="Java")
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    assert out["status"] == "staged"
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    assert 'java_version       = "' in main and 'install_dir        = "/opt/app-he"' in main
    assert 'service_name       = "app-he"' in main
