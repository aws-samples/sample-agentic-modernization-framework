"""ECS/Lambda/EC2 + database wiring found reviewing the 2026-10-23 apps.

serverless-java-frameworks-samples/springboot (Lambda + DynamoDB): the SAM scan merged the
template.native*/snapstart variants (16 duplicate functions, `provided` runtimes) and
`PRODUCT_TABLE_NAME: !Ref ProductsTable` reached the function as the literal string; the
CodeBuild smoke invoke sent a v2.0 event to REST (payload 1.0) handlers.
java-on-aws/apps/unicorn-store-spring (ECS + RDS PostgreSQL): no datasource reached the task.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend.src.worker.repo_scan import scan_wiring
from backend.tests.unit.test_journey_exec_infra import staged, te  # noqa: F401 — fixture

SAM = """\
Transform: AWS::Serverless-2016-10-31
Globals:
  Function:
    Handler: org.springframework.cloud.function.adapter.aws.FunctionInvoker::handleRequest
    Runtime: {runtime}
    Environment:
      Variables:
        PRODUCT_TABLE_NAME: !Ref ProductsTable
Resources:
  GetProductsFunction:
    Type: AWS::Serverless::Function
    Properties:
      Environment:
        Variables:
          SPRING_CLOUD_FUNCTION_DEFINITION: getAllProductsFunction
      Events:
        Get:
          Type: Api
          Properties:
            Path: /products
            Method: get
  PutProductFunction:
    Type: AWS::Serverless::Function
    Properties:
      Events:
        Put:
          Type: Api
          Properties:
            Path: /products/{{id}}
            Method: put
  ProductsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      KeySchema:
        - AttributeName: PK
          KeyType: HASH
"""


def _sam_repo(tmp_path: Path) -> Path:
    app = tmp_path / "springboot"
    app.mkdir()
    (app / "pom.xml").write_text("<project/>")
    (app / "template.yaml").write_text(SAM.format(runtime="java17"))
    for variant in ("template.native.yaml", "template.snapstart.yaml"):
        (app / variant).write_text(SAM.format(runtime="provided.al2"))
    return tmp_path


def test_sam_scan_reads_only_the_canonical_template(tmp_path):
    r = scan_wiring(_sam_repo(tmp_path), "springboot")
    assert [f["name"] for f in r["lambda_functions"]] == ["GetProductsFunction", "PutProductFunction"]
    assert {f["runtime"] for f in r["lambda_functions"]} == {"java17"}
    assert r["sources"]["lambda_functions"] == "springboot/template.yaml"


def test_sam_table_ref_names_the_env_var(tmp_path):
    r = scan_wiring(_sam_repo(tmp_path), "springboot")
    assert r["dynamodb_tables"] == [{"hash_key": "PK", "env_var": "PRODUCT_TABLE_NAME"}]


def _seed_lambda(env, tmp_path):
    det = scan_wiring(_sam_repo(tmp_path), "springboot")
    meta = dict(migration_strategy="refactor", target_compute="lambda", target_database="dynamodb",
                cicd_target="codepipeline", language="Java", build_tool="Maven", build_dir="springboot",
                transformation_steps=[], **{f"detected_{k}": v for k, v in det.items() if k not in ("sources", "build_dir")})
    env.seed("APP-CLI", **meta)


def test_lambda_functions_get_the_real_table_name(te, tmp_path):  # noqa: F811
    mod, env = te
    _seed_lambda(env, tmp_path)
    assert mod.provision_infrastructure({"app_id": "APP-CLI"})["status"] == "staged"
    main = staged(env, "APP-CLI", "infrastructure", "main.tf")
    assert "PRODUCT_TABLE_NAME = module.database.table_name" in main
    assert "Ref:" not in main and "${Ref" not in main and "$${Ref" not in main


def test_lambda_smoke_event_matches_rest_payload_format(te, tmp_path):  # noqa: F811
    mod, env = te
    _seed_lambda(env, tmp_path)
    assert mod.generate_cicd({"app_id": "APP-CLI"})["status"] == "staged"
    spec = staged(env, "APP-CLI", "cicd", "buildspec.yml")
    payload = json.loads(spec.split("PAYLOAD='", 1)[1].split("'", 1)[0])
    assert payload["httpMethod"] == "GET" and payload["path"] == "/products" and "version" not in payload
    assert "APP-CLI-getproductsfunction" in spec


def test_ecs_gets_the_rds_datasource_from_the_managed_secret(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", migration_strategy="replatform", target_compute="ecs_fargate", target_database="rds_postgresql",
             db_instance_class="db.t4g.micro", cicd_target="codepipeline", transformation_steps=[])
    assert mod.provision_infrastructure({"app_id": "APP-HE"})["status"] == "staged"
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    compute = main[main.index('module "compute"'):]
    compute = compute[:compute.index("\n}")]
    assert ('SPRING_DATASOURCE_URL = "jdbc:postgresql://${module.database.endpoint}:'
            '${module.database.port}/${module.database.db_name}"') in compute
    assert 'SPRING_DATASOURCE_PASSWORD = "${module.database.master_user_secret_arn}:password::"' in compute
    assert "secret_kms_key_arns = [module.database.kms_key_arn]" in compute
    assert "db_name                    = var.db_name" in main
    assert re.search(r'^db_name\s+= "appdb"$', staged(env, "APP-HE", "infrastructure", "terraform.tfvars"), re.M)
    # The ECS task SG only allows HTTPS out: the DB port must be opened to the VPC (APP-PETCLINIC-EC2 2026-11-04).
    assert "db_egress_port        = var.db_port" in compute and "module.database.port" not in compute.split("db_egress_port")[1][:40]
    assert re.search(r"^db_port\s+= 5432$", staged(env, "APP-HE", "infrastructure", "terraform.tfvars"), re.M)
    assert "db_egress_cidr_blocks = [var.vpc_cidr]" in compute


def test_ecs_with_dynamodb_gets_no_db_egress(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", migration_strategy="replatform", target_compute="ecs_fargate", target_database="dynamodb",
             cicd_target="codepipeline", transformation_steps=[])
    assert mod.provision_infrastructure({"app_id": "APP-HE"})["status"] == "staged"
    main = staged(env, "APP-HE", "infrastructure", "main.tf")
    assert "db_egress_port" not in main


def test_ecs_template_db_egress_is_vpc_only():
    tf = (Path(__file__).resolve().parents[2] / "resources/templates/terraform/modules/ecs-fargate/main.tf").read_text()
    assert 'variable "db_egress_port"' in tf and 'variable "db_egress_cidr_blocks"' in tf
    assert '!contains(var.db_egress_cidr_blocks, "0.0.0.0/0")' in tf
    assert "cidr_blocks = var.db_egress_cidr_blocks" in tf


def test_generated_buildspec_has_no_third_party_scanners(te):  # noqa: F811
    mod, env = te
    env.seed("APP-TE", migration_strategy="relocate", target_compute="ec2", target_database="dynamodb",
             cicd_target="codepipeline", ami_id="al2023", transformation_steps=[])
    assert mod.generate_cicd({"app_id": "APP-TE"})["status"] == "staged"
    spec = staged(env, "APP-TE", "cicd", "buildspec.yml")
    assert "{{" not in spec and "trivy" not in spec.lower() and "semgrep" not in spec.lower()


# ── Validation L0 live checks (2026-10-23 review) ──────────────────────────────
class _Clients:
    """boto3.client stand-in: one object per service name."""

    def __init__(self, **svc):
        self.svc = svc

    def __call__(self, name, **_):
        return self.svc[name]


class _Lam:
    def __init__(self, fns):
        self.fns = fns  # name -> versions

    def get_function(self, FunctionName):  # noqa: N803
        if FunctionName not in self.fns:
            raise RuntimeError("ResourceNotFound")
        return {"Configuration": {"State": "Active", "LastUpdateStatus": "Successful"}}

    def list_versions_by_function(self, FunctionName):  # noqa: N803
        return {"Versions": [{"Version": "$LATEST"}] + [{"Version": v} for v in self.fns[FunctionName]]}


class _Pipe:
    def list_pipeline_executions(self, **_):
        return {"pipelineExecutionSummaries": [{"status": "Succeeded"}]}


class _Rows(list):
    def by(self, name):
        return next(c for c in self if c["check"] == name)


def test_multi_function_lambda_is_live_checked_per_function(te, tmp_path, monkeypatch):  # noqa: F811
    mod, env = te
    _seed_lambda(env, tmp_path)
    meta = env.table.items[("APP#APP-CLI", "META")]
    lam = _Lam({"APP-CLI-getproductsfunction": ["1"], "APP-CLI-putproductfunction": []})
    monkeypatch.setattr(mod.boto3, "client", _Clients(**{"lambda": lam, "codepipeline": _Pipe(),
                                                         "apigatewayv2": type("A", (), {"get_apis": lambda s: {"Items": []}})()}))
    rows = _Rows()
    mod._lambda_live_checks("APP-CLI", meta, rows)
    assert not any(c["check"] == "live_deployment" for c in rows)  # found, not "not_deployed"
    assert rows.by("lambda_function_active")["result"] == "pass"
    assert rows.by("lambda_code_deployed")["result"] == "fail"
    assert "APP-CLI-putproductfunction" in rows.by("lambda_code_deployed")["detail"]


def test_alb_ingress_must_be_the_vpc_or_approved_cidrs(te, monkeypatch):  # noqa: F811
    mod, _ = te

    class Elb:
        def describe_load_balancers(self, Names):  # noqa: N803
            return {"LoadBalancers": [{"Scheme": "internet-facing", "SecurityGroups": ["sg-1"], "LoadBalancerArn": "lb"}]}

        def describe_target_groups(self, **_):
            return {"TargetGroups": []}

    class Ec2:
        def __init__(self, cidrs):
            self.cidrs = cidrs

        def describe_security_groups(self, GroupIds):  # noqa: N803
            return {"SecurityGroups": [{"GroupId": "sg-1", "IpPermissions": [
                {"FromPort": 80, "IpRanges": [{"CidrIp": c} for c in self.cidrs]}]}]}

    meta = {"vpc_cidr": "10.20.0.0/16", "allowed_ingress_cidrs": ["203.0.113.10/32"], "expose_publicly": True}
    for cidrs, want in ((["10.20.0.0/16", "203.0.113.10/32"], "pass"), (["10.20.0.0/16", "203.0.113.0/24"], "fail")):
        monkeypatch.setattr(mod.boto3, "client", _Clients(elbv2=Elb(), ec2=Ec2(cidrs)))
        rows = _Rows()
        mod._alb_checks("APP-HE", meta, rows)
        assert rows.by("alb_ingress_scope")["result"] == want, cidrs
        assert rows.by("alb_not_public")["result"] == "warn"  # public by explicit opt-in



def test_alb_targets_ignore_draining(te, monkeypatch):  # noqa: F811
    """A rolling deploy leaves the old target draining; that alone must not fail validation."""
    mod, _ = te

    class Elb:
        def __init__(self, states):
            self.states = states

        def describe_load_balancers(self, Names):  # noqa: N803
            return {"LoadBalancers": [{"Scheme": "internal", "SecurityGroups": [], "LoadBalancerArn": "lb"}]}

        def describe_target_groups(self, **_):
            return {"TargetGroups": [{"TargetGroupArn": "tg"}]}

        def describe_target_health(self, TargetGroupArn):  # noqa: N803
            return {"TargetHealthDescriptions": [{"TargetHealth": {"State": s}} for s in self.states]}

    class Ec2:
        def describe_security_groups(self, GroupIds):  # noqa: N803
            return {"SecurityGroups": []}

    for states, want in ((["draining", "healthy"], "pass"), (["healthy"], "pass"), (["draining"], "fail"),
                         (["unhealthy", "healthy"], "fail"), (["draining", "unhealthy"], "fail"), ([], "fail")):
        monkeypatch.setattr(mod.boto3, "client", _Clients(elbv2=Elb(states), ec2=Ec2()))
        rows = _Rows()
        mod._alb_checks("APP-PETCLINIC-EC2", {"vpc_cidr": "10.0.1.0/24"}, rows)
        assert rows.by("alb_targets_healthy")["result"] == want, states


def test_ecs_steady_waits_for_an_in_progress_rollout(te, monkeypatch):  # noqa: F811
    """Validation right after Deploy sees the forced rollout mid-way; it must wait, not fail."""
    mod, _ = te
    rolling = {"status": "ACTIVE", "runningCount": 1, "desiredCount": 1, "taskDefinition": "td",
               "deployments": [{"rolloutState": "IN_PROGRESS"}, {"rolloutState": "COMPLETED"}]}
    done = {**rolling, "deployments": [{"rolloutState": "COMPLETED"}]}
    seq = [rolling, rolling, done]

    class Ecs:
        def describe_services(self, **_):
            return {"services": [seq.pop(0) if len(seq) > 1 else seq[0]]}

    monkeypatch.setattr(mod, "ecs", Ecs())
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    assert mod._ecs_steady(mod._wait_for_ecs_rollout("c", "s", rolling))

    failed = {**rolling, "deployments": [{"rolloutState": "FAILED"}, {"rolloutState": "COMPLETED"}]}
    assert mod._wait_for_ecs_rollout("c", "s", failed) is failed      # a failed rollout isn't waited on
    monkeypatch.setattr(mod, "_ECS_ROLLOUT_WAIT_SECONDS", 0)
    assert not mod._ecs_steady(mod._wait_for_ecs_rollout("c", "s", rolling))  # bounded

def test_database_live_checks(te, monkeypatch):  # noqa: F811
    mod, _ = te

    class Rds:
        def __init__(self, public):
            self.public = public

        def describe_db_instances(self, DBInstanceIdentifier):  # noqa: N803
            assert DBInstanceIdentifier == "app-he-db"
            return {"DBInstances": [{"DBInstanceStatus": "available", "PubliclyAccessible": self.public,
                                     "StorageEncrypted": True}]}

    class Ddb:
        def describe_table(self, TableName):  # noqa: N803
            assert TableName == "APP-TE-table"
            return {"Table": {"TableStatus": "ACTIVE", "SSEDescription": {"Status": "ENABLED"}}}

    for public, want in ((False, "pass"), (True, "fail")):
        monkeypatch.setattr(mod.boto3, "client", _Clients(rds=Rds(public)))
        rows = _Rows()
        mod._db_live_checks("APP-HE", {"target_database": "rds_postgresql"}, rows)
        assert rows.by("database_live")["result"] == want
    monkeypatch.setattr(mod.boto3, "client", _Clients(dynamodb=Ddb()))
    rows = _Rows()
    mod._db_live_checks("APP-TE", {"target_database": "dynamodb"}, rows)
    assert rows.by("database_live")["result"] == "pass"
    rows = _Rows([{"check": "live_deployment", "result": "not_deployed"}])
    mod._db_live_checks("APP-TE", {"target_database": "dynamodb"}, rows)
    assert len(rows) == 1  # nothing deployed → no DB verdict


def test_generate_dockerfile_replaces_a_broken_repo_dockerfile(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", migration_strategy="replatform", target_compute="ecs_fargate", target_database="rds_postgresql",
             db_instance_class="db.t4g.medium", cicd_target="codepipeline", language="Java", build_tool="Maven",
             transformation_steps=[])
    env.cc.get_folder = lambda **_: {"files": [{"relativePath": "pom.xml"}, {"relativePath": "Dockerfile"}],
                                     "subFolders": []}
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert "Dockerfile" not in out["files"]  # the repo's own is used by default
    env.table.items[("APP#APP-HE", "META")]["generate_dockerfile"] = True
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert "Dockerfile" in out["files"] and any("REPLACED" in n for n in out["notes"])
    spec = staged(env, "APP-HE", "cicd", "buildspec.yml")
    assert "sort -rn | head -1" in spec.split("- docker build -t")[0].split("#@")[-1]  # the executable (largest) jar


def test_pipeline_sources_the_branch_the_deploy_gate_applies(te):  # noqa: F811
    """APP-CLI 2026-10-26: the pipeline watched the linked branch atx-run1 while the gate deployed
    migration/app-cli — it waited 60 min for a run of the reviewed commit that never came."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from backend.src.worker.deploy_runner import deploy_branch
    mod, env = te
    env.seed("APP-HE", migration_strategy="replatform", target_compute="ecs_fargate", target_database="none",
             cicd_target="codepipeline", transformation_steps=[])
    mod.provision_infrastructure({"app_id": "APP-HE"})
    tfvars = staged(env, "APP-HE", "infrastructure", "terraform.tfvars")
    want = deploy_branch(env.table.items[("APP#APP-HE", "META")], "APP-HE")
    assert re.search(rf'^branch\s*= "{re.escape(want)}"$', tfvars, re.M) and want == "migration/app-he"


def test_worker_dry_run_outputs_are_not_committed(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", migration_strategy="replatform", target_compute="ecs_fargate", target_database="none",
             cicd_target="codepipeline", transformation_steps=[])
    mod.provision_infrastructure({"app_id": "APP-HE"})
    for f in ("plan.txt", "terraform-run.log", "validate.txt", "exposure.json"):
        env.s3.put_object(Bucket="b", Key=f"applications/APP-HE/execution/infrastructure/{f}", Body=b"x")
    mod.provision_infrastructure({"app_id": "APP-HE"})
    paths = {f["filePath"] for f in env.cc.commits[-1]["putFiles"]}
    assert "infra/main.tf" in paths and not paths & {"infra/plan.txt", "infra/terraform-run.log",
                                                     "infra/validate.txt", "infra/exposure.json"}


def test_migration_branch_pipeline_has_no_push_trigger(te):  # noqa: F811
    """A push to the migration branch must not deploy unreviewed code (2026-10-26)."""
    mod, env = te
    env.seed("APP-HE", migration_strategy="replatform", target_compute="ecs_fargate", target_database="none",
             cicd_target="codepipeline", transformation_steps=[])
    mod.provision_infrastructure({"app_id": "APP-HE"})
    tfvars = staged(env, "APP-HE", "infrastructure", "terraform.tfvars")
    assert re.search(r"^trigger_on_push\s*= false$", tfvars, re.M)
    assert "trigger_on_push  = var.trigger_on_push" in staged(env, "APP-HE", "infrastructure", "main.tf")
    env.table.items[("APP#APP-HE", "META")]["deploy_branch"] = "main"  # after merge: deploy on push
    mod.provision_infrastructure({"app_id": "APP-HE"})
    assert re.search(r"^trigger_on_push\s*= true$", staged(env, "APP-HE", "infrastructure", "terraform.tfvars"), re.M)


def test_approved_db_multi_az_false_wins_over_the_sla_rule(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE", migration_strategy="replatform", target_compute="ecs_fargate", target_database="rds_postgresql",
             db_instance_class="db.t4g.medium", cicd_target="codepipeline", transformation_steps=[],
             uptime_sla="99.95%", db_multi_az=False)
    mod.provision_infrastructure({"app_id": "APP-HE"})
    assert re.search(r"^db_multi_az\s*= false$", staged(env, "APP-HE", "infrastructure", "terraform.tfvars"), re.M)


def test_spring_profiles_are_detected(tmp_path):
    """PetClinic ships application-mysql.properties; the datasource alone would run its HSQL scripts."""
    res = tmp_path / "petclinic" / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (tmp_path / "petclinic" / "pom.xml").write_text("<project/>")
    (res / "application.properties").write_text("database=hsqldb\n")
    (res / "application-mysql.properties").write_text("database=mysql\n")
    (res / "application-dev.yml").write_text("x: 1\n")
    r = scan_wiring(tmp_path, "petclinic")
    assert r["spring_profiles"] == ["dev", "mysql"]
    assert r["sources"]["spring_profiles"] == "petclinic/src/main/resources"
