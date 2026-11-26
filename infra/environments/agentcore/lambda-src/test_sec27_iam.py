"""IAM + infra hardening contracts.

- every IAM role in the committed app Terraform templates carries the AMF app
  permissions boundary, and every module that creates a role declares the variable;
- provision_infrastructure's spec renders the boundary ARN into terraform.tfvars
  and wires it into every role-creating module block of the root main.tf;
- the deploy role can only create/grow APP-* roles with that boundary, and denies
  boundary removal, privileged managed-policy attachment and sts:AssumeRole APP-*;
- PassRole on the tool Lambda / worker roles lists exact roles (no prefix wildcard);
- hosted mode refuses to plan without Cognito / with dev auth; the portal has a CSP.
"""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DYNAMODB_TABLE", "test_table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

import tool_execution as te  # noqa: E402

HERE = Path(__file__).resolve().parent
ENV_DIR = HERE.parent
REPO_ROOT = HERE.parents[3]
TEMPLATES = REPO_ROOT / "backend" / "resources" / "templates" / "terraform"
MODULES = TEMPLATES / "modules"


def _resource_blocks(text: str, rtype: str):
    """(name, body) of each top-level `resource "<rtype>" "<name>" { ... }` block."""
    for m in re.finditer(r'^resource "' + re.escape(rtype) + r'" "([^"]+)" \{\n(.*?)^\}', text, re.S | re.M):
        yield m.group(1), m.group(2)


# ── Templates: every app role is bounded ────────────────────────────────


def test_every_template_iam_role_sets_the_permissions_boundary():
    found = 0
    for tf in sorted(MODULES.glob("*/main.tf")):
        text = tf.read_text()
        for name, body in _resource_blocks(text, "aws_iam_role"):
            found += 1
            assert re.search(r"^\s*permissions_boundary\s*=\s*var\.permissions_boundary_arn\s*$", body, re.M), \
                f"{tf.parent.name}: aws_iam_role.{name} has no permissions_boundary = var.permissions_boundary_arn"
        if 'resource "aws_iam_role" ' in text:
            assert re.search(r'^variable "permissions_boundary_arn"', text, re.M), \
                f"{tf.parent.name}: creates IAM roles but declares no permissions_boundary_arn variable"
    assert found >= 8  # codepipeline(4) + ecs-fargate(2) + ec2 + lambda


def test_no_template_attaches_a_privileged_managed_policy():
    for tf in MODULES.glob("*/main.tf"):
        for arn in re.findall(r'policy_arn\s*=\s*"([^"]+)"', tf.read_text()):
            assert not re.search(r"(AdministratorAccess|PowerUserAccess|FullAccess)$", arn), (tf, arn)


# ── tool_execution renders the boundary ─────────────────────────────────

_REPO = {"host": "codecommit", "name": "app-repo", "default_branch": "main"}


def _meta(compute: str, strategy: str, db: str = "none") -> dict:
    return {"app_id": "APP-SEC", "migration_strategy": strategy, "target_compute": compute,
            "target_database": db, "cicd_target": "codepipeline", "target_account": "123456789012",
            "vpc_cidr": "10.20.0.0/16", "language": "java", "ami_id": "al2023"}


@pytest.fixture
def boundary_env(monkeypatch):
    monkeypatch.setattr(te, "PROJECT_PREFIX", "amfx")
    monkeypatch.setattr(te, "APP_PERMISSIONS_BOUNDARY_NAME", "amfx-app-permissions-boundary")
    monkeypatch.setattr(te, "AWS_PARTITION", "aws")


@pytest.mark.parametrize("compute,strategy,module", [
    ("ecs_fargate", "replatform", "ecs-fargate"),
    ("lambda", "refactor", "lambda"),
    ("ec2", "rehost", "ec2"),
])
def test_tfvars_and_root_wire_the_boundary(boundary_env, compute, strategy, module):
    spec = te._infra_spec("APP-SEC", _meta(compute, strategy), {}, _REPO)
    arn = "arn:aws:iam::123456789012:policy/amfx-app-permissions-boundary"
    tfvars = te._tfvars_body(spec)
    assert re.search(r'^permissions_boundary_arn\s*=\s*"' + re.escape(arn) + '"$', tfvars, re.M), tfvars
    root = te._root_main_tf(spec)
    assert 'variable "permissions_boundary_arn" { type = string }' in root
    # Every module that creates roles (compute + pipeline) receives it.
    for mod in (module, "codepipeline"):
        block = re.search(r'module "\w+" \{\n  source\s*=\s*"\./modules/' + re.escape(mod) + r'"\n(.*?)\n\}', root, re.S)
        assert block, f"{mod} block missing:\n{root}"
        assert re.search(r"permissions_boundary_arn\s*=\s*var\.permissions_boundary_arn", block.group(1)), block.group(1)
    # Every module in the spec that declares the variable gets it (no silent gaps).
    for mod in spec["modules"]:
        if 'variable "permissions_boundary_arn"' in (MODULES / mod / "main.tf").read_text():
            assert f'./modules/{mod}"' in root


def test_boundary_name_defaults_from_prefix(monkeypatch):
    monkeypatch.setattr(te, "PROJECT_PREFIX", "uat")
    monkeypatch.setattr(te, "APP_PERMISSIONS_BOUNDARY_NAME", "")
    monkeypatch.setattr(te, "AWS_PARTITION", "aws-us-gov")
    assert te._permissions_boundary_arn("111122223333") == \
        "arn:aws-us-gov:iam::111122223333:policy/uat-app-permissions-boundary"


def test_tfvars_template_still_renders_body():
    assert "${tfvars_body}" in (TEMPLATES / "environments" / "template.tfvars").read_text()


# ── Platform Terraform (static checks; terraform validate covers syntax) ──


def _tf(name: str) -> str:
    return (ENV_DIR / name).read_text()


def test_deploy_role_requires_the_boundary_and_denies_escalation():
    deploy = _tf("deploy.tf")
    allow = re.search(r'sid\s*=\s*"AppScopedIam".*?\n  \}', deploy, re.S).group(0)
    for action in ("iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:PutRolePermissionsBoundary"):
        assert f'"{action}"' not in allow, f"{action} must only be granted with the boundary condition"
    bounded = re.search(r'sid\s*=\s*"BoundedAppRoleWrite".*?\n  \}', deploy, re.S).group(0)
    assert '"iam:PermissionsBoundary"' in bounded and "local.app_permissions_boundary_arn" in bounded
    for sid in ("DenyBoundaryRemoval", "DenyBoundaryPolicyTamper", "DenyPrivilegedManagedPolicies",
                "DenyAssumeAppRoles", "DenyPassDeployRole"):
        assert re.search(r'sid\s*=\s*"' + sid + r'"\s*\n\s*effect\s*=\s*"Deny"', deploy), sid
    for p in ("AdministratorAccess", "PowerUserAccess", "IAMFullAccess", "*FullAccess"):
        assert f"policy/{p}\"" in deploy
    assert 'resource "aws_iam_policy" "app_permissions_boundary"' in deploy


def test_boundary_policy_grants_no_iam_write_or_assume_role():
    deploy = _tf("deploy.tf")
    body = re.search(r'data "aws_iam_policy_document" "app_permissions_boundary" \{(.*?)^\}', deploy, re.S | re.M).group(1)
    for st in re.findall(r"statement \{(.*?)\n  \}", body, re.S):
        if 'effect = "Deny"' in st or re.search(r'effect\s*=\s*"Deny"', st):
            continue
        listed = re.search(r"actions\s*=\s*\[(.*?)\]", st, re.S).group(1)
        actions = re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', listed)
        assert actions
        for a in actions:
            assert not a.startswith(("sts:", "organizations:", "account:")), a
            assert not a.startswith("iam:") or a == "iam:PassRole", a


# The ONLY boundary Allow statement that may keep Resource "*", and its exact actions. Every action here has
# no resource-level permissions (Service Authorization Reference) or is the SSM agent set that
# AmazonSSMManagedInstanceCore grants on "*". Adding an action must fail this test until it is reviewed.
_BOUNDARY_STAR_STATEMENT = "NoResourceLevelPermissions"
_BOUNDARY_STAR_ACTIONS = {
    "logs:DescribeLogGroups", "cloudwatch:PutMetricData", "cloudwatch:GetMetricData",
    "xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets",
    "xray:GetSamplingStatisticSummaries", "tag:GetResources", "s3:GetEncryptionConfiguration",
    "ecr:GetAuthorizationToken", "ec2:Describe*",
    "ssm:DescribeAssociation", "ssm:GetDeployablePatchSnapshotForInstance", "ssm:GetDocument",
    "ssm:DescribeDocument", "ssm:GetManifest", "ssm:ListAssociations", "ssm:ListInstanceAssociations",
    "ssm:PutInventory", "ssm:PutComplianceItems", "ssm:PutConfigurePackageResult", "ssm:UpdateAssociationStatus",
    "ssm:UpdateInstanceAssociationStatus", "ssm:UpdateInstanceInformation", "ssm:GetParameter",
    "ssm:GetParameters", "ssmmessages:*", "ec2messages:*",
    "ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition", "autoscaling:Describe*",
    "elasticloadbalancing:Describe*",
}


def _boundary_statements():
    deploy = _tf("deploy.tf")
    body = re.search(r'data "aws_iam_policy_document" "app_permissions_boundary" \{(.*?)^\}', deploy, re.S | re.M).group(1)
    for st in re.findall(r"statement \{(.*?)\n  \}", body, re.S):
        sid = re.search(r'sid\s*=\s*"([^"]+)"', st).group(1)
        actions = set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', re.search(r"actions\s*=\s*\[(.*?)\]", st, re.S).group(1)))
        yield sid, st, actions


def test_boundary_star_is_one_statement_with_a_pinned_action_list():
    starred = {}
    for sid, st, actions in _boundary_statements():
        if re.search(r'effect\s*=\s*"Deny"', st):
            continue
        if re.search(r'resources\s*=\s*\["\*"\]', st):
            starred[sid] = actions
    assert set(starred) == {_BOUNDARY_STAR_STATEMENT}, sorted(starred)
    assert starred[_BOUNDARY_STAR_STATEMENT] == _BOUNDARY_STAR_ACTIONS, (
        sorted(starred[_BOUNDARY_STAR_STATEMENT] ^ _BOUNDARY_STAR_ACTIONS))


def test_no_other_boundary_allow_uses_a_wildcard_service():
    for sid, st, actions in _boundary_statements():
        if re.search(r'effect\s*=\s*"Deny"', st) or sid == _BOUNDARY_STAR_STATEMENT:
            continue
        assert re.search(r'resources\s*=\s*\[\s*"arn:', st), f"{sid} must name ARNs"


# ── B3: the generated CodePipeline module ─────────────────────────────────────

def _codepipeline_tf():
    return (TEMPLATES / "modules" / "codepipeline" / "main.tf").read_text()


def test_codepipeline_passrole_names_only_the_apps_task_and_execution_roles():
    tf = _codepipeline_tf()
    lines = [ln for ln in tf.splitlines() if '"iam:PassRole"' in ln]
    assert lines, "PassRole statement missing"
    for ln in lines:
        assert 'Resource = "*"' not in ln and "role/*" not in ln, ln
        assert "${local.name}-ecs-task-role" in ln and "${local.name}-ecs-exec-role" in ln, ln
        assert '"iam:PassedToService" = "ecs-tasks.amazonaws.com"' in ln and "StringEquals" in ln, ln


def test_codepipeline_ecs_service_actions_are_scoped():
    tf = _codepipeline_tf()
    for action in ("ecs:UpdateService", "ecs:DescribeServices", "ecs:DescribeTasks", "ecs:ListTasks", "ecs:TagResource"):
        ln = next(ln for ln in tf.splitlines() if f'"{action}"' in ln)
        assert 'Resource = "*"' not in ln, ln
    star = next(ln for ln in tf.splitlines() if '"ecs:RegisterTaskDefinition"' in ln)
    assert set(re.findall(r'"(ecs:[A-Za-z]+)"', star)) == {"ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"}


def test_every_module_requires_the_permissions_boundary():
    for mod in ("codepipeline", "ecs-fargate", "lambda", "ec2"):
        tf = (TEMPLATES / "modules" / mod / "main.tf").read_text()
        block = re.search(r'variable "permissions_boundary_arn" \{(.*?)\n\}', tf, re.S).group(1)
        assert "default" not in block, mod


# ── B4: the worker role's AWS Transform grants ───────────────────────────────

def test_worker_transform_grants_are_scoped():
    worker = (REPO_ROOT / "infra" / "modules" / "worker-task" / "main.tf").read_text()
    assert '"transform:*"' not in worker
    deny = re.search(r'Sid\s*=\s*"TransformCustomNoShareOrDelete"(.*?)\}', worker, re.S).group(1)
    assert 'Effect = "Deny"' in deny
    for action in ("transform-custom:PutResourcePolicy", "transform-custom:DeleteResourcePolicy",
                   "transform-custom:ShareTransformationPackage", "transform-custom:UnshareTransformationPackage",
                   "transform-custom:Delete*"):
        assert f'"{action}"' in deny, action


def test_harness_runtime_role_has_no_agentcore_wildcard():
    main = _tf("main.tf")
    assert '"bedrock-agentcore:*"' not in main


def test_passrole_lists_exact_roles():
    lambdas = _tf("lambdas.tf")
    assert ':role/${var.project_prefix}-*"' not in lambdas
    worker = (REPO_ROOT / "infra" / "modules" / "worker-task" / "main.tf").read_text()
    assert ':role/${var.project_prefix}-*"' not in worker
    ecs = (REPO_ROOT / "infra" / "modules" / "ecs" / "main.tf").read_text()
    # The ecs module's unused, broad `{prefix}-ecs-task` role was removed; the only task role is worker-task's.
    assert '"iam:PassRole"' not in ecs and 'resource "aws_iam_role" "ecs_task"' not in ecs
    for text in (lambdas, worker):
        assert '"iam:PassedToService" = "ecs-tasks.amazonaws.com"' in text
        assert "deploy-role" in text or "deploy_role_named_arn" in text


def test_probe_dynamodb_is_not_any_table():
    validation = _tf("validation.tf")
    stmts = re.findall(r'Action\s*=\s*\["dynamodb:GetItem", "dynamodb:DeleteItem"\]\s*\n\s*Resource\s*=\s*"([^"]+)"(.*?)\n      \}',
                       validation, re.S)
    assert stmts
    for resource, rest in stmts:
        assert resource.endswith(":table/APP-*") or "aws:ResourceTag/" in rest, resource


def test_hosted_mode_requires_cognito_and_no_dev_auth():
    hosting = _tf("public-hosting.tf")
    assert re.search(r"precondition \{\s*\n\s*condition\s*=\s*var\.enable_cognito && lower\(trimspace\(var\.dev_auth\)\) == \"false\"",
                     hosting)


def test_portal_has_csp_and_security_headers():
    hosting = _tf("public-hosting.tf")
    assert 'variable "portal_csp"' in hosting
    for directive in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'", "object-src 'none'"):
        assert directive in hosting
    assert hosting.count("response_headers_policy_id = aws_cloudfront_response_headers_policy.portal_security[0].id") == 2
    for block in ("strict_transport_security", "content_type_options", "frame_options", "referrer_policy"):
        assert block in hosting


# ── Boundary: account scope for S3, and the control-plane Deny ───────────────

def _boundary_statement(sid: str) -> str:
    return next(st for s, st, _ in _boundary_statements() if s == sid)


def test_app_buckets_are_limited_to_this_account():
    st = _boundary_statement("AppBuckets")
    assert 'variable = "aws:ResourceAccount"' in st and "local.account_id" in st


def test_control_plane_deny_covers_every_service_the_boundary_allows_account_wide():
    st = _boundary_statement("NoAmfControlPlane")
    assert re.search(r'effect\s*=\s*"Deny"', st)
    actions = set(re.findall(r'"([a-z0-9-]+):\*"', re.search(r"actions\s*=\s*\[(.*?)\]", st, re.S).group(1)))
    assert actions == {"dynamodb", "s3", "secretsmanager", "states", "ecs", "ecr", "codecommit", "kms"}, actions
    for needle in (
        "table/${local.prefix_us}_*",        # {prefix_us}_portfolio (Terraform replaces "-" with "_")
        "table/${var.project_prefix}_*",     # {prefix}_tf_lock (setup.sh keeps the raw prefix)
        "s3:::${var.project_prefix}-*",
        "secret:${var.project_prefix}/*",
        "states:*:*:*:${var.project_prefix}-*",
        "ecs:*:*:*/${var.project_prefix}-*",
        "repository/${var.project_prefix}-*",
        "codecommit:*:*:${var.project_prefix}-*",
        "aws_kms_key.platform.arn",
    ):
        assert needle in st, needle


def test_project_prefix_is_validated():
    main = _tf("main.tf")
    block = main[main.index('variable "project_prefix" {'):]
    block = block[:block.index("\n}\n")]
    assert '"^[a-z][a-z0-9-]{1,11}$"' in block           # 12 chars keeps the boundary under 6,144
    assert '"^app(-|$)"' in block                         # would collide with app-* / APP-* resources


def test_codebuild_logs_are_scoped_to_the_projects_log_group():
    tf = _codepipeline_tf()
    i = tf.index('"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"')
    stmt = tf[i:tf.index("},", i)]
    assert 'Resource = "*"' not in stmt
    assert "log-group:/aws/codebuild/${local.name}-build" in stmt and "log-group:/aws/codebuild/${local.name}-build:*" in stmt


# ── Platform roles are described; RunTask tagging stays scoped ───────────


def _platform_tf():
    for tf in sorted(list(ENV_DIR.glob("*.tf")) + list((REPO_ROOT / "infra" / "modules").glob("*/main.tf"))):
        yield tf, tf.read_text()


def test_every_platform_iam_role_has_a_description():
    found = 0
    for tf, text in _platform_tf():
        for name, body in _resource_blocks(text, "aws_iam_role"):
            found += 1
            m = re.search(r'^\s*description\s*=\s*"([^"]+)"', body, re.M)
            assert m, f"{tf.name}: aws_iam_role.{name} has no description"
            assert all(ord(c) < 127 for c in m.group(1)), f"{tf.name}: {name} description must be ASCII (IAM)"
    assert found >= 9


def test_ecs_tag_resource_only_as_part_of_run_task():
    text = (ENV_DIR / "lambdas.tf").read_text()
    assert text.count('"ecs:TagResource"') == 1, "expected exactly one ecs:TagResource statement"
    i = text.index('"ecs:TagResource"')
    stmt = text[text.rindex("{", 0, i):text.index("Condition", i)]
    cond = text[text.index("Condition", i):].split("\n", 1)[0]
    assert '"ecs:CreateAction" = "RunTask"' in cond
    assert ":task/*" in stmt and 'Resource  = "*"' not in stmt
