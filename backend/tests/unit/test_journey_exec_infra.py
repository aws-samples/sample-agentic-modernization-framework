"""Execution-phase IaC generation (tool_execution.provision_infrastructure).

Covers the cross-app audit findings for the three reference profiles:

  * APP-CLI  relocate   -> ec2 / same_as_source / gitlab       (CodeCommit repo)
  * APP-HE   replatform -> ecs_fargate / rds_postgresql / codepipeline
  * APP-TE   rehost     -> ec2 / same_as_source / github_actions (default branch master)

EC2 now gets a real compute module, managed databases get the rds module, the
root main.tf declares + wires only the variables of the included modules, and
terraform.tfvars sets exactly those. The state backend is prefix-derived and
everything is committed to the migration branch, never the default branch.

The fakes below (DynamoDB table, S3 serving the real template library, CodeCommit)
are shared with the other test_journey_exec_* modules.

Optional: AMF_TF_VALIDATE=1 runs `terraform init -backend=false && validate` on
the rendered root of each profile (needs the terraform binary + provider download).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_SRC = _REPO_ROOT / "infra" / "environments" / "agentcore" / "lambda-src"
TEMPLATES = _REPO_ROOT / "backend" / "resources" / "templates"

os.environ.setdefault("DYNAMODB_TABLE", "test_table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
if str(_LAMBDA_SRC) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_SRC))


# ── Fakes ──────────────────────────────────────────────────────────────────────

class FakeTable:
    """Minimal single-table DynamoDB: get_item / put_item / update_item / query(PK, begins_with SK)."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item):  # noqa: N803 — boto3 signature
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,  # noqa: N803
                    ExpressionAttributeNames=None, **_):
        item = self.items.setdefault((Key["PK"], Key["SK"]), {"PK": Key["PK"], "SK": Key["SK"]})
        names = ExpressionAttributeNames or {}
        vals = ExpressionAttributeValues or {}
        for part in UpdateExpression.replace("SET ", "", 1).split(","):
            k, v = (s.strip() for s in part.split("="))
            item[names.get(k, k)] = vals[v]

    def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=None, **_):  # noqa: N803
        pk = sk_prefix = None
        for cond in KeyConditionExpression._values:  # boto3 And(Equals(PK), BeginsWith(SK))
            key, val = cond._values
            if key.name == "PK":
                pk = val
            else:
                sk_prefix = val
        rows = sorted((it for (p, s), it in self.items.items() if p == pk and s.startswith(sk_prefix or "")),
                      key=lambda it: it["SK"], reverse=not ScanIndexForward)
        return {"Items": [dict(r) for r in rows[:Limit] if r] if Limit else [dict(r) for r in rows]}


class _Body:
    def __init__(self, data: bytes):
        self._d = data

    def read(self):
        return self._d


class FakeS3:
    """Serves the real template library for config/templates/* and stores staged objects."""

    def __init__(self, templates_prefix: str):
        self.prefix = templates_prefix
        self.objects: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key.startswith(self.prefix + "/"):
            path = TEMPLATES / Key[len(self.prefix) + 1:]
            if not path.is_file():
                raise FileNotFoundError(Key)
            return {"Body": _Body(path.read_bytes())}
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **_):  # noqa: N803
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def list_objects_v2(self, Bucket, Prefix, **_):  # noqa: N803
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def text(self, key: str) -> str:
        return self.objects[key].decode()


class FakeCodeCommit:
    class exceptions:  # noqa: N801 — mirrors boto3 client.exceptions
        class BranchDoesNotExistException(Exception):
            pass

        class NoChangeException(Exception):
            pass

        class SameFileContentException(Exception):
            pass

    def __init__(self, branches: dict[str, str], default_branch: str = "main"):
        self.branches = dict(branches)  # name -> head commit
        self.default_branch = default_branch
        self.commits: list[dict] = []
        self.created_branches: list[tuple[str, str]] = []
        self.folders: dict[str, dict] = {"/": {"files": [{"relativePath": "pom.xml"}], "subFolders": []}}

    def get_branch(self, repositoryName, branchName):  # noqa: N803
        if branchName not in self.branches:
            raise self.exceptions.BranchDoesNotExistException(branchName)
        return {"branch": {"commitId": self.branches[branchName]}}

    def create_branch(self, repositoryName, branchName, commitId):  # noqa: N803
        self.branches[branchName] = commitId
        self.created_branches.append((branchName, commitId))

    def create_commit(self, **kw):
        cid = f"{len(self.commits) + 1:040x}"
        self.commits.append(kw)
        self.branches[kw["branchName"]] = cid
        return {"commitId": cid}

    def get_repository(self, repositoryName):  # noqa: N803
        return {"repositoryMetadata": {"defaultBranch": self.default_branch}}

    def get_folder(self, repositoryName, folderPath, commitSpecifier):  # noqa: N803
        return self.folders.get(folderPath, {"files": [], "subFolders": []})


# ── Profiles (trimmed from the /tmp/review audit dumps) ─────────────────────────

PROFILES = {
    "APP-CLI": {
        "meta": {"app_id": "APP-CLI", "app_name": "CLI Application", "language": "Java", "build_tool": "Maven",
                 "framework": "Spring Boot 3.0.6 / Micronaut 3.9.1", "migration_strategy": "relocate",
                 "target_compute": "ec2", "target_database": "same_as_source", "cicd_target": "codepipeline",
                 "target_account": "123456789879", "vpc_cidr": "10.0.0.0/16", "tier": "tier_2",
                 "business_unit": "Telecom", "transformation_steps": []},
        "repo": {"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/serverless-java-frameworks-samples",
                 "branch": "main"},
        "assessment": {"detected_stack": {"language": "Java", "runtime_versions": {
            "dagger_module": "Java 11", "springboot_module": "Java 17"}}},
    },
    "APP-HE": {
        "meta": {"app_id": "APP-HE", "app_name": "HE Application", "language": "Java", "build_tool": "Maven",
                 "framework": "Spring Boot 4.1.0", "migration_strategy": "replatform",
                 "target_compute": "ecs_fargate", "target_database": "rds_postgresql", "cicd_target": "codepipeline",
                 "target_account": "987654321912", "vpc_cidr": "172.16.0.0/16", "tier": "tier_1",
                 "business_unit": "Health Services", "performance": {"uptime_sla": "99.99%"},
                 "database": {"engine": "PostgreSQL", "version": "13", "size_gb": 100},
                 "transformation_steps": ["AWS/java-version-upgrade", "AWS/python-version-upgrade",
                                          "AWS/java-aws-sdk-v1-to-v2"]},
        "repo": {"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/java-on-aws", "branch": "main"},
        "assessment": {"stack": {"language": "Java", "language_version": "21", "runtime": "Amazon Corretto 21"}},
    },
    "APP-TE": {
        "meta": {"app_id": "APP-TE", "app_name": "TE Application", "language": "Java", "build_tool": "Maven",
                 "framework": "Spring Boot 1.5.2.RELEASE", "migration_strategy": "rehost",
                 "target_compute": "ec2", "target_database": "same_as_source", "cicd_target": "codepipeline",
                 "target_account": "987654321981", "vpc_cidr": "192.168.0.0/16", "tier": "tier_1",
                 "business_unit": "Telecom", "performance": {"uptime_sla": "99.9%"}, "transformation_steps": []},
        "repo": {"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/amazon-ecs-java-microservices",
                 "branch": "master"},
        "assessment": {"detected_stack": {"language": "Java", "language_version": "1.8 (Java 8 — EOL)"}},
    },
}


@pytest.fixture
def te(monkeypatch):
    """tool_execution with fake AWS clients; returns (module, env) where env.seed(app) loads a profile."""
    te = importlib.import_module("tool_execution")
    table = FakeTable()
    s3 = FakeS3(te.TEMPLATES_PREFIX)
    cc = FakeCodeCommit({"main": "a" * 40, "master": "b" * 40})
    monkeypatch.setattr(te, "table", table)
    monkeypatch.setattr(te, "s3", s3)
    monkeypatch.setattr(te, "codecommit", cc)
    monkeypatch.setattr(te, "BUCKET", "test-bucket")
    monkeypatch.setattr(te, "PROJECT_PREFIX", "uat")
    # These journey tests exercise the deploy path; deploy-off is covered in test_deploy_disabled.py.
    monkeypatch.setattr(te, "DEPLOY_ENABLED", True)

    class Env:
        pass

    env = Env()
    env.table, env.s3, env.cc = table, s3, cc

    def seed(app_id: str, **meta_overrides):
        prof = PROFILES[app_id]
        meta = {**prof["meta"], **meta_overrides}
        table.put_item({"PK": f"APP#{app_id}", "SK": "META", **meta})
        name = prof["repo"]["url"].rstrip("/").split("/")[-1]
        table.put_item({"PK": f"APP#{app_id}", "SK": f"REPO#{name}", "name": name, **prof["repo"]})
        table.put_item({"PK": f"APP#{app_id}", "SK": "ANALYSIS#assessment-summary#2026-10-01T00:00:00Z",
                        "result": prof["assessment"]})
        return meta

    env.seed = seed
    return te, env


def staged(env, app_id: str, kind: str, rel: str) -> str:
    return env.s3.text(f"applications/{app_id}/execution/{kind}/{rel}")


def _declared_vars(main_tf: str) -> set[str]:
    return set(re.findall(r'^variable "(\w+)"', main_tf, re.M))


def _tfvars_keys(tfvars: str) -> set[str]:
    return set(re.findall(r"^(\w+)\s*=", tfvars, re.M))


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("app_id", ["APP-CLI", "APP-TE"])
def test_ec2_targets_get_the_ec2_module(te, app_id):
    mod, env = te
    env.seed(app_id)
    out = mod.provision_infrastructure({"app_id": app_id})
    assert out["status"] == "staged"
    assert out["modules"] == ["vpc", "ec2", "codepipeline"]  # CodePipeline is the only CI/CD target
    main_tf = staged(env, app_id, "infrastructure", "main.tf")
    assert 'source             = "./modules/ec2"' in main_tf
    assert "modules/ec2/main.tf" in out["files"]
    assert not any("No standardized module" in n for n in out["notes"])
    # The AMI is a required, human-supplied input — the note says so.
    assert any("ami_id is empty" in n for n in out["notes"])


@pytest.mark.parametrize("app_id", list(PROFILES))
def test_tfvars_set_exactly_the_declared_variables(te, app_id):
    mod, env = te
    env.seed(app_id)
    mod.provision_infrastructure({"app_id": app_id})
    main_tf = staged(env, app_id, "infrastructure", "main.tf")
    tfvars = staged(env, app_id, "infrastructure", "terraform.tfvars")
    assert _declared_vars(main_tf) == _tfvars_keys(tfvars)
    # Every declared variable is actually consumed (no dead `var.region`).
    for name in _declared_vars(main_tf) - {"region", "environment", "app_id", "tags"}:
        assert f"var.{name}" in main_tf, name
    assert "${" not in tfvars  # no unrendered template placeholders


@pytest.mark.parametrize("app_id", ["APP-CLI", "APP-TE"])
def test_ec2_alb_is_internal_and_never_open_to_the_internet(te, app_id):
    """APP-HE 2026-10-14: the ec2 module hardcoded an internet-facing ALB open to 0.0.0.0/0,
    so every EC2 app failed the InfrastructureValidate exposure policy."""
    mod, env = te
    env.seed(app_id)
    mod.provision_infrastructure({"app_id": app_id})
    tfvars = staged(env, app_id, "infrastructure", "terraform.tfvars")
    assert re.search(r"^internal_alb\s*=\s*true", tfvars, re.M)
    assert "0.0.0.0/0" not in re.search(r"^allowed_ingress_cidrs\s*=.*$", tfvars, re.M).group(0)
    ec2 = staged(env, app_id, "infrastructure", "modules/ec2/main.tf")
    alb_sg = ec2[ec2.index('resource "aws_security_group" "alb"'):ec2.index('resource "aws_security_group" "app"')]
    ingress_part = alb_sg.split("egress")[0]
    assert "cidr_blocks = var.allowed_ingress_cidrs" in ingress_part
    assert not re.search(r'cidr_blocks\s*=\s*\["0\.0\.0\.0/0"\]', ingress_part)
    assert re.search(r"internal\s*=\s*var\.internal_alb", ec2)


def test_ec2_root_has_no_container_or_lambda_settings(te):
    mod, env = te
    env.seed("APP-TE")
    mod.provision_infrastructure({"app_id": "APP-TE"})
    keys = _tfvars_keys(staged(env, "APP-TE", "infrastructure", "terraform.tfvars"))
    assert not keys & {"cpu", "memory", "desired_count", "container_port", "runtime", "lambda_runtime", "memory_size"}
    assert not any(k.startswith("db_") for k in keys)  # same_as_source -> no managed DB


def test_replatform_with_rds_includes_rds_and_codepipeline(te):
    mod, env = te
    env.seed("APP-HE")
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    assert out["modules"] == ["vpc", "ecs-fargate", "rds", "codepipeline"]
    main_tf = staged(env, "APP-HE", "infrastructure", "main.tf")
    tfvars = staged(env, "APP-HE", "infrastructure", "terraform.tfvars")
    assert 'engine                     = var.db_engine' in main_tf
    assert "allowed_security_group_ids = [module.compute.app_security_group_id]" in main_tf
    assert re.search(r'^db_engine\s*= "rds_postgresql"$', tfvars, re.M)
    # uptime_sla 99.99% >= 99.95% -> Multi-AZ, and the README says why.
    assert re.search(r"^db_multi_az\s*= true$", tfvars, re.M)
    assert "uptime_sla 99.99%" in staged(env, "APP-HE", "infrastructure", "README.md")
    # CodePipeline runs the COMMITTED buildspec and sources the branch the deploy gate
    # applies (the migration branch) — not the repo's default/linked branch.
    assert "buildspec        = var.buildspec_path" in main_tf
    assert re.search(r'^buildspec_path\s*= "cicd/buildspec.yml"$', tfvars, re.M)
    assert re.search(r'^branch\s*= "migration/app-he"$', tfvars, re.M)
    assert re.search(r"^cpu\s*= 1024$", tfvars, re.M)


@pytest.mark.parametrize("engine", ["aurora_mysql", "rds_postgresql", "rds_mysql", "aurora_postgresql"])
def test_every_managed_engine_uses_the_rds_module(te, engine):
    mod, env = te
    env.seed("APP-HE", target_database=engine)
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    assert "rds" in out["modules"]
    assert f'"{engine}"' in staged(env, "APP-HE", "infrastructure", "terraform.tfvars")


def test_dynamodb_target_uses_the_dynamodb_module(te):
    mod, env = te
    env.seed("APP-HE", target_database="dynamodb")
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    assert "dynamodb" in out["modules"] and "rds" not in out["modules"]


def test_multi_az_derivation_precedence(te):
    mod, _ = te
    assert mod._db_multi_az({"dr_availability": {"multi_az": "no"}, "performance": {"uptime_sla": "99.99%"}})[0] is False
    assert mod._db_multi_az({"multi_az": "yes"})[0] is True
    assert mod._db_multi_az({"performance": {"uptime_sla": "99.9%"}})[0] is False
    assert mod._db_multi_az({"performance": {"uptime_sla": "99.95"}})[0] is True
    assert mod._db_multi_az({"multi_region": "active_passive"})[0] is True
    assert mod._db_multi_az({})[0] is False


def test_az_count_from_dr_availability(te):
    mod, env = te
    assert mod._az_count({}) == 2
    assert mod._az_count({"dr_availability": {"availability_zones": 3}}) == 3
    assert mod._az_count({"dr_availability": json.dumps({"az_count": "3"})}) == 3
    assert mod._az_count({"availability_zones": ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d"]}) == 4
    env.seed("APP-TE", dr_availability={"availability_zones": 3})
    mod.provision_infrastructure({"app_id": "APP-TE"})
    assert re.search(r"^az_count\s*= 3$", staged(env, "APP-TE", "infrastructure", "terraform.tfvars"), re.M)
    vpc = (TEMPLATES / "terraform/modules/vpc/main.tf").read_text()
    assert "us-east-1a" not in vpc  # no region-pinned AZ defaults


def test_empty_tags_are_omitted(te):
    mod, env = te
    env.seed("APP-TE")
    mod.provision_infrastructure({"app_id": "APP-TE"})
    tfvars = staged(env, "APP-TE", "infrastructure", "terraform.tfvars")
    assert "CostCenter" not in tfvars
    assert 'BusinessUnit = "Telecom"' in tfvars


def test_backend_is_prefix_derived_and_provider_uses_vars(te):
    mod, env = te
    env.seed("APP-HE", target_region="us-west-2", environment="staging")
    mod.provision_infrastructure({"app_id": "APP-HE"})
    backend = staged(env, "APP-HE", "infrastructure", "backend.tf")
    assert 'bucket         = "uat-tfstate-987654321912-us-west-2"' in backend
    assert 'dynamodb_table = "uat-tfstate-lock"' in backend
    assert "amf-tfstate" not in backend
    assert "region = var.region" in backend and "Environment = var.environment" in backend
    tfvars = staged(env, "APP-HE", "infrastructure", "terraform.tfvars")
    assert re.search(r'^region\s*= "us-west-2"$', tfvars, re.M)
    assert re.search(r'^environment\s*= "staging"$', tfvars, re.M)
    readme = staged(env, "APP-HE", "infrastructure", "README.md")
    assert "Bootstrap the state backend" in readme and "aws dynamodb create-table --table-name uat-tfstate-lock" in readme


def test_iac_commits_to_migration_branch_created_from_default(te):
    mod, env = te
    env.seed("APP-TE")  # default branch master
    out = mod.provision_infrastructure({"app_id": "APP-TE"})
    assert out["commit"]["status"] == "committed"
    assert out["commit"]["branch"] == "migration/app-te"
    assert env.cc.created_branches == [("migration/app-te", "b" * 40)]  # from master's head
    assert env.cc.commits[0]["branchName"] == "migration/app-te"
    assert all(c["branchName"] not in ("main", "master") for c in env.cc.commits)


def test_explicit_default_branch_commit_is_refused(te):
    mod, env = te
    env.seed("APP-HE")
    # provision_infrastructure commits IaC internally; the shared _commit_staged_to_repo
    # guard is reached through the Gateway tool commit_cicd_to_repo.
    out = mod.commit_cicd_to_repo({"app_id": "APP-HE", "branch": "main"})
    assert out["status"] == "rejected"


def test_hcl_rendering_escapes_template_sequences(te):
    mod, _ = te
    assert mod._hcl("a${b}") == '"a$${b}"'
    assert mod._hcl(True) == "true" and mod._hcl(3) == "3"
    assert mod._hcl(["a", "b"]) == '["a", "b"]'


@pytest.mark.skipif(not (os.environ.get("AMF_TF_VALIDATE") and shutil.which("terraform")),
                    reason="set AMF_TF_VALIDATE=1 (needs terraform + provider download)")
@pytest.mark.parametrize("app_id", list(PROFILES))
def test_rendered_root_terraform_validates(te, app_id, tmp_path):
    mod, env = te
    env.seed(app_id)
    mod.provision_infrastructure({"app_id": app_id})
    prefix = f"applications/{app_id}/execution/infrastructure/"
    for key, body in env.s3.objects.items():
        if key.startswith(prefix):
            dest = tmp_path / key[len(prefix):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
    # Same local-backend override the infra worker uses (terraform_runner).
    (tmp_path / "_amf_local_backend_override.tf").write_text('terraform {\n  backend "local" {}\n}\n')
    init = subprocess.run(["terraform", f"-chdir={tmp_path}", "init", "-backend=false", "-input=false", "-no-color"],
                          capture_output=True, text=True)
    assert init.returncode == 0, init.stdout + init.stderr
    val = subprocess.run(["terraform", f"-chdir={tmp_path}", "validate", "-no-color"], capture_output=True, text=True)
    assert val.returncode == 0, val.stdout + val.stderr
    assert "Warning" not in val.stdout, val.stdout


def test_missing_create_branch_permission_still_attempts_the_commit(te):
    """The tool Lambda role may lack codecommit:CreateBranch: fall back to
    create_commit(parentCommitId=<default head>) on the migration branch."""
    mod, env = te

    def denied(**_):
        raise RuntimeError("AccessDeniedException: codecommit:CreateBranch")

    env.cc.create_branch = denied
    env.seed("APP-HE")
    out = mod.provision_infrastructure({"app_id": "APP-HE"})
    assert out["commit"]["status"] == "committed"
    assert env.cc.commits[0]["branchName"] == "migration/app-he"
    assert env.cc.commits[0]["parentCommitId"] == "a" * 40


def test_lift_onto_rds_mysql_wires_the_database_into_ec2(te):
    """Rehost/relocate may move a MySQL source onto RDS MySQL: EC2 gets the endpoint as env and the
    RDS-managed password from Secrets Manager at boot; a shipped mysql profile is activated."""
    mod, env = te
    env.seed("APP-TE", migration_strategy="relocate", target_compute="ec2", target_database="rds_mysql",
             ami_id="al2023", db_instance_class="db.t4g.medium", detected_spring_profiles=["mysql"])
    out = mod.provision_infrastructure({"app_id": "APP-TE"})
    assert out["modules"] == ["vpc", "ec2", "rds", "codepipeline"]
    main_tf = staged(env, "APP-TE", "infrastructure", "main.tf")
    compute = main_tf[main_tf.index('module "compute"'):main_tf.index('module "database"')]
    assert 'SPRING_DATASOURCE_URL = "jdbc:mysql://${module.database.endpoint}:' in compute
    assert 'SPRING_PROFILES_ACTIVE = "mysql"' in compute
    assert 'SPRING_DATASOURCE_PASSWORD = { arn = module.database.master_user_secret_arn, key = "password" }' in compute
    assert "secret_kms_key_arns = [module.database.kms_key_arn]" in compute
    assert "allowed_security_group_ids = [module.compute.app_security_group_id]" in main_tf
    assert _declared_vars(main_tf) == _tfvars_keys(staged(env, "APP-TE", "infrastructure", "terraform.tfvars"))


def test_no_profile_is_activated_without_a_matching_one(te):
    mod, env = te
    env.seed("APP-HE", detected_spring_profiles=["dev", "mysql"])  # target is rds_postgresql
    mod.provision_infrastructure({"app_id": "APP-HE"})
    assert "SPRING_PROFILES_ACTIVE" not in staged(env, "APP-HE", "infrastructure", "main.tf")
