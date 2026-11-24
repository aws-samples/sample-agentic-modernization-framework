"""Single source of truth for what inputs are valid per 7R strategy.

Pure, dependency-free (stdlib only) so the same module backs:
  • the FastAPI validator on PUT /portfolio/apps/{id}
  • future ASL / Lambda strategy gates
Mirror of frontend/src/workflow/strategyOptions.ts — keep in sync. The frontend
hides invalid dropdown options; the backend REJECTS invalid combinations even
if a client bypasses the UI.

Strategy semantics (AWS 7R):
  rehost / relocate          → lift-and-shift → compute=ec2, DB as-is or DynamoDB, no transforms
  replatform / refactor      → cloud-native   → compute from the modern set, DB migratable
  retire / retain / repurchase → close-out    → no target compute / DB / CI-CD (the workflow
                                                routes these straight to Handover)

Scope: only what is proven end to end — ECS Fargate and Lambda for
replatform/refactor (EC2 for rehost/relocate only), DynamoDB / RDS / Aurora
(PostgreSQL, MySQL), CI/CD on AWS CodePipeline (CodeCommit source) only.
"""

from __future__ import annotations

import re

# Strategies the workflow closes out (ResolveStrategy → Handover): no AWS target.
CLOSE_OUT_STRATEGIES = ("retire", "retain", "repurchase")

# ── compute (target_compute) ──────────────────────────────────────────────────
_COMPUTE_LIFT = {"ec2"}
_COMPUTE_MODERN = {"ecs_fargate", "lambda"}
_COMPUTE_NONE = {"none"}


def compute_options_for(strategy: str | None) -> set[str]:
    s = (strategy or "").lower()
    if s in ("rehost", "relocate"):
        return _COMPUTE_LIFT
    if s in ("replatform", "refactor"):
        return _COMPUTE_MODERN
    if s in CLOSE_OUT_STRATEGIES:
        return _COMPUTE_NONE
    return _COMPUTE_MODERN


# ── database (target_database) ────────────────────────────────────────────────
# Lift keeps the source DB, lands its data in a new DynamoDB table, or moves a MySQL source
# onto RDS MySQL (same engine: the app only gets a new endpoint — no code transforms).
_DB_LIFT = {"same_as_source", "dynamodb", "rds_mysql"}
_DB_MODERN = {
    "aurora_postgresql", "aurora_mysql",
    "rds_postgresql", "rds_mysql",
    "dynamodb", "none",
}
_DB_NONE = {"none"}


def database_options_for(strategy: str | None) -> set[str]:
    s = (strategy or "").lower()
    if s in ("rehost", "relocate"):
        return _DB_LIFT
    if s in ("replatform", "refactor"):
        return _DB_MODERN
    if s in CLOSE_OUT_STRATEGIES:
        return _DB_NONE
    return _DB_MODERN


# ── CI/CD (cicd_target) ───────────────────────────────────────────────────────
_CICD_FULL = {"codepipeline"}


def cicd_options_for(strategy: str | None) -> set[str]:
    s = (strategy or "").lower()
    if s in CLOSE_OUT_STRATEGIES:
        return {"none"}
    return _CICD_FULL


def transforms_allowed_for(strategy: str | None) -> bool:
    """Only replatform/refactor migrate code. Everything else runs AS-IS."""
    return (strategy or "").lower() in ("replatform", "refactor")


def requires_aws_targeting(strategy: str | None) -> bool:
    """AWS Account ID + VPC CIDR are mandatory unless the app is closed out (retired, kept on-prem, SaaS)."""
    return (strategy or "").lower() not in CLOSE_OUT_STRATEGIES


# ── Conditional sizing inputs (assessment approval) ──────────────────────────
# EC2 targets launch from an AMI (MGN-launched or golden image) — required so the
# generated ec2 module can plan. Managed DB targets need an instance class; the
# valid families differ per engine (Aurora: r/t only).
_MANAGED_DB = {"rds_postgresql", "rds_mysql", "aurora_postgresql", "aurora_mysql"}
_DB_CLASSES_GRAVITON = ["db.t4g.medium", "db.t4g.large", "db.m6g.large", "db.m6g.xlarge", "db.m6g.2xlarge",
                        "db.r6g.large", "db.r6g.xlarge", "db.r6g.2xlarge", "db.r6g.4xlarge"]
_DB_CLASSES_AURORA = ["db.t4g.medium", "db.t4g.large", "db.r6g.large", "db.r6g.xlarge", "db.r6g.2xlarge",
                      "db.r6g.4xlarge"]


def requires_ami(target_compute: str | None) -> bool:
    return (target_compute or "").lower() == "ec2"


def requires_db_instance_class(target_database: str | None) -> bool:
    return (target_database or "").lower() in _MANAGED_DB


def db_instance_classes_for(target_database: str | None) -> list[str]:
    td = (target_database or "").lower()
    if td.startswith("aurora_"):
        return _DB_CLASSES_AURORA
    if td in _MANAGED_DB:
        return _DB_CLASSES_GRAVITON
    return []


# ── Validators ────────────────────────────────────────────────────────────────
AWS_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
# An MGN/golden AMI id, or "al2023": a fresh Amazon Linux 2023 instance bootstrapped by the ec2 module.
AMI_ID_RE = re.compile(r"^(?:ami-(?:[0-9a-f]{8}|[0-9a-f]{17})|al2023)$")
VPC_CIDR_RE = re.compile(
    # RFC1918 ranges: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16. VPC prefix /16–/24: the generated vpc module
    # needs room for 3 tiers × AZs of subnets no smaller than AWS's /28 (a /24 → /27 subnets).
    r"^(?:10\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}|192\.168\.\d{1,3})\.\d{1,3}/(?:1[6-9]|2[0-4])$"
)


def vpc_cidr_ok(cidr: str) -> bool:
    """RFC1918 /16–/24 AND a network address (no host bits) — what CreateVpc accepts.

    APP-HE/APP-TE (2026-10-13) were approved with 10.0.0.2/24 and 10.0.0.1/24.
    """
    import ipaddress

    if not VPC_CIDR_RE.match(cidr or ""):
        return False
    try:
        ipaddress.ip_network(cidr, strict=True)
    except ValueError:
        return False
    return True


def validate_assessment_inputs(strategy: str | None, inputs: dict, require_sizing: bool = True) -> list[dict]:
    """Return a list of {field,message} errors; empty list = ok.

    `require_sizing=False` still validates the format of target_account / vpc_cidr /
    ami_id / db_instance_class when given, but doesn't demand them — for generic app
    edits (e.g. linking a repo in Discovery) made before the app has been through
    assessment approval, where those inputs are collected (the Execution pre-hook
    enforces them).

    Mirrors the frontend validator so the UI's "disabled dropdown" guarantee
    survives any direct API call that bypasses it.
    """
    errs: list[dict] = []
    s = (strategy or "").lower()

    tc = inputs.get("target_compute")
    if tc and tc not in compute_options_for(s):
        allowed = ", ".join(sorted(compute_options_for(s)))
        errs.append({"field": "target_compute",
                     "message": f"target_compute='{tc}' is not valid for strategy='{s}'. Allowed: {allowed}."})

    td = inputs.get("target_database")
    if td and td not in database_options_for(s):
        allowed = ", ".join(sorted(database_options_for(s)))
        errs.append({"field": "target_database",
                     "message": f"target_database='{td}' is not valid for strategy='{s}'. Allowed: {allowed}."})

    cc = inputs.get("cicd_target")
    if cc and cc not in cicd_options_for(s):
        errs.append({"field": "cicd_target",
                     "message": f"cicd_target='{cc}' is not valid for strategy='{s}'."})

    # transformations zero out for non-code-changing strategies
    steps = inputs.get("transformation_steps") or []
    if steps and not transforms_allowed_for(s):
        errs.append({"field": "transformation_steps",
                     "message": f"transformation_steps are not applicable to strategy='{s}' — the code migrates AS-IS."})

    if requires_aws_targeting(s):
        acct = (inputs.get("target_account") or "").strip()
        if not acct and require_sizing:
            errs.append({"field": "target_account",
                         "message": "AWS Account ID is required for this strategy."})
        elif acct and not AWS_ACCOUNT_ID_RE.match(acct):
            errs.append({"field": "target_account",
                         "message": "AWS Account ID must be exactly 12 digits."})

        cidr = (inputs.get("vpc_cidr") or "").strip()
        if not cidr and require_sizing:
            errs.append({"field": "vpc_cidr",
                         "message": "VPC CIDR is required for this strategy."})
        elif cidr and not vpc_cidr_ok(cidr):
            errs.append({"field": "vpc_cidr",
                         "message": "VPC CIDR must be an RFC1918 /16–/28 network address (e.g. 10.0.0.0/16, not 10.0.0.1/24)."})

    if requires_ami(tc):
        ami = (inputs.get("ami_id") or "").strip()
        if not ami and require_sizing:
            errs.append({"field": "ami_id",
                         "message": "AMI ID is required for an EC2 target (the MGN-launched or golden AMI)."})
        elif ami and not AMI_ID_RE.match(ami):
            errs.append({"field": "ami_id",
                         "message": "AMI ID must look like ami-0123456789abcdef0 (8 or 17 hex characters), or be al2023."})

    if requires_db_instance_class(td):
        cls = (inputs.get("db_instance_class") or "").strip()
        if not cls and require_sizing:
            errs.append({"field": "db_instance_class",
                         "message": f"DB instance class is required for target_database='{td}'."})
        elif cls and cls not in db_instance_classes_for(td):
            errs.append({"field": "db_instance_class",
                         "message": f"db_instance_class='{cls}' is not valid for {td}. Allowed: "
                                    + ", ".join(db_instance_classes_for(td)) + "."})

    return errs
