"""Gateway Tool target: Execution — ATX ECS launcher + generate/stage IaC & CI/CD."""
import json
import logging
import os
import re
import time
import uuid

import boto3
from agent_tools import ToolRegistry, json_safe
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()

TABLE = os.environ["DYNAMODB_TABLE"]
BUCKET = os.environ.get("S3_BUCKET", "")
REGION = os.environ["AWS_REGION"]  # set by the Lambda runtime
PROJECT_PREFIX = os.environ.get("PROJECT_PREFIX", "")
# Standardized template library (synced to the data bucket by setup.sh).
TEMPLATES_PREFIX = os.environ.get("TEMPLATES_PREFIX", "config/templates")
# No hardcoded resource literals: these must be provided by the environment.
ECS_CLUSTER = os.environ.get("ECS_CLUSTER", "")
ECS_TASK_DEF = os.environ.get("ECS_TASK_DEF", "")
ECS_SUBNETS = [s.strip() for s in os.environ.get("ECS_SUBNETS", "").split(",") if s.strip()]
# Workers run in public subnets with no NAT, so they need a public IP to pull the image.
ECS_ASSIGN_PUBLIC_IP = os.environ.get("ECS_ASSIGN_PUBLIC_IP", "ENABLED")
WORKER_CONTAINER_NAME = os.environ.get("WORKER_CONTAINER_NAME", "migration-worker")
GIT_AUTHOR_NAME = os.environ.get("GIT_AUTHOR_NAME", "AMF Execution Agent")
GIT_AUTHOR_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "amf-agent@migration.local")
ATX_DEFAULT_TD = os.environ.get("ATX_CODE_ANALYSIS_TD_NAME", "AWS/comprehensive-codebase-analysis")
ATX_SHELL_TIMEOUT = os.environ.get("ATX_SHELL_TIMEOUT", "36000")
# The app's Terraform state backend in the TARGET account (str.format with prefix/account/region).
APP_TFSTATE_BUCKET_PATTERN = os.environ.get("APP_TFSTATE_BUCKET_PATTERN", "{prefix}-tfstate-{account}-{region}")
APP_TFSTATE_LOCK_PATTERN = os.environ.get("APP_TFSTATE_LOCK_PATTERN", "{prefix}-tfstate-lock")
# Permissions boundary every generated app IAM role carries (deploy.tf): the
# deploy role may only create/modify APP-* roles that have exactly this boundary attached.
APP_PERMISSIONS_BOUNDARY_NAME = os.environ.get("APP_PERMISSIONS_BOUNDARY_NAME", "")
AWS_PARTITION = os.environ.get("AWS_PARTITION", "aws")
# ECS RunTask retries (transient ServerException/Throttling or no task placed): exponential backoff.
RUNTASK_MAX_ATTEMPTS = max(1, int(os.environ.get("RUNTASK_MAX_ATTEMPTS", "4")))
RUNTASK_BACKOFF_BASE_SECONDS = float(os.environ.get("RUNTASK_BACKOFF_BASE_SECONDS", "3"))
# Upgrade targets used when transformation_steps must be derived from the language.
JAVA_UPGRADE_TARGET_DEFAULT = os.environ.get("JAVA_UPGRADE_TARGET_DEFAULT", "21")
PYTHON_UPGRADE_TARGET_DEFAULT = os.environ.get("PYTHON_UPGRADE_TARGET_DEFAULT", "3.12")
DOTNET_UPGRADE_TARGET_DEFAULT = os.environ.get("DOTNET_UPGRADE_TARGET_DEFAULT", "8")
# Runtime versions when neither META, the upgrade target nor the assessment says otherwise.
DEFAULT_JAVA_VERSION = int(os.environ.get("DEFAULT_JAVA_VERSION", "17"))
DEFAULT_PYTHON_VERSION = os.environ.get("DEFAULT_PYTHON_VERSION", "3.12")
DEFAULT_NODE_VERSION = os.environ.get("DEFAULT_NODE_VERSION", "20")
# CI coverage gate (%) when META coverage_threshold is unset (0 = opted out, with a reason).
COVERAGE_THRESHOLD_DEFAULT = int(os.environ.get("COVERAGE_THRESHOLD_DEFAULT", "70"))
# uptime_sla (%) at or above which the managed database is built Multi-AZ.
MULTI_AZ_SLA_THRESHOLD = float(os.environ.get("MULTI_AZ_SLA_THRESHOLD", "99.95"))
# Generated Lambda compute: memory (MB) / timeout (s) when the repo template and META don't say.
GENERATED_LAMBDA_MEMORY_DEFAULT = int(os.environ.get("GENERATED_LAMBDA_MEMORY_DEFAULT", "512"))
GENERATED_LAMBDA_TIMEOUT_DEFAULT = int(os.environ.get("GENERATED_LAMBDA_TIMEOUT_DEFAULT", "30"))
# Wiring defaults (META field -> detected_* -> these).
DEFAULT_CONTAINER_PORT = int(os.environ.get("DEFAULT_CONTAINER_PORT", "8080"))
DEFAULT_HEALTH_CHECK_PATH = os.environ.get("DEFAULT_HEALTH_CHECK_PATH", "/health")
# ManagedBy tag on generated IaC — matches the committed module templates' tags.
IAC_MANAGED_BY_TAG = os.environ.get("IAC_MANAGED_BY_TAG", "amf-terraform")

ddb = boto3.resource("dynamodb", region_name=REGION)
ecs = boto3.client("ecs", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
codecommit = boto3.client("codecommit", region_name=REGION)
table = ddb.Table(TABLE)

registry = ToolRegistry("execution")


def _fail_task(token, error, cause):
    """Signal the Step Functions callback that the ATX task failed."""
    try:
        sfn.send_task_failure(taskToken=token, error=error[:256], cause=cause[:32000])
    except Exception as e:
        logger.error("send_task_failure failed: %s", e)


def _succeed_task(token, payload):
    """Resume a waitForTaskToken task that has no work to launch."""
    if not token:
        return
    try:
        sfn.send_task_success(taskToken=token, output=json.dumps(payload))
    except Exception as e:
        logger.error("send_task_success failed: %s", e)


def _derive_default_steps(meta):
    """Fallback transform steps derived from the assessed stack.

    transformation_steps is normally seeded from the app's units.md. If it is
    absent we derive one conservative version-upgrade step from the detected
    language so LaunchTransformation still launches real work rather than
    no-opping into a 4h waitForTaskToken hang. Returns the same 4-tuple shape
    that _normalize() produces: (td_name, additional_context, target_version,
    build_command).
    """
    lang = (meta.get("language") or "").lower()
    tgt = meta.get("target_version") or ""
    if lang == "java":
        return [("AWS/java-version-upgrade", "", tgt or JAVA_UPGRADE_TARGET_DEFAULT, "")]
    if lang == "python":
        return [("AWS/python-version-upgrade", "", tgt or PYTHON_UPGRADE_TARGET_DEFAULT, "")]
    if lang in ("c#", "csharp", "dotnet", ".net", "asp.net"):
        return [("AWS/dotnet-version-upgrade", "", tgt or DOTNET_UPGRADE_TARGET_DEFAULT, "")]
    return []


def handler(event, ctx=None):
    # The SFN waitForTaskToken callback passes a task_token alongside the tool
    # arguments. Thread it into the args so the launcher tools can pick it up.
    task_token = event.get("task_token", "")
    if task_token:
        args = event.setdefault("arguments", event if "arguments" not in event else event["arguments"])
        if isinstance(args, dict):
            args.setdefault("_task_token", task_token)
    return registry.dispatch(event, ctx)


@registry.tool(
    "start_code_analysis",
    description="Launch an ATX code-analysis ECS task for an app (async, SFN callback).",
    schema={"app_id": {"type": "string", "required": True}, "td_name": {"type": "string"}},
    infer=lambda a: "app_id" in a and "job_id" not in a and not a.get("td_name") and "transform" not in json.dumps(a).lower(),
)
def start_code_analysis(args):
    app_id = args["app_id"]
    td_name = args.get("td_name", ATX_DEFAULT_TD)
    job_id = f"analysis-{app_id}-{uuid.uuid4().hex[:8]}"
    return _launch_ecs_task(job_id, app_id, "code-analysis", td_name, task_token=args.get("_task_token", ""))


@registry.tool(
    "run_terraform_plan",
    description="Launch an ECS worker (MODE=infra) that runs `terraform validate` + `plan` on the "
                "generated IaC staged under execution/infrastructure/, capturing the output back to S3. "
                "Plan runs with the worker's own account credentials (nothing is applied). Async, SFN callback.",
    schema={"app_id": {"type": "string", "required": True}},
    infer=lambda a: "app_id" in a and str(a.get("_action", "")) == "run_terraform_plan",
)
def run_terraform_plan(args):
    app_id = args["app_id"]
    job_id = f"infra-{app_id}-{uuid.uuid4().hex[:8]}"
    return _launch_ecs_task(job_id, app_id, "infra", "", task_token=args.get("_task_token", ""))


@registry.tool(
    "run_ci_fix",
    description="Launch an ECS worker (MODE=ci-fix) that brings the migration branch to CI-readiness: "
                "dependency manifest, pytest config, runtime-only multi-stage Dockerfile, safe lint fixes and "
                "scaffolded smoke tests (build/test files only, never app logic), re-checks, commits and pushes. "
                "Async, SFN callback; advisory — its result rides on the execution gate.",
    schema={"app_id": {"type": "string", "required": True}},
    infer=lambda a: "app_id" in a and str(a.get("_action", "")) == "run_ci_fix",
)
def run_ci_fix(args):
    app_id = args["app_id"]
    job_id = f"cifix-{app_id}-{uuid.uuid4().hex[:8]}"
    return _launch_ecs_task(job_id, app_id, "ci-fix", "", task_token=args.get("_task_token", ""))


# Deploy is OFF by default (Terraform enable_deploy): Execution ends at a reviewed migration branch and
# nothing is applied. Only "true" turns it on.
DEPLOY_ENABLED = os.environ.get("DEPLOY_ENABLED", "false").strip().lower() == "true"
DEPLOY_DISABLED_MESSAGE = ("Deploy is disabled (enable_deploy = false): AMF generated, validated and committed the IaC "
                           "and CI/CD to the migration branch but applies nothing. Apply it yourself, or redeploy AMF "
                           "with ENABLE_DEPLOY=true.")
# Accounts AMF may apply into (comma-separated; default: the account it runs in).
DEPLOY_ALLOWED_ACCOUNTS = [a.strip() for a in os.environ.get("DEPLOY_ALLOWED_ACCOUNTS", "").split(",") if a.strip()]
DEPLOY_ROLE_ARN = os.environ.get("DEPLOY_ROLE_ARN", "")
# Role the ephemeral in-VPC validation probe Lambda runs as (validation.tf).
VALIDATION_PROBE_ROLE_ARN = os.environ.get("VALIDATION_PROBE_ROLE_ARN", "")


_ACCOUNT_ID: list = []


def _account_id() -> str:
    """The account this Lambda runs in (cached; '' when STS is unreachable)."""
    if not _ACCOUNT_ID:
        try:
            _ACCOUNT_ID.append(boto3.client("sts", region_name=REGION).get_caller_identity()["Account"])
        except Exception as e:  # noqa: BLE001
            logger.warning("get_caller_identity failed: %s", e)
            return ""
    return _ACCOUNT_ID[0]


def _deploy_guard(meta: dict) -> str:
    """'' when AMF may apply this app's IaC, else the reason it may not."""
    acct = str(meta.get("target_account") or "")
    allowed = DEPLOY_ALLOWED_ACCOUNTS or [_account_id()]
    if acct not in allowed:
        return f"target_account {acct or '(unset)'} is not in the deploy allowlist ({', '.join(allowed)})."
    if not DEPLOY_ROLE_ARN:
        return "DEPLOY_ROLE_ARN is not configured (deploy.tf)."
    return ""


@registry.tool(
    "deploy_infrastructure",
    description="Deploy gate action: launch the ECS worker (MODE=infra, INFRA_ACTION=apply) "
                "to terraform-apply the IaC committed on the migration branch AT the human-approved commit "
                "(META deploy_approved_commit; refuses a moved branch), re-check the exposure policy on the plan, "
                "then wait for the app's first CodePipeline run on that commit. Async, SFN callback.",
    schema={"app_id": {"type": "string", "required": True}},
    infer=lambda a: "app_id" in a and str(a.get("_action", "")) == "deploy_infrastructure",
)
def deploy_infrastructure(args):
    app_id = args["app_id"]
    if not DEPLOY_ENABLED:
        # Resume the Deploy task at once: Check_Deploy routes "skipped" to Validation (static checks only).
        _set_exec_status(app_id, "exec_deploy_status", "skipped")
        out = {"status": "skipped", "app_id": app_id, "message": DEPLOY_DISABLED_MESSAGE}
        _succeed_task(args.get("_task_token", ""), out)
        return out
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    reason = _deploy_guard(meta)
    commit = str(meta.get("deploy_approved_commit") or "")
    if not reason and not commit:
        reason = "no approved deploy commit (deploy_approved_commit) — approve the execution gate with the reviewed commit."
    if reason:
        _set_exec_status(app_id, "exec_deploy_status", "error")
        _set_exec_status(app_id, "exec_deploy_error", reason)
        return {"status": "error", "app_id": app_id, "message": reason}
    job_id = f"deploy-{app_id}-{uuid.uuid4().hex[:8]}"
    _set_exec_status(app_id, "exec_deploy_status", "running")
    return _launch_ecs_task(job_id, app_id, "infra", "", task_token=args.get("_task_token", ""),
                            extra_env={"INFRA_ACTION": "apply", "DEPLOY_COMMIT": commit,
                                       "DEPLOY_ROLE_ARN": DEPLOY_ROLE_ARN})


@registry.tool(
    "destroy_infrastructure",
    description="Tear down an app's deployed stack (terraform destroy via the ECS worker, INFRA_ACTION=destroy). "
                "Explicit and separate from the workflow: `confirm` must equal the app_id.",
    schema={"app_id": {"type": "string", "required": True},
            "confirm": {"type": "string", "required": True, "description": "Must equal app_id"}},
    infer=lambda a: "app_id" in a and str(a.get("_action", "")) == "destroy_infrastructure",
)
def destroy_infrastructure(args):
    app_id = args["app_id"]
    if str(args.get("confirm") or "") != app_id:
        return {"status": "error", "app_id": app_id, "message": "confirm must equal the app_id to destroy its stack."}
    if not DEPLOY_ENABLED:
        return {"status": "error", "app_id": app_id, "message": DEPLOY_DISABLED_MESSAGE}
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    reason = _deploy_guard(meta)
    if reason:
        return {"status": "error", "app_id": app_id, "message": reason}
    job_id = f"destroy-{app_id}-{uuid.uuid4().hex[:8]}"
    _set_exec_status(app_id, "exec_deploy_status", "destroying")
    return _launch_ecs_task(job_id, app_id, "infra", "",
                            extra_env={"INFRA_ACTION": "destroy", "DEPLOY_ROLE_ARN": DEPLOY_ROLE_ARN})


def _running_transformation(app_id):
    """The job id of a code transformation whose ECS task is still alive, else ''."""
    plans = table.query(
        KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("PLAN#transform-"),
    ).get("Items", [])
    # start_code_transformation links the launched task as PLAN.task_arns (a list).
    live = {arn: p["plan_id"] for p in plans if p.get("status") == "running"
            for arn in (p.get("task_arns") or [])}
    if not live or not ECS_CLUSTER:
        return ""
    try:
        tasks = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=list(live)[:100]).get("tasks", [])
    except Exception as exc:  # noqa: BLE001 — can't verify → don't block
        logger.warning("describe_tasks failed for %s: %s", app_id, exc)
        return ""
    return next((live[t["taskArn"]] for t in tasks if t.get("lastStatus") != "STOPPED"), "")


@registry.tool(
    "start_code_transformation",
    description="Launch an ATX code-transformation ECS task applying selected transforms across repos.",
    schema={
        "app_id": {"type": "string", "required": True},
        "td_name": {"type": "string", "description": "Optional single transform to run"},
        "target_version": {"type": "string"},
        "build_context": {"type": "string"},
    },
)
def start_code_transformation(args):
    app_id = args["app_id"]
    # One transformation job per app at a time: steps run SEQUENTIALLY inside one
    # job, all pushing to the app's migration branch. APP-TE 2026-10-14: the agent
    # launched each step as its own job, in parallel, on the same branch. The
    # workflow path (_task_token) is already serialised by Step Functions.
    if not args.get("_task_token") and (running := _running_transformation(app_id)):
        return {"status": "rejected", "app_id": app_id, "running_job_id": running,
                "message": f"A code transformation is already running for {app_id} (job {running}). "
                           "Wait for it to finish — all approved steps run in order within one job; "
                           "call this tool once, without td_name, to run them all."}
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    explicit_td = args.get("td_name", "")

    # An empty list is the reviewer's decision at the assessment gate ("no code
    # transformation" — e.g. ATX already ran outside this workflow): skip, don't
    # derive a default. The new-run reset REMOVES the field, so present-but-empty
    # can only come from that gate; an absent field still takes the fallback below.
    if not explicit_td and meta.get("transformation_steps") == []:
        result = {"status": "skipped", "app_id": app_id,
                  "message": "No transformation steps were approved at the assessment gate; transformation skipped."}
        _succeed_task(args.get("_task_token", ""), result)
        logger.info("app %s: transformation skipped (none approved)", app_id)
        return result

    # transformation_steps accepts two shapes, so a spec-driven run and a
    # catalogue-driven run use the same path:
    #
    #   ["AWS/java-version-upgrade", ...]                       (legacy, bare names)
    #   [{"td_name": ..., "additional_context": ...}, ...]       (unit-derived)
    #
    # The second shape lets an AIDLC units.md carry a DIFFERENT instruction per
    # unit through to the ATX CLI's additionalPlanContext. The `build_context`
    # argument cannot express that: it is one value applied to every step.
    # Entries are normalised to (td_name, unit_context) pairs; a bare string
    # simply has no unit context and falls back to the derived default below.
    def _normalize(entry):
        if isinstance(entry, dict):
            return (
                entry.get("td_name", "") or entry.get("name", ""),
                entry.get("additional_context", "") or "",
                entry.get("target_version", "") or "",
                # Optional: a unit may name the command ATX should run to verify
                # its changes. Left empty, the worker detects it from the repo.
                entry.get("build_command", "") or "",
            )
        return (str(entry), "", "", "")

    if explicit_td:
        td_entries = [(explicit_td, "", "", "")]
    else:
        td_entries = _assessment_order(app_id, [
            e for e in (_normalize(x) for x in (meta.get("transformation_steps", []) or []))
            if e[0]
        ])
        # The security fixer remediates what the upgrades leave behind, so it always runs last.
        td_entries = sorted(td_entries, key=lambda e: e[0] == SECURITY_FIXER_TD)

    repos_resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("REPO#")
    )
    repos = []
    for r in repos_resp.get("Items", []):
        if not r.get("url"):
            continue
        branch = r.get("branch") or "main"
        source_ref = r.get("source_ref", "")
        if not _REVIEW_BRANCH_RE.fullmatch(branch):
            # The record holds the repo's default branch: transform on the shared
            # migration branch (same one IaC/CI-CD commits use), created by the
            # worker from the default branch when it does not exist yet.
            source_ref = source_ref or f"origin/{branch}"
            branch = migration_branch(app_id)
        repos.append({
            "url": r["url"], "branch": branch,
            # Explicit source for a target branch that does not exist yet. The
            # worker will create the branch from this ref; absent this field, a
            # missing branch remains an error and no fallback is attempted.
            "source_ref": source_ref,
            "review_base_branch": r.get("review_base_branch", ""),
            "name": r["url"].rstrip("/").split("/")[-1].replace(".git", ""),
        })
    if not repos:
        # waitForTaskToken: a bare return never resumes the state machine, so an
        # unlinked repo would hang LaunchTransformation until its 4h timeout.
        # Fail the callback immediately so the run surfaces the error in seconds.
        _fail_task(args.get("_task_token", ""), "NoRepos",
                   "No repositories linked to this app; cannot transform.")
        return {"error": "No repositories linked; cannot transform.", "app_id": app_id, "status": "no_repos"}
    if not td_entries:
        # transformation_steps is normally seeded from the app's units.md. If it
        # is missing, derive one conservative step from the assessed stack rather
        # than no-opping into a 4h waitForTaskToken hang. If even that is not
        # possible, fail the callback fast instead of hanging.
        td_entries = _derive_default_steps(meta)
        if not td_entries:
            _fail_task(args.get("_task_token", ""), "NoTransforms",
                       "No transformation_steps configured and no default derivable from the assessed stack.")
            return {"error": "No transformation_steps selected for this app; nothing to transform.",
                    "app_id": app_id, "status": "no_transforms"}
        logger.warning("app %s had no transformation_steps; derived default from stack: %s", app_id, td_entries)

    # Reject transforms written for another language (e.g. python-version-upgrade
    # on a Java app): ATX would otherwise run them against whatever stray files
    # match and the plan would report an upgrade that was never asked for.
    app_lang = _norm_lang(meta)
    skipped = []
    if app_lang:
        # Polyglot repos: a TD the assessment recommended from ATX evidence targets
        # code that IS in the repo (APP-CLI: Java monolith + a Node.js 14 Lambda →
        # AWS/nodejs-version-upgrade) even though META.language is the primary one.
        evidenced = _assessment_recommended_tds(app_id)
        kept = []
        for entry in td_entries:
            td_lang = _td_language(entry[0])
            if td_lang and td_lang != app_lang and entry[0] not in evidenced:
                skipped.append({"td_name": entry[0], "reason": f"{entry[0]} targets {td_lang}; app language is {app_lang}"})
                continue
            kept.append(entry)
        td_entries = kept
    if skipped:
        logger.warning("app %s: skipped language-mismatched transforms: %s", app_id, skipped)
    if not td_entries:
        cause = "All transformation_steps target another language: " + "; ".join(s["reason"] for s in skipped)
        _fail_task(args.get("_task_token", ""), "NoTransforms", cause)
        return {"error": cause, "app_id": app_id, "status": "no_transforms", "skipped_steps": skipped}

    # ATX transforms run non-interactively and REQUIRE an additionalPlanContext
    # (target version/goal). Precedence, most specific first:
    #   1. an explicit argument on this call
    #   2. the unit's own instruction from units.md
    #   3. a derived default from the TD NAME first; the app language is only a
    #      fallback for language-neutral TDs (it used to win, so a Java app's
    #      python-version-upgrade step was told "upgrade to Java 21")
    def _plan_context(td: str, unit_context: str = "", unit_target: str = "") -> str:
        if td == SECURITY_FIXER_TD:
            # Findings come from the operator (AMF runs no scanner); see the README.
            return unit_context or SECURITY_FIXER_CONTEXT
        explicit = args.get("build_context") or args.get("additional_context")
        if explicit:
            return explicit
        if unit_context:
            return unit_context
        tgt = unit_target or meta.get("target_version") or args.get("target_version", "")
        lang = _td_language(td) or app_lang
        low = td.lower()
        if lang == "java":
            if "sdk" in low:
                return ("Migrate all AWS SDK for Java v1 (com.amazonaws) usage to AWS SDK for Java v2 "
                        "(software.amazon.awssdk); keep the project's current Java version.")
            # Not version upgrades — "upgrade to Java 21" was sent for these too.
            if "graviton" in low or "arm64" in low:
                return ("Make the application run on AWS Graviton (ARM64): replace x86-only native libraries and "
                        "dependencies with ARM64-compatible versions and use ARM64-compatible base images; keep "
                        "the project's current Java version.")
            if "performance" in low:
                return ("Optimize the application's runtime performance (JVM settings, hot paths, resource usage) "
                        "without changing behaviour; keep the project's current Java version.")
            return f"The target Java version to upgrade to is Java {tgt or '21'}."
        if lang == "python":
            return f"The target Python version to upgrade to is Python {tgt or '3.12'}."
        if lang == "dotnet":
            return f"The target .NET version to upgrade to is .NET {tgt or '8'}."
        if lang == "javascript":
            return f"The target Node.js version to upgrade to is Node.js {tgt or '20'}."
        return f"Modernize this application for AWS. Target: {tgt or 'latest supported runtime'}."

    steps, n = [], 1
    for td, unit_context, unit_target, unit_build in td_entries:
        for repo in repos:
            steps.append({
                "step_number": n, "td_name": td, "repo_url": repo["url"],
                "repo_branch": repo["branch"], "repo_source_ref": repo["source_ref"],
                "repo_review_base_branch": repo["review_base_branch"],
                "repo_name": repo["name"],
                # Key MUST match what the workbench worker reads (additional_context).
                "additional_context": _plan_context(td, unit_context, unit_target),
                # Record whether this step was driven by a checked-in spec or by
                # a derived default, so the audit trail can tell them apart.
                "context_source": "unit" if unit_context else "derived",
                # Empty means "detect from the repository" in the worker, not
                # "skip verification" -- the worker warns loudly if it finds no
                # build tool, because an unverified transform must not look
                # identical to a verified one.
                "build_command": unit_build,
                # Seed a status so the UI shows per-step progress; the worker
                # (worker/dynamo.update_plan_step_status) flips this to
                # running/completed/failed as it processes each step.
                "status": "pending", "message": "",
            })
            n += 1

    job_id = f"transform-{app_id}-{uuid.uuid4().hex[:8]}"
    # Create the PLAN# record BEFORE launch. The worker keys per-step status
    # writes off APP#<app_id>/PLAN#<PLAN_ID> where PLAN_ID == job_id; without
    # this record every step write silently no-ops and the UI shows no progress.
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    table.put_item(Item={
        "PK": f"APP#{app_id}", "SK": f"PLAN#{job_id}",
        "GSI1PK": "TYPE#PLAN", "GSI1SK": job_id,
        "plan_id": job_id, "app_id": app_id, "app_name": meta.get("app_name", app_id),
        "description": f"Code transformation ({len(steps)} step(s))",
        "steps": steps, "status": "running", "created_at": now, "updated_at": now,
        "skipped_steps": skipped, "branch": steps[0]["repo_branch"] if steps else "",
    })

    extra_env = {"PLAN_ID": job_id, "WORKBENCH_STEPS": json.dumps(steps)}
    result = _launch_ecs_task(
        job_id, app_id, "workbench", explicit_td or "AWS/multi",
        target_version=args.get("target_version", ""), task_token=args.get("_task_token", ""), extra_env=extra_env,
    )
    # Link the launched ECS task ARN onto the plan so the UI can join task→plan.
    if result.get("task_arn"):
        try:
            table.update_item(
                Key={"PK": f"APP#{app_id}", "SK": f"PLAN#{job_id}"},
                UpdateExpression="SET task_arns = :t",
                ExpressionAttributeValues={":t": [result["task_arn"]]},
            )
        except Exception as e:
            logger.warning("Failed to link task_arn to PLAN#%s: %s", job_id, e)
    if skipped:
        result = {**result, "skipped_steps": skipped}
    return result


def _assessment_recommended_tds(app_id: str) -> set:
    """td_names in the latest assessment-summary's transformations.recommended."""
    try:
        items = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("ANALYSIS#assessment-summary#"),
            ScanIndexForward=False, Limit=1,
        ).get("Items", [])
    except Exception as e:  # noqa: BLE001 — absence just means "no evidence"
        logger.warning("assessment-summary read failed for %s: %s", app_id, e)
        return set()
    rec = ((items[0].get("result") or {}).get("transformations") or {}).get("recommended") if items else None
    return {str(r.get("td_name")) for r in rec or [] if isinstance(r, dict) and r.get("td_name")}


# ATX's security fixer only remediates findings it is given (AMF ships no scanner; supply them
# from Amazon Inspector or your own scanner). When selected it runs last (mirror: core/strategy_options.py).
SECURITY_FIXER_TD = "AWS/security-issue-fixer"
SECURITY_FIXER_CONTEXT = (
    "Remediate ONLY the security findings in the scanner results supplied with this run (vulnerable "
    "dependencies and exposed secrets). For a vulnerable dependency, upgrade to the nearest fixed version — "
    "for a BOM-managed library (e.g. Spring Boot) override the managed version property rather than adding "
    "a conflicting explicit version. Keep the current Java version, framework major versions and behaviour; "
    "do not change unrelated code. The build must still pass."
)


def _spring_profile_env(meta: dict, target_db: str) -> str:
    """`, SPRING_PROFILES_ACTIVE = "<p>"` when the repo ships a profile for the target engine.

    Apps such as PetClinic choose their schema/data scripts from the active profile
    (application-mysql.properties sets database=mysql); the datasource URL alone would run the
    default (HSQLDB) scripts against MySQL. The repo scan records detected_spring_profiles.
    """
    engine = "postgres" if "postgres" in target_db else "mysql" if "mysql" in target_db else ""
    profiles = [str(p) for p in (meta.get("detected_spring_profiles") or [])]
    match = next((p for p in profiles if engine and engine in p.lower()), "")
    return f', SPRING_PROFILES_ACTIVE = "{_shell_safe(match)}"' if match else ""


def _td_language(td: str) -> str:
    """Language a transformation definition targets, from its NAME ('' = neutral)."""
    low = (td or "").lower()
    if "java" in low and "javascript" not in low:
        return "java"
    if "python" in low:
        return "python"
    if "dotnet" in low or ".net" in low or "csharp" in low:
        return "dotnet"
    if re.search(r"node|javascript|typescript", low):
        return "javascript"
    return ""


_REVIEW_BRANCH_RE = re.compile(r"^(?:demo|migration)/[A-Za-z0-9._/-]+$")


@registry.tool(
    "get_job_status",
    description="Poll an ATX job's status; reconciles against the ECS task when running.",
    schema={"job_id": {"type": "string", "required": True}},
    infer=lambda a: "job_id" in a and "app_id" not in a,
)
def get_job_status(args):
    job_id = args["job_id"]
    item = table.get_item(Key={"PK": f"JOB#{job_id}", "SK": "META"}).get("Item")
    if not item:
        return {"error": f"Job {job_id} not found"}
    result = {k: v for k, v in item.items() if k not in ("PK", "SK", "GSI1PK", "GSI1SK")}

    if result.get("status") == "RUNNING" and result.get("task_arn") and ECS_CLUSTER:
        try:
            tasks = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=[result["task_arn"]])
            if tasks.get("tasks"):
                if tasks["tasks"][0].get("lastStatus") == "STOPPED":
                    exit_code = tasks["tasks"][0].get("containers", [{}])[0].get("exitCode", -1)
                    new_status = "COMPLETED" if exit_code == 0 else "FAILED"
                    table.update_item(
                        Key={"PK": f"JOB#{job_id}", "SK": "META"},
                        UpdateExpression="SET #s = :s",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":s": new_status},
                    )
                    result["status"] = new_status
        except Exception as _err:
            logger.debug("best-effort step ignored: %s", _err)
    return json_safe(result)


# ── Generate + stage: Terraform IaC and CI/CD (render only; no apply, no git) ──

def _tpl(key: str) -> str:
    """Read a template file from the data bucket's template library."""
    return s3.get_object(Bucket=BUCKET, Key=f"{TEMPLATES_PREFIX}/{key}")["Body"].read().decode("utf-8")


def _stage(app_id: str, kind: str, rel: str, text: str) -> str:
    """Write a generated artifact into the app's execution/{kind}/ staging area."""
    key = f"applications/{app_id}/execution/{kind}/{rel}"
    s3.put_object(Bucket=BUCKET, Key=key, Body=text.encode("utf-8"))
    return rel


def _render(text: str, variables: dict) -> str:
    """Fill both ${var} (Terraform tfvars) and {{var}} (CI/CD) placeholders."""
    for k, v in variables.items():
        text = text.replace("${%s}" % k, str(v)).replace("{{%s}}" % k, str(v))
    return text


def _record_execution(app_id: str, kind: str, s3_prefix: str, files: list, extra: dict | None = None):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    item = {
        "PK": f"APP#{app_id}", "SK": f"EXECUTION#{kind}#{now}",
        "GSI1PK": "TYPE#EXECUTION", "GSI1SK": f"{app_id}#{kind}",
        "app_id": app_id, "kind": kind, "status": "staged",
        "s3_prefix": s3_prefix, "files": files, "created_at": now,
    }
    if extra:
        item.update(extra)
    table.put_item(Item=item)


# ── Shared helpers: META parsing, linked repo, migration branch ────────────────

# Strategies whose only valid compute is EC2 (see core/strategy_options.py; retain
# is a close-out and never reaches Execution).
_LIFT_STRATEGIES = ("rehost", "relocate")
# target_compute -> standardized Terraform module under templates/terraform/modules/.
# Exactly the computes the strategy options offer — EKS has no module, so it is not
# selectable (core/strategy_options.py).
_COMPUTE_MODULES = {"ecs_fargate": "ecs-fargate", "lambda": "lambda", "ec2": "ec2"}
# Mirror of core/strategy_options.compute_options_for for the strategies Execution runs.
# Scope: EC2 is the lift-and-shift target only.
_COMPUTE_FOR_STRATEGY = {"rehost": ("ec2",), "relocate": ("ec2",),
                         "replatform": ("ecs_fargate", "lambda"), "refactor": ("ecs_fargate", "lambda")}
# compute module -> deploy_target of the codepipeline module.
_DEPLOY_TARGETS = {"ecs-fargate": "ecs", "ec2": "ec2", "lambda": "lambda"}
# target_database values provisioned by the `rds` module (RDS instance or Aurora).
_RDS_ENGINES = ("rds_postgresql", "rds_mysql", "aurora_postgresql", "aurora_mysql")
_CONTAINER_COMPUTE = ("ecs_fargate",)
_DEFAULT_BRANCH_NAMES = {"main", "master", "primary", "develop", "development", "trunk"}
# One naming convention for every write AMF makes to an app repo (transformation
# pushes, IaC + CI/CD commits). MUST match backend/src/worker/branches.py
# (pinned by backend/tests/unit/test_journey_exec_transform.py).
MIGRATION_BRANCH_PREFIX = "migration/"
# worker terraform_runner._RUN_OUTPUTS — uploaded beside the staged IaC, never committed.
_WORKER_RUN_OUTPUTS = {"validate.txt", "plan.txt", "terraform-run.log", "exposure.json"}


def _resolve_compute(meta: dict) -> tuple[str, str]:
    """(target_compute, error) — the compute the user approved, never a guess.

    Lift strategies have exactly one valid compute (ec2), so an unset value resolves
    to it; anything else must be set, valid for the strategy, and backed by a module.
    """
    strategy = (meta.get("migration_strategy") or "").strip().lower()
    tc = (meta.get("target_compute") or "").strip().lower()
    allowed = _COMPUTE_FOR_STRATEGY.get(strategy)
    if not tc:
        if allowed and len(allowed) == 1:
            return allowed[0], ""
        return "", "target_compute is not set — approve the assessment gate with a target compute first."
    if allowed and tc not in allowed:
        return "", (f"target_compute='{tc}' is not valid for strategy='{strategy}' "
                    f"(allowed: {', '.join(allowed)}).")
    if tc not in _COMPUTE_MODULES:
        return "", (f"target_compute='{tc}' has no standardized Terraform module "
                    f"(supported: {', '.join(sorted(_COMPUTE_MODULES))}).")
    return tc, ""


def migration_branch(app_id: str) -> str:
    """The branch AMF commits generated code/IaC/CI-CD to: `migration/<app_id lower>`."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", (app_id or "").lower()).strip("-.") or "app"
    return f"{MIGRATION_BRANCH_PREFIX}{slug}"


def _norm_lang(meta: dict) -> str:
    """Canonical app language: java | python | javascript | dotnet | <other lowercased>."""
    lang = str(meta.get("language") or "").strip().lower()
    if lang in ("node", "nodejs", "node.js", "typescript", "js", "javascript"):
        return "javascript"
    if lang in ("c#", "csharp", ".net", "dotnet", "asp.net"):
        return "dotnet"
    return lang


def _as_dict(v) -> dict:
    """META sub-objects may arrive as a dict or a JSON string (L2 import); never raise."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _truthy(v) -> bool | None:
    """yes/true/1 -> True, no/false/0 -> False, anything else -> None (unknown)."""
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in ("yes", "true", "1", "y", "on", "enabled"):
        return True
    if s in ("no", "false", "0", "n", "off", "disabled"):
        return False
    return None


def _num(v, default, lo=None, hi=None):
    """Coerce a META number (Decimal/str/int) to int, clamped; default when unusable."""
    try:
        n = int(float(str(v).strip()))
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _sla_pct(v) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return float(m.group(0)) if m else None


def _target_region(meta: dict) -> str:
    """META target_region (L2 field, may be absent) -> first target_regions -> Lambda region."""
    region = meta.get("target_region")
    if not region:
        regions = meta.get("target_regions")
        if isinstance(regions, (list, tuple)) and regions:
            region = regions[0]
    region = str(region or "").strip()
    return region if re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region) else REGION


def _environment(meta: dict) -> str:
    env = re.sub(r"[^A-Za-z0-9_-]", "", str(meta.get("environment") or meta.get("target_environment") or ""))
    return env or "prod"


def _az_count(meta: dict) -> int:
    """AZ count for the VPC: META dr_availability.availability_zones / az_count, default 2."""
    dr = _as_dict(meta.get("dr_availability"))
    for v in (dr.get("az_count"), dr.get("availability_zones"), meta.get("az_count"), meta.get("availability_zones")):
        if v in (None, "", []):
            continue
        if isinstance(v, (list, tuple)):
            return _num(len(v), 2, 1, 6)
        n = _num(v, 0)
        if n > 0:
            return min(n, 6)
    return 2


def _db_multi_az(meta: dict) -> tuple[bool, str]:
    """Multi-AZ for the managed database, with the reason (rendered into the README).

    Precedence: an explicit flag (META dr_availability.multi_az, META multi_az, or
    database.multi_az) -> availability SLA >= 99.95% (single-AZ RDS cannot meet
    it) -> a multi-region DR posture (active_passive/active_active implies a
    production-critical tier) -> single-AZ.
    """
    dr = _as_dict(meta.get("dr_availability"))
    db = _as_dict(meta.get("database"))
    # db_multi_az is the field the API/approval sets (APP-HE 2026-10-26: false was ignored
    # and the SLA rule built Multi-AZ).
    for src, v in (("db_multi_az", meta.get("db_multi_az")),
                   ("dr_availability.multi_az", dr.get("multi_az")), ("multi_az", meta.get("multi_az")),
                   ("database.multi_az", db.get("multi_az"))):
        flag = _truthy(v)
        if flag is not None:
            return flag, f"explicit {src}={v}"
    perf = _as_dict(meta.get("performance"))
    sla = _sla_pct(dr.get("uptime_sla") or perf.get("uptime_sla") or meta.get("uptime_sla"))
    if sla is not None:
        ok = sla >= MULTI_AZ_SLA_THRESHOLD
        return ok, f"uptime_sla {sla}% {'>=' if ok else '<'} {MULTI_AZ_SLA_THRESHOLD:g}%"
    mr = str(meta.get("multi_region") or dr.get("multi_region") or "").lower()
    if mr in ("active_passive", "active_active", "yes", "true"):
        return True, f"multi_region={mr}"
    return False, "no availability requirement recorded"


def _tags(meta: dict, app_id: str) -> dict:
    """Resource tags; empty values (e.g. CostCenter='') are omitted, not rendered blank."""
    tags = {
        "Application": app_id,
        "BusinessUnit": meta.get("business_unit", ""),
        "Tier": meta.get("tier", ""),
        "CostCenter": meta.get("cost_center", ""),
        "ManagedBy": IAC_MANAGED_BY_TAG,
    }
    return {k: str(v) for k, v in tags.items() if str(v or "").strip()}


def _repo_host(url: str) -> str:
    u = (url or "").lower()
    if "git-codecommit" in u or u.startswith("codecommit:"):
        return "codecommit"
    return "unknown"


def _linked_repo(app_id: str) -> dict:
    """The app's primary linked repo (CodeCommit preferred) with its host + default branch.

    Returns {} when no repo is linked. `default_branch` is the repo record's branch
    unless that is itself an AMF review/migration branch, in which case the
    CodeCommit default branch (get_repository) is used.
    """
    resp = table.query(KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("REPO#"))
    items = [r for r in resp.get("Items", []) if r.get("url")]
    if not items:
        return {}
    items.sort(key=lambda r: 0 if (_repo_host(r["url"]) == "codecommit" or str(r.get("provider", "")).lower() == "codecommit") else 1)
    r = items[0]
    url = r["url"]
    host = "codecommit" if str(r.get("provider", "")).lower() == "codecommit" else _repo_host(url)
    name = url.rstrip("/").split("/")[-1].replace(".git", "") or r.get("name", "")
    branch = str(r.get("branch") or "")
    default_branch = branch
    if not branch or _REVIEW_BRANCH_RE.fullmatch(branch):
        default_branch = ""
        if host == "codecommit":
            try:
                default_branch = codecommit.get_repository(repositoryName=name)["repositoryMetadata"].get("defaultBranch", "")
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning("get_repository(%s) failed: %s", name, e)
        default_branch = default_branch or "main"
    return {"url": url, "name": name, "host": host, "branch": branch,
            "default_branch": default_branch, "source_ref": r.get("source_ref", "")}


def _assessment_order(app_id: str, entries: list) -> list:
    """Selected steps in the order the Assessment recommended them (ATX lists them in dependency
    order, e.g. spring-boot-version-upgrade before java-version-upgrade). The gate is a checkbox
    list, so the saved order is arbitrary (APP-CLI 2026-10-27). Unlisted steps keep their order, last."""
    recommended = [str(x.get("td_name") or x.get("name") or "") for x in
                   ((_latest_assessment(app_id).get("transformations") or {}).get("recommended") or [])
                   if isinstance(x, dict)]
    rank = {name: i for i, name in enumerate(recommended) if name}
    ordered = sorted(enumerate(entries), key=lambda ie: (rank.get(ie[1][0], len(rank)), ie[0]))
    return [e for _, e in ordered]


def _latest_assessment(app_id: str) -> dict:
    """Most recent ANALYSIS#assessment-summary result (detected stack), or {}."""
    try:
        resp = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("ANALYSIS#assessment-summary"),
            ScanIndexForward=False, Limit=1,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("assessment lookup for %s failed: %s", app_id, e)
        return {}
    items = resp.get("Items", [])
    if not items:
        return {}
    res = items[0].get("result")
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except ValueError:
            res = {}
    return res if isinstance(res, dict) else {}


_JAVA_SUPPORTED = (8, 11, 17, 21, 25)


def _parse_major(text, lang: str) -> int | None:
    """'1.8 (Java 8 — EOL)' -> 8, 'Amazon Corretto 21' -> 21, 'JDK11' -> 11, '3.11' -> (python) 3.11."""
    s = str(text or "")
    if lang == "java":
        m = re.search(r"\b1\.([5-8])\b", s) or re.search(r"(?<![\d.])(\d{1,2})(?![\d])", s)
        if not m:
            return None
        n = int(m.group(1))
        return n if 5 <= n <= 30 else None
    m = re.search(r"\b(\d{1,2})(?:\.\d+)?\b", s)
    return int(m.group(1)) if m else None


def _detected_versions(assessment: dict, lang: str) -> list:
    """Language versions the assessment detected (stack / detected_stack / runtime_versions)."""
    found = []
    for key in ("stack", "detected_stack"):
        st = _as_dict(assessment.get(key))
        if not st:
            continue
        for k in ("language_version", "runtime", "container_runtime"):
            if st.get(k):
                found.append(st[k])
        rv = _as_dict(st.get("runtime_versions"))
        found.extend(rv.values())
    return found


def _runtime_versions(meta: dict, app_id: str) -> dict:
    """Language versions for CI images, with the source of each (for notes).

    Java precedence: a planned java-version-upgrade (the CI builds the TRANSFORMED
    code, so its target wins) -> META runtime.jdk_version -> META language_version
    -> the assessment's detected version (highest across modules) -> 17. Python /
    Node follow the same order (python-/nodejs-version-upgrade target, runtime.*,
    language_version, assessment) with fallbacks 3.12 / 20.
    """
    lang = _norm_lang(meta)
    runtime = _as_dict(meta.get("runtime"))
    strategy = (meta.get("migration_strategy") or "").lower()
    steps = [str(s.get("td_name", "") if isinstance(s, dict) else s).lower() for s in (meta.get("transformation_steps") or [])]
    assessment = None
    out = {}

    def _assessed():
        nonlocal assessment
        if assessment is None:
            assessment = _latest_assessment(app_id)
        return _detected_versions(assessment, lang)

    # Java
    java, src = None, ""
    if lang == "java" and strategy not in _LIFT_STRATEGIES and any("java-version-upgrade" in s for s in steps):
        java, src = _parse_major(meta.get("target_version") or JAVA_UPGRADE_TARGET_DEFAULT, "java"), "java-version-upgrade target"
    if not java:
        java, src = _parse_major(runtime.get("jdk_version") or runtime.get("java_version"), "java"), "META runtime.jdk_version"
    if not java and lang == "java":
        java, src = _parse_major(meta.get("language_version"), "java"), "META language_version"
    if not java and lang == "java":
        detected = [v for v in (_parse_major(x, "java") for x in _assessed()) if v]
        java, src = (max(detected), "assessment detected stack") if detected else (None, "")
    if not java:
        java, src = DEFAULT_JAVA_VERSION, "default"
    java = next((v for v in _JAVA_SUPPORTED if v >= java), _JAVA_SUPPORTED[-1])
    out["java_version"], out["java_version_source"] = str(java), src

    # Python / Node: keep minor versions (3.12), parse loosely.
    def _ver(keys, td_key, lang_name, fallback):
        if lang == lang_name and strategy not in _LIFT_STRATEGIES and any(td_key in s for s in steps) and meta.get("target_version"):
            return str(meta["target_version"]), f"{td_key} target"
        for k in keys:
            if runtime.get(k):
                return str(runtime[k]), f"META runtime.{k}"
        if lang == lang_name and meta.get("language_version"):
            return str(meta["language_version"]), "META language_version"
        if lang == lang_name:
            for x in _assessed():
                m = re.search(r"\d+(?:\.\d+)?", str(x))
                if m:
                    return m.group(0), "assessment detected stack"
        return fallback, "default"

    out["python_version"], out["python_version_source"] = _ver(("python_version",), "python-version-upgrade", "python", DEFAULT_PYTHON_VERSION)
    node, node_src = _ver(("node_version", "nodejs_version"), "nodejs-version-upgrade", "javascript", DEFAULT_NODE_VERSION)
    out["node_version"], out["node_version_source"] = node.split(".")[0], node_src
    return out


def _build_module_facts(repo: dict, build_dir: str, branch: str) -> dict:
    """{build_tool, java_version, has_dockerfile} read from the build module in
    CodeCommit (best-effort; {} when unreadable). The repo is the truth for the
    build tool — the assessment recorded Maven for a Gradle project (APP-CLI)."""
    if repo.get("host") != "codecommit" or not repo.get("name"):
        return {}
    folder = "/" if build_dir in ("", ".") else f"/{build_dir}"
    try:
        f = codecommit.get_folder(repositoryName=repo["name"], folderPath=folder, commitSpecifier=branch)
    except Exception as e:  # noqa: BLE001 — advisory
        logger.warning("build module read failed for %s:%s: %s", repo.get("name"), folder, e)
        return {}
    names = {x.get("relativePath") for x in f.get("files", [])}
    facts = {"has_dockerfile": "Dockerfile" in names}
    build_file = next((n for n in ("build.gradle.kts", "build.gradle", "pom.xml") if n in names), "")
    facts["build_tool"] = "maven" if build_file == "pom.xml" else ("gradle" if build_file else "")
    if build_file:
        try:
            path = build_file if folder == "/" else f"{build_dir}/{build_file}"
            text = codecommit.get_file(repositoryName=repo["name"], filePath=path,
                                       commitSpecifier=branch)["fileContent"].decode("utf-8", "replace")
            m = (re.search(r"languageVersion\s*(?:=|\.set\()\s*JavaLanguageVersion\.of\((\d+)\)", text)
                 or re.search(r"(?:source|target)Compatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+)", text)
                 or re.search(r"<(?:maven\.compiler\.release|maven\.compiler\.source|java\.version|release)>\s*(\d+)", text))
            if m:
                facts["java_version"] = m.group(1)
        except Exception as e:  # noqa: BLE001
            logger.warning("build file read failed for %s: %s", repo.get("name"), e)
    return facts


def _cpu_architecture(meta: dict) -> str:
    """ARM64 when META says so or a Graviton transformation is planned, else X86_64."""
    explicit = str(meta.get("cpu_architecture") or "").strip().upper()
    if explicit in ("ARM64", "X86_64"):
        return explicit
    steps = [str(t.get("td_name", "") if isinstance(t, dict) else t).lower() for t in (meta.get("transformation_steps") or [])]
    return "ARM64" if any("graviton" in t or "arm64" in t for t in steps) else "X86_64"


def _detect_build_dir(repo: dict, lang: str, branch: str) -> tuple[str, str]:
    """Build root for CI. Repo root when it has the build file; else the single
    shallowest directory that does (CodeCommit only, best-effort). Returns (dir, note)."""
    build_files = {"java": ("pom.xml", "build.gradle", "build.gradle.kts"), "python": ("requirements.txt",),
                   "javascript": ("package.json",)}.get(lang, ())
    build_file = "/".join(build_files)
    if not build_files or repo.get("host") != "codecommit":
        return ".", ""
    try:
        frontier, depth = ["/"], 0
        while frontier and depth < 3:
            hits, nxt = [], []
            for folder in frontier[:15]:
                f = codecommit.get_folder(repositoryName=repo["name"], folderPath=folder, commitSpecifier=branch)
                if any(x.get("relativePath") in build_files for x in f.get("files", [])):
                    hits.append(folder.strip("/") or ".")
                nxt += [s["absolutePath"] for s in f.get("subFolders", []) if not s["relativePath"].startswith(".")]
            if hits:
                if len(hits) == 1:
                    return hits[0], ("" if hits[0] == "." else f"No {build_file} at repo root; CI builds from {hits[0]}/ (override with META build_dir).")
                return ".", (f"No root {build_file}; {len(hits)} candidate build roots ({', '.join(hits[:5])}) — "
                             "CI builds from the repo root: set META build_dir or add an aggregator build file.")
            frontier, depth = nxt, depth + 1
    except Exception as e:  # noqa: BLE001 — detection is advisory
        logger.warning("build dir detection for %s failed: %s", repo.get("name"), e)
    return ".", ""


# ── Terraform root generation (main.tf + terraform.tfvars from ONE spec) ───────

def _hcl(v) -> str:
    """Render a Python value as an HCL literal (tfvars)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        width = max((len(k) for k in v), default=0)
        body = "\n".join(f"  {k.ljust(width)} = {_hcl(x)}" for k, x in v.items())
        return "{\n" + body + "\n}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_hcl(x) for x in v) + "]"
    # json.dumps gives a valid HCL quoted string; escape template sequences.
    return json.dumps(str(v)).replace("${", "$${").replace("%{", "%%{")


# Runtime facts Execution needs, resolved as: human-confirmed META field →
# Assessment's detected_* (worker repo scan) → documented default.
_WIRING_DEFAULTS = {"container_port": DEFAULT_CONTAINER_PORT, "health_check_path": DEFAULT_HEALTH_CHECK_PATH}


def _wired(meta: dict, field: str):
    """(value, source) for a wiring field: 'meta' | 'detected' | 'default'."""
    v = meta.get(field)
    if v not in (None, ""):
        return v, "meta"
    d = meta.get(f"detected_{field}")
    if d not in (None, ""):
        return d, "detected"
    return _WIRING_DEFAULTS.get(field), "default"


def _detected_table(meta: dict) -> dict:
    tables = meta.get("detected_dynamodb_tables") or []
    return tables[0] if tables and isinstance(tables[0], dict) else {}


def _wiring_notes(meta: dict, fields: tuple) -> list:
    """One note per field that fell back to a default — the gate shows them."""
    return [f"{f} not set or detected: using the default {_WIRING_DEFAULTS.get(f)!r} — confirm it at the assessment gate."
            for f in fields if _wired(meta, f)[1] == "default"]


def _table_env(meta: dict) -> str:
    """HCL env map: TABLE_NAME / DYNAMODB_TABLE plus every env var the code reads the table name from."""
    names = ["TABLE_NAME", "DYNAMODB_TABLE"] + [str(t.get("env_var")) for t in (meta.get("detected_dynamodb_tables") or [])
                                                 if isinstance(t, dict) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(t.get("env_var") or ""))]
    return "{ " + ", ".join(f"{n} = module.database.table_name" for n in dict.fromkeys(names)) + " }"


_FN_KEY_RE = re.compile(r"[^a-z0-9-]+")
_CFN_INTRINSIC_RE = re.compile(r"^\$\{(?:Ref|GetAtt|Sub|ImportValue|Join|Select|If|FindInMap)[:}]")
_ROUTE_RE = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|ANY) /\S*$")


def _lambda_functions(meta: dict) -> dict:
    """{key: spec} for an app with SEVERAL functions (META lambda_functions → the
    repo scan's detected_lambda_functions). {} = the single-function default."""
    raw = meta.get("lambda_functions") or meta.get("detected_lambda_functions") or []
    out: dict = {}
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict) or not f.get("handler"):
            continue
        key = _FN_KEY_RE.sub("-", str(f.get("name") or f["handler"]).lower()).strip("-")[:40] or f"fn{len(out)}"
        routes = [r for r in (f.get("routes") or []) if isinstance(r, str) and _ROUTE_RE.match(r)]
        out[key] = {"handler": str(f["handler"]), "routes": routes,
                    "memory_size": _num(f.get("memory"), _num(meta.get("lambda_memory"), GENERATED_LAMBDA_MEMORY_DEFAULT),
                                        128, 10240),
                    "timeout": _num(f.get("timeout"), _num(meta.get("lambda_timeout"), GENERATED_LAMBDA_TIMEOUT_DEFAULT), 1, 900),
                    # CFN intrinsics (`${Ref:ProductsTable}`) can't resolve here: the table name
                    # comes from the module's environment (_table_env), so drop them.
                    "env": {str(k): str(v) for k, v in (f.get("env") or {}).items()
                            if re.fullmatch(r"[A-Za-z_]\w*", str(k)) and not _CFN_INTRINSIC_RE.match(str(v))},
                    # SAM `Api` (REST v1) events → payload 1.0; `HttpApi` → 2.0.
                    "payload_format": "2.0" if str(f.get("payload_format") or "") == "2.0" else "1.0"}
    return out if len(out) > 1 else {}


def _smoke_target(app_id: str, meta: dict) -> dict:
    """{smoke_function, smoke_path} for the CodePipeline smoke invoke: the health path on
    the single function, or a multi-function app's first parameter-free GET route."""
    health = str(_wired(meta, "health_check_path")[0])
    fns = _lambda_functions(meta)
    for key, f in fns.items():
        for r in f["routes"]:
            method, _, path = r.partition(" ")
            if method in ("GET", "ANY") and "{" not in path:
                return _smoke(f"{app_id}-{key}", path, f["payload_format"])
    if fns:
        first = next(iter(fns))
        return _smoke(f"{app_id}-{first}", health, fns[first]["payload_format"])
    return _smoke(f"{app_id}-function", health, "2.0")


def _smoke(function: str, path: str, payload_format: str) -> dict:
    """The smoke invoke's event in the shape the function's route delivers: REST (SAM `Api`)
    routes use payload 1.0 — an APIGatewayProxyRequestEvent handler reads httpMethod/path."""
    if payload_format == "1.0":
        event = {"resource": path, "path": path, "httpMethod": "GET", "headers": {}, "queryStringParameters": None,
                 "pathParameters": None, "requestContext": {"httpMethod": "GET", "path": path, "resourcePath": path},
                 "body": None, "isBase64Encoded": False}
    else:
        event = {"version": "2.0", "rawPath": path, "path": path, "httpMethod": "GET",
                 "requestContext": {"http": {"method": "GET", "path": path}}, "isBase64Encoded": False}
    return {"smoke_function": function, "smoke_path": path,
            "smoke_payload": json.dumps(event, separators=(",", ":")).replace("'", "")}


def _lambda_handler(meta: dict, lang: str) -> tuple[str, str]:
    """(handler, note) for the Lambda function — META lambda_handler wins.

    Spring Boot apps run behind aws-serverless-java-container's delegating
    handler; other Java apps have no conventional entry point, so it must be set."""
    explicit = str(meta.get("lambda_handler") or meta.get("detected_lambda_handler") or "").strip()
    if explicit:
        return explicit, ""
    if lang == "java":
        if "spring" in str(meta.get("framework") or "").lower():
            return ("com.amazonaws.serverless.proxy.spring.SpringDelegatingLambdaContainerHandler",
                    "lambda_handler defaults to aws-serverless-java-container's SpringDelegatingLambdaContainerHandler: "
                    "the app needs the aws-serverless-java-container-springboot3 dependency and a shaded jar.")
        return ("example.Handler::handleRequest",
                "lambda_handler is a placeholder (example.Handler::handleRequest): set lambda_handler on the app to the "
                "function's <class>::<method> before the first deploy.")
    if lang == "javascript":
        return "index.handler", ""
    return "app.handler", ""


def _permissions_boundary_arn(account_id: str) -> str:
    """ARN of the app permissions boundary in the target account (deploy.tf creates it)."""
    name = APP_PERMISSIONS_BOUNDARY_NAME or (f"{PROJECT_PREFIX}-app-permissions-boundary" if PROJECT_PREFIX else "")
    return f"arn:{AWS_PARTITION}:iam::{account_id}:policy/{name}" if name else ""


def _infra_spec(app_id: str, meta: dict, args: dict, repo: dict) -> dict:
    """Pure: decide the modules + root variables + module wiring for an app.

    Every root variable is declared in main.tf AND set in terraform.tfvars, and only
    for the modules included — so there are no "undeclared variable" warnings and
    no ECS/Lambda settings leak into an EC2 root.
    """
    # provision_infrastructure rejects an unresolvable compute before calling this.
    target_compute, _ = _resolve_compute(meta)
    target_db = (meta.get("target_database") or "").lower()
    lang = _norm_lang(meta)
    notes = []
    compute_module = _COMPUTE_MODULES.get(target_compute)

    rds = target_db in _RDS_ENGINES
    dynamo = target_db == "dynamodb"
    if target_db in ("same_as_source", "") and compute_module:
        notes.append("target_database=same_as_source: the database moves with the servers (e.g. AWS MGN); no managed DB module generated.")

    az_count = _az_count(meta)
    if rds and az_count < 2:
        notes.append("az_count raised to 2: an RDS DB subnet group requires subnets in at least two AZs.")
        az_count = 2
    vpc_cidr = args.get("vpc_cidr") or meta.get("vpc_cidr") or "10.0.0.0/16"
    region = _target_region(meta)

    variables = [  # (name, hcl type, value)
        ("app_id", "string", app_id), ("region", "string", region), ("environment", "string", _environment(meta)),
        ("vpc_cidr", "string", vpc_cidr), ("az_count", "number", az_count), ("tags", "map(string)", _tags(meta, app_id)),
    ]
    # Every app role is created with the AMF app permissions boundary —
    # the deploy role is denied CreateRole/PutRolePolicy/AttachRolePolicy without it.
    account_id = str(args.get("account_id") or meta.get("target_account") or "REPLACE_ACCOUNT_ID")
    boundary = _permissions_boundary_arn(account_id)
    pb = "  permissions_boundary_arn = var.permissions_boundary_arn\n" if boundary else ""
    if boundary:
        variables.append(("permissions_boundary_arn", "string", boundary))
    blocks = [
        'module "vpc" {\n  source   = "./modules/vpc"\n  app_id   = var.app_id\n  vpc_cidr = var.vpc_cidr\n'
        '  az_count = var.az_count\n  tags     = var.tags\n}',
    ]
    outputs = ['output "vpc_id" { value = module.vpc.vpc_id }']
    modules = ["vpc"]
    health = _wired(meta, "health_check_path")[0]
    port = _num(meta.get("app_port") or _wired(meta, "container_port")[0], DEFAULT_CONTAINER_PORT, 1, 65535)
    if compute_module == "ecs-fargate":
        notes += _wiring_notes(meta, ("container_port", "health_check_path"))
    desired = _num(meta.get("desired_count"), 2, 1, 100)

    arch = _cpu_architecture(meta)
    # The generated DynamoDB table reaches the app as env vars + CRUD on the table.
    data_access = ""
    if dynamo and compute_module in ("ecs-fargate", "lambda", "ec2"):
        data_access = (f'  environment        = {_table_env(meta)}\n'
                       '  dynamodb_table_arns = [module.database.table_arn]\n')
    elif rds and compute_module in ("ecs-fargate", "ec2"):
        # The RDS endpoint + the RDS-managed master secret reach the app as Spring
        # datasource and generic DB_* env — the app runs unmodified (APP-HE 2026-10-23).
        # ECS injects the secret as a container secret; EC2 reads it at boot (lift: rds_mysql).
        jdbc = "postgresql" if target_db.endswith("postgresql") else "mysql"
        if compute_module == "ecs-fargate":
            secret = '"${module.database.master_user_secret_arn}:password::"'  # nosec B105 — Terraform ref to the RDS-managed secret's JSON key, no value
        else:
            secret = '{ arn = module.database.master_user_secret_arn, key = "password" }'  # nosec B105 — Terraform ref to the secret's JSON key, no value
        data_access = (
            '  environment        = { SPRING_DATASOURCE_URL = "jdbc:' + jdbc + '://${module.database.endpoint}:'
            '${module.database.port}/${module.database.db_name}", SPRING_DATASOURCE_USERNAME = module.database.username, '
            'DB_HOST = module.database.endpoint, DB_PORT = tostring(module.database.port), '
            'DB_NAME = module.database.db_name, DB_USER = module.database.username'
            + _spring_profile_env(meta, target_db) + ' }\n'
            f'  secrets            = {{ SPRING_DATASOURCE_PASSWORD = {secret}, DB_PASSWORD = {secret} }}\n'
            '  secret_kms_key_arns = [module.database.kms_key_arn]\n')
        if compute_module == "ecs-fargate":
            # The ECS task SG only allows HTTPS out; open the DB port to the VPC CIDR so JDBC connects.
            # A static engine port (not module.database.port): the database module already takes the
            # compute SG as input, so referencing its output here would be a dependency cycle.
            variables.append(("db_port", "number", 5432 if "postgresql" in target_db else 3306))
            data_access += ('  db_egress_port        = var.db_port\n'
                            '  db_egress_cidr_blocks = [var.vpc_cidr]\n')
    if compute_module == "ecs-fargate":
        modules.append("ecs-fargate")
        # Not exposed: internal ALB reachable from the VPC (+ explicitly allowed
        # private CIDRs) only. A public ALB is an explicit META opt-in.
        public = _truthy(meta.get("expose_publicly")) is True
        ingress = [vpc_cidr] + [c for c in (meta.get("allowed_ingress_cidrs") or []) if c not in ("0.0.0.0/0", "::/0")]
        if public:
            notes.append("expose_publicly=true: the ALB is internet-facing (public subnets) but still only accepts "
                         "allowed_ingress_cidrs — never 0.0.0.0/0.")
        variables += [("container_port", "number", port), ("health_check_path", "string", health),
                      ("cpu", "number", _num(meta.get("cpu"), 1024)), ("memory", "number", _num(meta.get("memory"), 2048)),
                      ("desired_count", "number", desired), ("internal_alb", "bool", not public),
                      ("allowed_ingress_cidrs", "list(string)", list(dict.fromkeys(ingress))),
                      ("cpu_architecture", "string", arch)]
        blocks.append(
            'module "compute" {\n  source             = "./modules/ecs-fargate"\n  app_id             = var.app_id\n'
            '  vpc_id             = module.vpc.vpc_id\n  public_subnet_ids  = module.vpc.public_subnet_ids\n'
            '  private_subnet_ids = module.vpc.private_app_subnet_ids\n  container_port     = var.container_port\n'
            '  health_check_path  = var.health_check_path\n  cpu                = var.cpu\n  memory             = var.memory\n'
            '  desired_count      = var.desired_count\n  internal_alb       = var.internal_alb\n'
            '  allowed_ingress_cidrs = var.allowed_ingress_cidrs\n  cpu_architecture   = var.cpu_architecture\n'
            + data_access + pb + '  tags               = var.tags\n}')
        outputs += ['output "alb_dns" { value = module.compute.alb_dns }', 'output "ecr_url" { value = module.compute.ecr_url }']
    elif compute_module == "ec2":
        modules.append("ec2")
        ami = str(meta.get("ami_id") or meta.get("source_ami_id") or "")
        if not ami:
            notes.append("ami_id is empty: set it in terraform.tfvars to the AWS MGN-launched / golden AMI (or \"al2023\") before plan/apply.")
        java_v = _runtime_versions(meta, app_id)["java_version"] if ami == "al2023" else ""
        if java_v:
            notes.append(f"ami_id=al2023: fresh Amazon Linux 2023 instances with Corretto {java_v} "
                         "and a systemd unit that runs the deployed jar (no source image).")
        public = _truthy(meta.get("expose_publicly")) is True
        ingress = [vpc_cidr] + [c for c in (meta.get("allowed_ingress_cidrs") or []) if c not in ("0.0.0.0/0", "::/0")]
        if public:
            notes.append("expose_publicly=true: the ALB is internet-facing (public subnets) but still only accepts "
                         "allowed_ingress_cidrs — never 0.0.0.0/0.")
        variables += [("ami_id", "string", ami), ("instance_type", "string", str(meta.get("instance_type") or "m6i.large")),
                      ("app_port", "number", port), ("health_check_path", "string", health),
                      ("min_size", "number", desired), ("max_size", "number", max(desired * 2, 2)),
                      ("desired_capacity", "number", desired), ("internal_alb", "bool", not public),
                      ("allowed_ingress_cidrs", "list(string)", list(dict.fromkeys(ingress)))]
        blocks.append(
            'module "compute" {\n  source             = "./modules/ec2"\n  app_id             = var.app_id\n'
            '  vpc_id             = module.vpc.vpc_id\n  public_subnet_ids  = module.vpc.public_subnet_ids\n'
            '  private_subnet_ids = module.vpc.private_app_subnet_ids\n  ami_id             = var.ami_id\n'
            '  instance_type      = var.instance_type\n  app_port           = var.app_port\n'
            '  health_check_path  = var.health_check_path\n  min_size           = var.min_size\n'
            '  max_size           = var.max_size\n  desired_capacity   = var.desired_capacity\n'
            '  internal_alb       = var.internal_alb\n  allowed_ingress_cidrs = var.allowed_ingress_cidrs\n'
            + (f'  service_name       = "{_shell_safe(meta.get("service_name")) or app_id.lower()}"\n' if data_access or ami == "al2023" else '')
            + (f'  java_version       = "{java_v}"\n'
               f'  install_dir        = "{_shell_safe(meta.get("install_dir")) or "/opt/" + app_id.lower()}"\n' if ami == "al2023" else '')
            + data_access + pb + '  tags               = var.tags\n}')
        outputs += ['output "alb_dns" { value = module.compute.alb_dns }',
                    'output "artifact_bucket" { value = module.compute.artifact_bucket }']
    elif compute_module == "lambda":
        modules.append("lambda")
        rv = _runtime_versions(meta, app_id)
        runtime = {"java": "java" + ("8.al2" if rv["java_version"] == "8" else rv["java_version"]),
                   "javascript": f"nodejs{rv['node_version']}.x"}.get(lang, f"python{rv['python_version']}")
        handler, handler_note = _lambda_handler(meta, lang)
        if handler_note:
            notes.append(handler_note)
        fns = _lambda_functions(meta)
        functions_hcl = ""
        if fns:
            # Several functions from the repo's SAM/CDK template.
            functions_hcl = "  functions = " + _hcl({
                k: {"handler": f["handler"], "memory_size": f["memory_size"], "timeout": f["timeout"],
                    "env": f["env"], "routes": f["routes"], "payload_format": f["payload_format"]}
                for k, f in fns.items()}).replace("\n", "\n  ") + "\n"
            notes.append(f"{len(fns)} Lambda functions from the repo template: "
                         + ", ".join(f"{app_id}-{k} ({', '.join(f['routes']) or 'no routes'})" for k, f in fns.items()))
        variables += [("lambda_runtime", "string", meta.get("lambda_runtime") or runtime),
                      ("lambda_handler", "string", handler),
                      ("lambda_memory_size", "number", _num(meta.get("lambda_memory"), GENERATED_LAMBDA_MEMORY_DEFAULT)),
                      ("lambda_timeout", "number", _num(meta.get("lambda_timeout"), GENERATED_LAMBDA_TIMEOUT_DEFAULT, 1, 900))]
        blocks.append(
            'module "compute" {\n  source             = "./modules/lambda"\n  app_id             = var.app_id\n'
            '  attach_vpc         = true\n'
            '  vpc_id             = module.vpc.vpc_id\n  private_subnet_ids = module.vpc.private_app_subnet_ids\n'
            '  runtime            = var.lambda_runtime\n  handler            = var.lambda_handler\n'
            '  memory_size        = var.lambda_memory_size\n'
            '  timeout            = var.lambda_timeout\n' + data_access + functions_hcl + pb + '  tags               = var.tags\n}')
        outputs += ['output "api_endpoint" { value = module.compute.api_endpoint }',
                    'output "function_name" { value = module.compute.function_name }',
                    'output "artifact_bucket" { value = module.compute.artifact_bucket }']
        notes.append(f"Lambda is created with a placeholder package; CI/CD publishes the real one to the artifact "
                     f"bucket and runs update-function-code ({app_id}-function).")

    db_reason = ""
    if rds:
        modules.append("rds")
        db = _as_dict(meta.get("database"))
        multi_az, db_reason = _db_multi_az(meta)
        instance_class = str(meta.get("db_instance_class") or db.get("instance_class") or "")
        if not instance_class:
            notes.append("db_instance_class is empty: set it in terraform.tfvars (e.g. db.r6g.large) — sizing is a design decision.")
        variables += [("db_engine", "string", target_db), ("db_instance_class", "string", instance_class),
                      ("db_multi_az", "bool", multi_az),
                      ("db_allocated_storage", "number", _num(db.get("size_gb"), 100, 20, 65536)),
                      ("db_backup_retention_days", "number", _num(meta.get("backup_retention_days"), 14, 1, 35)),
                      ("db_name", "string", re.sub(r"[^A-Za-z0-9_]", "", str(meta.get("db_name") or db.get("name") or "appdb"))[:63] or "appdb")]
        sg = "[module.compute.app_security_group_id]" if compute_module in ("ecs-fargate", "ec2") else "[]"
        blocks.append(
            'module "database" {\n  source                     = "./modules/rds"\n  app_id                     = var.app_id\n'
            '  engine                     = var.db_engine\n  instance_class             = var.db_instance_class\n'
            '  multi_az                   = var.db_multi_az\n  allocated_storage          = var.db_allocated_storage\n'
            '  backup_retention_days      = var.db_backup_retention_days\n  db_name                    = var.db_name\n'
            '  vpc_id                     = module.vpc.vpc_id\n'
            '  db_subnet_ids              = module.vpc.private_db_subnet_ids\n  allowed_cidr_blocks        = [var.vpc_cidr]\n'
            f'  allowed_security_group_ids = {sg}\n  tags                       = var.tags\n}}')
        outputs += ['output "db_endpoint" { value = module.database.endpoint }',
                    'output "db_secret_arn" { value = module.database.master_user_secret_arn }']
    elif dynamo:
        modules.append("dynamodb")
        # Key schema / name the app expects (META), else the module's {app_id}-table PK/SK.
        det = _detected_table(meta)
        hash_key = meta.get("dynamodb_hash_key") or det.get("hash_key") or ""
        # A custom hash key means "no range key" unless one is given alongside it.
        range_key = (meta.get("dynamodb_range_key") or "") if meta.get("dynamodb_hash_key") else (det.get("range_key") or "")
        if not hash_key:  # nothing set or detected: the module's generic single-table layout
            hash_key, range_key = "PK", "SK"
            notes.append("DynamoDB key schema not set or detected: using PK/SK — confirm it at the assessment gate.")
        variables += [("dynamodb_table_name", "string", str(meta.get("dynamodb_table_name") or det.get("name") or "")),
                      ("dynamodb_hash_key", "string", str(hash_key)),
                      ("dynamodb_range_key", "string", str(range_key))]
        blocks.append('module "database" {\n  source     = "./modules/dynamodb"\n  app_id     = var.app_id\n'
                      '  table_name = var.dynamodb_table_name\n  hash_key   = var.dynamodb_hash_key\n'
                      '  range_key  = var.dynamodb_range_key\n  tags       = var.tags\n}')
        outputs.append('output "table_name" { value = module.database.table_name }')
        if compute_module == "ec2":
            notes.append("DynamoDB table: the EC2 instance role gets CRUD on it and the service gets TABLE_NAME / "
                         "DYNAMODB_TABLE via /etc/<service>.env (systemd drop-in).")

    # CodePipeline (CodeCommit -> CodeBuild -> ECS | CodeDeploy to the EC2 ASG |
    # Lambda update from CodeBuild): needs a CodeCommit repo (its native source).
    # Sources the migration (deploy) branch the gate deploys — see deploy_branch below.
    cicd_target = (meta.get("cicd_target") or "").lower()
    include_codepipeline = False
    deploy_target = _DEPLOY_TARGETS.get(compute_module or "", "")
    if cicd_target == "codepipeline" and deploy_target:
        if repo.get("host") == "codecommit":
            include_codepipeline = True
            modules.append("codepipeline")
            # The pipeline sources the branch the deploy gate applies and waits on
            # (worker deploy_runner.deploy_branch): META deploy_branch, else the migration
            # branch. Sourcing the repo's default branch made the gate wait for a run of
            # the reviewed commit that never came (APP-CLI 2026-10-26: linked branch atx-run1).
            deploy_branch = str(meta.get("deploy_branch") or "").strip() or migration_branch(app_id)
            # On the migration branch a push must not deploy unreviewed code: the deploy gate
            # starts the run of the approved commit itself (deploy_runner.wait_for_pipeline).
            on_push = deploy_branch != migration_branch(app_id)
            notes.append(f"Pipeline sources branch '{deploy_branch}'"
                         + (" (push trigger on)." if on_push else
                            " (the reviewed migration branch; push trigger OFF — runs start only from the deploy gate)."))
            variables += [("repo_name", "string", repo["name"]), ("branch", "string", deploy_branch),
                          ("trigger_on_push", "bool", on_push),
                          ("buildspec_path", "string", "cicd/buildspec.yml"), ("build_arch", "string", arch)]
            target_wiring = {
                "ecs": ('  ecr_repo_url     = module.compute.ecr_url\n  ecs_cluster_name = "${var.app_id}-cluster"\n'
                        '  ecs_service_name = module.compute.service_name\n  container_name   = var.app_id\n'),
                "ec2": ('  asg_name            = module.compute.asg_name\n'
                        '  target_group_name   = module.compute.target_group_name\n'
                        '  instance_role_name  = module.compute.instance_role_name\n'
                        '  app_artifact_bucket = module.compute.artifact_bucket\n'),
                "lambda": ('  function_name       = module.compute.function_name\n'
                           '  function_names      = module.compute.function_names\n'
                           '  app_artifact_bucket = module.compute.artifact_bucket\n'),
            }[deploy_target]
            blocks.append(
                'module "cicd" {\n  source           = "./modules/codepipeline"\n  app_id           = var.app_id\n'
                '  repo_name        = var.repo_name\n  branch           = var.branch\n  trigger_on_push  = var.trigger_on_push\n  buildspec        = var.buildspec_path\n'
                f'  build_arch       = var.build_arch\n  deploy_target    = "{deploy_target}"\n'
                + target_wiring + pb + '  tags             = var.tags\n}')
            outputs.append('output "pipeline_name" { value = module.cicd.pipeline_name }')
        else:
            notes.append("cicd_target=codepipeline but no CodeCommit repo linked; skipped pipeline module (seed the repo to CodeCommit first).")

    return {"target_compute": target_compute, "compute_module": compute_module, "modules": modules,
            "variables": variables, "blocks": blocks, "outputs": outputs, "notes": notes, "region": region,
            "include_codepipeline": include_codepipeline, "db_multi_az_reason": db_reason}


def _root_main_tf(spec: dict) -> str:
    """Root main.tf: variable declarations (no defaults — values live in tfvars) +
    module wiring + outputs. The `terraform {}` / `provider "aws" {}` blocks live in
    backend.tf (declaring them twice fails `terraform validate`)."""
    lines = [f'variable "{n}" {{ type = {t} }}' for n, t, _ in spec["variables"]]
    return "\n".join(lines) + "\n\n" + "\n\n".join(spec["blocks"]) + "\n\n" + "\n".join(spec["outputs"]) + "\n"


def _tfvars_body(spec: dict) -> str:
    """`terraform fmt`-clean tfvars: runs of single-line values are aligned; a
    multi-line value (map) stands alone between blank lines."""
    out, group = [], []

    def flush():
        if group:
            width = max(len(n) for n, _ in group)
            out.append("\n".join(f"{n.ljust(width)} = {v}" for n, v in group))
            group.clear()

    for n, _, v in spec["variables"]:
        rendered = _hcl(v)
        if "\n" in rendered:
            flush()
            out.append(f"{n} = {rendered}")
        else:
            group.append((n, rendered))
    flush()
    return "\n\n".join(out) + "\n"


def _tfstate_names(state_prefix: str, account_id: str, region: str) -> tuple[str, str]:
    """(bucket, lock table) of the app's Terraform state backend in the target account."""
    fmt = {"prefix": state_prefix, "account": account_id, "region": region}
    return APP_TFSTATE_BUCKET_PATTERN.format(**fmt), APP_TFSTATE_LOCK_PATTERN.format(**fmt)


def _prefix_error(app_id: str, action: str) -> dict:
    """Fail closed: every generated resource name derives from PROJECT_PREFIX — never guess one."""
    return {"status": "error", "app_id": app_id,
            "message": f"PROJECT_PREFIX is not configured on the tool Lambda; refusing to {action} "
                       "(resource names would not match this AMF deployment). Set it via Terraform (lambdas.tf)."}


def _infra_readme(app_id: str, spec: dict, state_prefix: str, account_id: str, notes: list) -> str:
    region = spec["region"]
    bucket, lock = _tfstate_names(state_prefix, account_id, region)
    create_bucket = (f"aws s3api create-bucket --bucket {bucket} --region {region}"
                     + ("" if region == "us-east-1" else f" --create-bucket-configuration LocationConstraint={region}"))
    db_line = f"\nDatabase Multi-AZ: {spec['db_multi_az_reason']}.\n" if spec.get("db_multi_az_reason") else ""
    return (
        f"# Generated Terraform for {app_id}\n\n"
        f"Target compute: **{spec['target_compute']}**. Modules: {', '.join(spec['modules'])}. Region: {region}.\n"
        f"{db_line}\n"
        "**Generated and staged — nothing was applied.** Committed to the migration branch "
        f"`{migration_branch(app_id)}` for review.\n\n"
        "## Bootstrap the state backend (once, in the TARGET account)\n\n"
        f"`backend.tf` stores state in the target account ({account_id}). Create the bucket and lock table "
        "with target-account credentials before `terraform init`:\n\n"
        "```\n"
        f"{create_bucket}\n"
        f"aws s3api put-bucket-versioning --bucket {bucket} --versioning-configuration Status=Enabled\n"
        f"aws s3api put-bucket-encryption --bucket {bucket} --server-side-encryption-configuration "
        "'{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"aws:kms\"}}]}'\n"
        f"aws s3api put-public-access-block --bucket {bucket} --public-access-block-configuration "
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true\n"
        f"aws dynamodb create-table --table-name {lock} --region {region} --billing-mode PAY_PER_REQUEST "
        "--attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH\n"
        "```\n\n"
        "## Apply\n\n"
        "```\nterraform init\nterraform plan\nterraform apply\n```\n\n"
        "Every root variable is declared in `main.tf` and set in `terraform.tfvars`; review the values "
        "(especially any left empty) before applying.\n"
        + ("\n## Notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n" if notes else "")
    )


@registry.tool(
    "provision_infrastructure",
    description="Generate standardized Terraform (from the module library) for the app's target compute and STAGE it to S3 (generate + stage only; nothing is applied).",
    schema={
        "app_id": {"type": "string", "required": True},
        "vpc_cidr": {"type": "string"},
        "account_id": {"type": "string"},
        "domain_name": {"type": "string"},
    },
    infer=lambda a: "app_id" in a and str(a.get("_action", "")) == "provision_infrastructure",
)
def provision_infrastructure(args):
    app_id = args["app_id"]
    # The state backend lives in the TARGET account; its names derive from the AMF
    # project prefix (PROJECT_PREFIX env). An unset prefix fails closed — it used to
    # fall back to "amf" and point another estate's IaC at the wrong state bucket.
    state_prefix = PROJECT_PREFIX
    if not state_prefix:
        _set_exec_status(app_id, "exec_infra_status", "error")
        return _prefix_error(app_id, "generate infrastructure")
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    account_id = args.get("account_id") or meta.get("target_account") or "REPLACE_ACCOUNT_ID"
    # Generate exactly the compute the user approved — a VPC-only stack for an
    # unsupported/invalid compute used to report "staged" and pass every check.
    _, compute_error = _resolve_compute(meta)
    if compute_error:
        _set_exec_status(app_id, "exec_infra_status", "error")
        return {"status": "error", "app_id": app_id, "message": compute_error}
    repo = _linked_repo(app_id)
    spec = _infra_spec(app_id, meta, args, repo)
    target_compute, modules, notes = spec["target_compute"], spec["modules"], list(spec["notes"])
    files = []
    missing_modules = []

    for m in modules:
        try:
            _stage(app_id, "infrastructure", f"modules/{m}/main.tf", _tpl(f"terraform/modules/{m}/main.tf"))
            files.append(f"modules/{m}/main.tf")
        except Exception:
            # Fail hard rather than silently skipping — a missing module template means
            # the generated IaC would reference a nonexistent `./modules/<m>` and never
            # terraform init. Record it + return an actionable error so the user adds
            # the template under backend/resources/templates/terraform/modules/<m>/ and retries.
            missing_modules.append(m)

    if missing_modules:
        _set_exec_status(app_id, "exec_infra_status", "error")
        return {
            "status": "error", "app_id": app_id,
            "missing_modules": missing_modules,
            "message": (
                "Required Terraform module template(s) not available: "
                + ", ".join(f"'{m}'" for m in missing_modules)
                + ". Add the module under backend/resources/templates/terraform/modules/<name>/main.tf"
                " (register it in templates.json), redeploy (setup.sh syncs the template library to S3),"
                " then retry provision_infrastructure."
            ),
        }

    tfstate_bucket, tfstate_lock = _tfstate_names(state_prefix, account_id, spec["region"])
    header_ctx = {
        "app_id": app_id, "app_name": meta.get("app_name", app_id), "strategy": meta.get("migration_strategy", ""),
        "target_compute": target_compute, "modules": ", ".join(modules),
    }
    body = _tfvars_body(spec)
    for rel, key, ctx, fallback in [
        ("terraform.tfvars", "terraform/environments/template.tfvars", {**header_ctx, "tfvars_body": body}, body),
        ("backend.tf", "terraform/backend.tf.tpl",
         {"account_id": account_id, "region": spec["region"], "app_id": app_id, "state_prefix": state_prefix,
          "tfstate_bucket": tfstate_bucket, "tfstate_lock": tfstate_lock}, None),
    ]:
        try:
            # tfvars_body is substituted last so values in it are never re-rendered.
            tpl = _tpl(key)
            body_ctx = {k: v for k, v in ctx.items() if k != "tfvars_body"}
            text = _render(tpl, body_ctx)
            if "tfvars_body" in ctx:
                text = text.replace("${tfvars_body}", ctx["tfvars_body"])
            _stage(app_id, "infrastructure", rel, text)
            files.append(rel)
        except Exception as e:
            if fallback is not None:
                _stage(app_id, "infrastructure", rel, fallback)
                files.append(rel)
            notes.append(f"{rel}: {e}")

    _stage(app_id, "infrastructure", "main.tf", _root_main_tf(spec))
    files.append("main.tf")
    _stage(app_id, "infrastructure", "README.md", _infra_readme(app_id, spec, state_prefix, account_id, notes))
    files.append("README.md")

    prefix = f"s3://{BUCKET}/applications/{app_id}/execution/infrastructure/"
    _record_execution(app_id, "iac", prefix, files, {
        "target_compute": target_compute, "modules": modules, "notes": notes,
        "branch": migration_branch(app_id), "region": spec["region"],
    })

    # Deterministically commit the staged Terraform into the repo under infra/ on the
    # migration branch and record the sub-step status — no LLM harness dependency.
    commit = _commit_staged_to_repo(
        app_id, f"applications/{app_id}/execution/infrastructure/",
        lambda rel: f"infra/{rel}", "iac-commit",
        f"chore(infra): add generated Terraform IaC for {app_id}",
    )
    # Honest status: "committed" only when the push actually succeeded;
    # otherwise "staged" (artifacts exist in S3 but repo was NOT updated).
    _set_exec_status(app_id, "exec_infra_status",
                     "committed" if commit.get("status") in ("committed", "no_change") else "staged")
    return {"status": "staged", "app_id": app_id, "target_compute": target_compute, "modules": modules,
            "s3_prefix": prefix, "files": files, "notes": notes, "commit": commit,
            "message": f"Terraform generated, staged to S3, and committed under infra/ on {commit.get('branch') or migration_branch(app_id)} "
                       "(nothing applied — a human runs terraform init/plan/apply)."}


# ── CI/CD generation ───────────────────────────────────────────────────────────

# cicd_target -> repo host(s) where that provider's pipeline files actually run.
# Scope: AWS CodePipeline sourcing CodeCommit is the only CI/CD target.
_PROVIDER_HOSTS = {"codepipeline": ("codecommit",)}
# Base-image mirror for unqualified Docker Official Images in buildspecs (Docker Hub 429s CodeBuild); "" = off.
_DOCKERHUB_MIRROR = os.environ.get("DOCKERHUB_MIRROR", "public.ecr.aws/docker/library")
# Validation waits this long for an in-progress ECS rollout before judging ecs_service_steady
# (must leave room for the in-VPC probe inside the 900 s tool timeout).
_ECS_ROLLOUT_WAIT_SECONDS = int(os.environ.get("ECS_ROLLOUT_WAIT_SECONDS", "240"))
_ECS_ROLLOUT_POLL_SECONDS = int(os.environ.get("ECS_ROLLOUT_POLL_SECONDS", "15"))


def _select_blocks(text: str, flags: set) -> str:
    """Keep `#@if <flag>` ... `#@endif` blocks whose flag is in `flags`; drop the rest
    and the marker lines themselves (markers are YAML comments, so raw templates parse)."""
    out, stack = [], []
    for line in text.splitlines(keepends=True):
        s = line.strip()
        m = re.fullmatch(r"#@if (\w+)", s)
        if m:
            stack.append(m.group(1) in flags)
            continue
        if s == "#@endif":
            if stack:
                stack.pop()
            continue
        if '"#@if"' in s:
            continue  # template-only comment explaining the markers
        if all(stack):
            out.append(line)
    return "".join(out)


def _provider_mismatch(cicd_target: str, host: str) -> str:
    """'' when the provider runs on the repo host, else a human-readable warning."""
    hosts = _PROVIDER_HOSTS.get(cicd_target)
    if not hosts or not host or host in hosts:
        return ""
    return (f"AWS CodePipeline will not run on a {host} repo as generated: the codepipeline module sources CodeCommit; "
            f"a {host} source needs a CodeStar connection (not provisioned). Mirror the repo to CodeCommit.")


def _shell_safe(value) -> str:
    """A META value rendered into shell/YAML (unit names, paths): '' unless it is a
    plain path/identifier, so a stray quote or `$(...)` never reaches a hook script."""
    v = str(value or "").strip()
    return v if re.fullmatch(r"[A-Za-z0-9_./@-]+", v) else ""


def _cicd_context(app_id: str, meta: dict, repo: dict) -> tuple[dict, list]:
    """Variables for the CI/CD templates (+ notes). Names MUST match the Terraform
    modules: ECR repo = lower(app_id); cluster/service/task/ALB = "{app_id}-cluster|
    -service|-task|-alb"; artifact bucket = ec2 module's lower("{app_id}-artifacts-{account}-{region}")."""
    notes = []
    account = str(meta.get("target_account") or "REPLACE_ACCOUNT")
    region = _target_region(meta)
    lang = _norm_lang(meta)
    default_branch = repo.get("default_branch") or "main"
    versions = _runtime_versions(meta, app_id)
    build_dir = str(meta.get("build_dir") or meta.get("detected_build_dir") or "").strip().strip("/") or ""
    if not build_dir:
        build_dir, note = _detect_build_dir(repo, lang, default_branch)
        if note:
            notes.append(note)
    # Every language: has_dockerfile decides whether a Dockerfile gets generated
    # (only reading it for Java reported "No Dockerfile" for APP-MUSIC's root one).
    facts = _build_module_facts(repo, build_dir or ".", default_branch)
    # The build file's declared release wins when it is higher than what META / the default chose:
    # javac cannot compile below the release the code declares (APP-HE: pom release 25, META said 21,
    # the pipeline failed "release version 25 not supported"). A planned java-version-upgrade target
    # still wins, because CI builds the transformed code.
    if lang == "java" and facts.get("java_version") and "java-version-upgrade" not in versions["java_version_source"]:
        detected = _parse_major(facts["java_version"], "java")
        current = _parse_major(versions["java_version"], "java") or 0
        if detected and (versions["java_version_source"] == "default" or detected > current):
            if versions["java_version_source"] != "default":
                notes.append(f"java_version: {build_dir or '.'} build file declares Java {detected}, above "
                             f"{versions['java_version_source']} ({current}) — building with the build file's version.")
            versions["java_version"] = str(next((v for v in _JAVA_SUPPORTED if v >= detected), _JAVA_SUPPORTED[-1]))
            versions["java_version_source"] = f"{build_dir or '.'} build file"
    if lang == "java":
        notes.append(f"java_version={versions['java_version']} (from {versions['java_version_source']}).")
    threshold = _num(meta.get("coverage_threshold"), COVERAGE_THRESHOLD_DEFAULT, 0, 100)
    reason = re.sub(r'["\\$`\n]', " ", str(meta.get("coverage_threshold_reason") or "")).strip()
    if threshold == 0:
        notes.append("coverage gate disabled for this app (coverage_threshold=0): "
                     + (reason or "NO REASON RECORDED — set coverage_threshold_reason on the app."))
    ctx = {
        "app_name": app_id, "branch": default_branch, "migration_branch": migration_branch(app_id),
        "aws_region": region, "aws_account_id": account,
        "ecr_repo": app_id.lower(), "iam_role_arn": f"arn:aws:iam::{account}:role/{app_id}-cicd",
        "java_version": versions["java_version"], "python_version": versions["python_version"],
        "node_version": versions["node_version"],
        "build_dir": build_dir or ".",
        "coverage_threshold": str(threshold), "coverage_reason": reason or "not recorded",
        "build_tool": facts.get("build_tool") or str(meta.get("build_tool") or "").lower(),
        "has_dockerfile": bool(facts.get("has_dockerfile")),
        "container_port": str(_num(meta.get("app_port") or _wired(meta, "container_port")[0], DEFAULT_CONTAINER_PORT, 1, 65535)),
        "dockerhub_mirror": _DOCKERHUB_MIRROR,
        "artifact_bucket": f"{app_id}-artifacts-{account}-{region}".lower(),
        "ecs_cluster": f"{app_id}-cluster", "ecs_service": f"{app_id}-service", "container_name": app_id,
        "task_definition": f"{app_id}-task", "alb_name": f"{app_id}-alb",
        "health_check_path": _wired(meta, "health_check_path")[0],
        "health_check_url": meta.get("health_check_url") or "",
        "environment": _environment(meta), "codedeploy_app": f"{app_id}-cd", "codedeploy_group": f"{app_id}-dg",
        # lambda module / CodeDeploy revision (systemd unit + release dir on the AMI).
        "function_name": f"{app_id}-function",
        **_smoke_target(app_id, meta),
        "service_name": _shell_safe(meta.get("service_name")) or app_id.lower(),
        "install_dir": _shell_safe(meta.get("install_dir")) or f"/opt/{app_id.lower()}",
    }
    return ctx, notes


@registry.tool(
    "generate_cicd",
    description="Render the standardized CI/CD pipeline (build + coverage gate + deploy) for the app's cicd_target and STAGE to S3, then commit it to the migration branch.",
    schema={
        "app_id": {"type": "string", "required": True},
        "cicd_target": {"type": "string", "description": "codepipeline (the only supported target)"},
    },
    infer=lambda a: "app_id" in a and str(a.get("_action", "")) == "generate_cicd",
)
def generate_cicd(args):
    app_id = args["app_id"]
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    cicd_target = (args.get("cicd_target") or meta.get("cicd_target") or "").lower()

    # Template-folder slug per provider. CodePipeline has its own library (CodeBuild
    # buildspec + deploy note); the pipeline itself is provisioned by the Terraform
    # `codepipeline` module from provision_infrastructure.
    provider = {"codepipeline": "codepipeline"}.get(cicd_target)
    if not provider:
        _set_exec_status(app_id, "exec_cicd_status", "error")
        return {"status": "error", "app_id": app_id,
                "message": (f"cicd_target='{cicd_target or '(not set)'}' has no template library "
                            f"(supported: {', '.join(sorted(_PROVIDER_HOSTS))}).")}
    target_compute, compute_error = _resolve_compute(meta)
    if compute_error:
        _set_exec_status(app_id, "exec_cicd_status", "error")
        return {"status": "error", "app_id": app_id, "message": compute_error}
    notes, warnings = [], []
    lang = _norm_lang(meta)
    is_container = target_compute in _CONTAINER_COMPUTE
    deploy_target = _DEPLOY_TARGETS[_COMPUTE_MODULES[target_compute]]

    try:
        tj = json.loads(_tpl(f"{provider}/templates.json"))
    except Exception as e:
        # Fail hard — match the IaC fail-fast discipline so the user adds the
        # missing template library instead of silently producing nothing.
        return {
            "status": "error", "app_id": app_id,
            "message": (
                f"CI/CD template library for '{cicd_target}' (provider='{provider}') not available: {e}. "
                "Add backend/resources/templates/{provider}/templates.json (plus the referenced ci/cd files), "
                "redeploy (setup.sh syncs the template library to S3), then retry generate_cicd."
            ),
        }
    tlist = tj if isinstance(tj, list) else tj.get("templates", [])
    ci = next((t for t in tlist if t.get("type") == "ci" and t.get("language", "").lower() == lang), None)
    if not ci:
        # Another language's CI (e.g. Maven for a Python app) would fail on its first run.
        _set_exec_status(app_id, "exec_cicd_status", "error")
        langs = sorted({t.get("language", "") for t in tlist if t.get("type") == "ci"})
        return {"status": "error", "app_id": app_id,
                "message": (f"No {cicd_target} CI template for language='{lang or '(not set)'}' "
                            f"(available: {', '.join(langs)}). Add one under backend/resources/templates/{provider}/ "
                            "or pick another cicd_target.")}
    # CD for exactly the approved compute — never another target's (an ECS deploy
    # for a Lambda app). Every CodePipeline compute has one; missing = broken library.
    cd = next((t for t in tlist if t.get("type") == "cd" and t.get("target") == deploy_target), None)
    if not cd:
        _set_exec_status(app_id, "exec_cicd_status", "error")
        return {"status": "error", "app_id": app_id,
                "message": f"No {cicd_target} CD template for target_compute={target_compute} (deploy_target={deploy_target})."}
    codedeploy = [t for t in tlist if t.get("type") == "codedeploy" and t.get("target") == deploy_target]
    repo = _linked_repo(app_id)
    ctx, ctx_notes = _cicd_context(app_id, meta, repo)
    notes += ctx_notes
    tool = ctx.get("build_tool") or ""
    if tool:
        ci = next((t for t in tlist if t.get("type") == "ci" and t.get("language", "").lower() == lang
                   and t.get("build_tool", "").lower() == tool), None) or ci
    dockerfile_tpl = None
    # META generate_dockerfile=true replaces a repo Dockerfile that can't build/run the
    # app (APP-HE: a JDK 24+ flag on Java 21, single-stage Maven image) with AMF's.
    regenerate = _truthy(meta.get("generate_dockerfile")) is True
    if is_container and (regenerate or not ctx.get("has_dockerfile")):
        dockerfile_tpl = next((t for t in tlist if t.get("type") == "container" and t.get("language", "").lower() == lang), None)
        if dockerfile_tpl and regenerate and ctx.get("has_dockerfile"):
            notes.append(f"generate_dockerfile=true: the repo's {ctx['build_dir']}/Dockerfile is REPLACED by AMF's "
                         f"(Corretto {ctx['java_version']}, non-root, runs the CodeBuild-built jar).")
        elif dockerfile_tpl:
            notes.append(f"No Dockerfile in {ctx['build_dir']}/ — generated one (Corretto {ctx['java_version']}, non-root).")
        else:
            warnings.append(f"No Dockerfile in {ctx['build_dir']}/ and no container template for language={lang}: "
                            "the docker build stage will fail until one is added.")
    mismatch = _provider_mismatch(cicd_target, repo.get("host", ""))
    if mismatch:
        warnings.append(mismatch)
    # Template markers: container (ECS image) | artifact (EC2 CodeDeploy revision) |
    # lambda (function package + deploy from CodeBuild).
    flags = {{"ecs": "container", "ec2": "artifact", "lambda": "lambda"}[deploy_target]}
    # No security-scan stage: AMF ships no third-party scanner. Add Amazon Inspector
    # (CI/CD scanning) to the buildspec if you need one (README "Security scanning").

    files, unresolved = [], set()
    for t in [ci, cd, dockerfile_tpl, *codedeploy]:
        if not t:
            continue
        content = _render(_select_blocks(_tpl(f"{provider}/{t['file']}"), flags), ctx)
        out = t.get("output_path", t["file"]).split("/")[-1]
        _stage(app_id, "cicd", out, content)
        files.append(out)
        unresolved |= set(re.findall(r"\{\{(\w+)\}\}", content))
    if not files:
        return {"status": "error", "app_id": app_id, "message": f"No CI/CD templates matched provider={provider}, language={lang}."}

    prefix = f"s3://{BUCKET}/applications/{app_id}/execution/cicd/"
    _record_execution(app_id, "cicd", prefix, files, {
        "provider": cicd_target, "unresolved_vars": sorted(unresolved), "notes": notes, "warnings": warnings,
        "repo_host": repo.get("host", ""), "repo_name": repo.get("name", ""), "provider_mismatch": bool(mismatch),
        "branch": ctx["migration_branch"], "trigger_branches": [ctx["branch"], ctx["migration_branch"]],
        "java_version": ctx["java_version"], "build_dir": ctx["build_dir"],
    })

    # Deterministically commit the staged CI/CD files to the migration branch and
    # record the sub-step status — no longer dependent on the LLM harness.
    commit = _commit_staged_to_repo(
        app_id, f"applications/{app_id}/execution/cicd/",
        lambda rel: (("Dockerfile" if ctx["build_dir"] in ("", ".") else f"{ctx['build_dir']}/Dockerfile")
                     if rel.split("/")[-1] == "Dockerfile" else _cicd_repo_path(rel.split("/")[-1], cicd_target)),
        "cicd-commit",
        f"chore(cicd): add generated CI/CD pipeline for {app_id}",
    )
    # Honest status: "committed" when push succeeded, else "staged". A provider that
    # cannot run on the repo host is surfaced separately (EXECUTION.provider_mismatch
    # + META.exec_cicd_warning) — the files exist, but nothing will execute them.
    _set_exec_status(app_id, "exec_cicd_status",
                     "committed" if commit.get("status") in ("committed", "no_change") else "staged")
    _set_exec_status(app_id, "exec_cicd_warning", mismatch)
    # The commit the deploy gate asks the human to review: CI/CD is
    # the last Execution writer, so the branch HEAD now holds IaC + CI fix + pipeline.
    head = _branch_head(repo, ctx["migration_branch"])
    if head:
        _set_exec_status(app_id, "exec_head_commit", head)
    return {"status": "staged", "app_id": app_id, "provider": cicd_target, "s3_prefix": prefix,
            "files": files, "unresolved_vars": sorted(unresolved), "notes": notes, "warnings": warnings,
            "provider_mismatch": bool(mismatch), "repo_host": repo.get("host", ""), "commit": commit,
            "message": "CI/CD pipeline generated (unit tests + coverage gate), staged to S3, and committed "
                       f"to {commit.get('branch') or ctx['migration_branch']}."
                       + (f" WARNING: {mismatch}" if mismatch else "")}


# ── Commit staged files into the app's CodeCommit repository (migration branch) ─

def _linked_codecommit_repo(app_id: str) -> tuple[str, str]:
    """Return (repo_name, default_branch) of the app's linked CodeCommit repo, or ("","")."""
    repo = _linked_repo(app_id)
    if repo.get("host") != "codecommit":
        return "", ""
    return repo["name"], repo["default_branch"]


def _cicd_repo_path(name: str, provider: str) -> str:
    """Map a staged CI/CD filename to a sensible path inside the repository."""
    low = name.lower()
    # CodeDeploy revision sources (the codepipeline buildspec reads cicd/codedeploy/).
    if low == "appspec.yml":
        return "cicd/codedeploy/appspec.yml"
    if low.endswith(".sh"):
        return f"cicd/codedeploy/scripts/{name}"
    del provider  # CodePipeline only: every file lives under cicd/ (buildspec = module var.buildspec)
    return f"cicd/{name}"


def _branch_head(repo: dict, branch: str) -> str:
    """Commit id at the tip of `branch` in the app's CodeCommit repo, '' when unknown."""
    if repo.get("host") != "codecommit" or not repo.get("name"):
        return ""
    try:
        return codecommit.get_branch(repositoryName=repo["name"], branchName=branch)["branch"]["commitId"]
    except Exception as e:  # noqa: BLE001 — advisory
        logger.warning("get_branch(%s, %s) failed: %s", repo.get("name"), branch, e)
        return ""


def _set_exec_status(app_id: str, field: str, value: str) -> None:
    """Persist an execution sub-step status on the app META (best-effort)."""
    try:
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression="SET #f = :v",
            ExpressionAttributeNames={"#f": field},
            ExpressionAttributeValues={":v": value},
        )
    except Exception as e:  # noqa: BLE001 — status is advisory, never block the commit
        logger.warning("set %s for %s failed: %s", field, app_id, e)


def _ensure_branch(repo_name: str, branch: str, base_branch: str) -> str | None:
    """Head commit of `branch`, creating it from `base_branch` when missing.

    Returns None when neither exists (empty repo: the first commit creates it)."""
    try:
        return codecommit.get_branch(repositoryName=repo_name, branchName=branch)["branch"]["commitId"]
    except codecommit.exceptions.BranchDoesNotExistException:
        pass
    try:
        base = codecommit.get_branch(repositoryName=repo_name, branchName=base_branch)["branch"]["commitId"]
    except codecommit.exceptions.BranchDoesNotExistException:
        return None
    try:
        codecommit.create_branch(repositoryName=repo_name, branchName=branch, commitId=base)
        logger.info("created %s@%s from %s (%s)", repo_name, branch, base_branch, base[:10])
    except Exception as e:  # noqa: BLE001 — e.g. role lacks codecommit:CreateBranch
        # create_commit with parentCommitId=<base head> is attempted next; if it
        # cannot create the branch either, the caller reports the error.
        logger.warning("create_branch(%s,%s) failed: %s", repo_name, branch, e)
    return base


def _commit_staged_to_repo(app_id, s3_prefix, to_repo_path, record_kind, default_msg,
                           commit_message=None, branch=None):
    """Commit files staged under an S3 prefix into the app's linked CodeCommit repo
    as a single commit on the MIGRATION branch (`migration/<app_id>`, created from
    the repo's default branch if missing) — never directly to the default branch.
    `to_repo_path(relative)` maps each staged key (relative to s3_prefix) to its
    path in the repo. Used by provision_infrastructure / generate_cicd
    (deterministic) and the standalone commit_* tools."""
    repo_name, default_branch = _linked_codecommit_repo(app_id)
    if not repo_name:
        return {"status": "skipped", "app_id": app_id,
                "message": "No CodeCommit repo linked to this app; cannot commit files. Seed the repository first."}
    branch = (branch or "").strip() or migration_branch(app_id)
    if branch in _DEFAULT_BRANCH_NAMES or branch == default_branch:
        return {"status": "rejected", "app_id": app_id, "repo": repo_name, "branch": branch,
                "message": f"Refusing to commit generated files to the default branch '{branch}'; "
                           f"they go to {migration_branch(app_id)} for review."}

    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix=s3_prefix).get("Contents", [])
    put_files = []
    for obj in listed:
        relative = obj["Key"].split(s3_prefix, 1)[-1]
        # The worker's dry-run outputs land next to the staged IaC; they are evidence, not code.
        if not relative or relative in _WORKER_RUN_OUTPUTS:
            continue
        body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
        put_files.append({"filePath": to_repo_path(relative), "fileContent": body, "fileMode": "NORMAL"})
    if not put_files:
        return {"status": "skipped", "app_id": app_id, "repo": repo_name,
                "message": f"No staged files found under {s3_prefix}."}

    try:
        parent = _ensure_branch(repo_name, branch, default_branch or "main")
    except Exception as e:
        return {"status": "error", "app_id": app_id, "repo": repo_name, "branch": branch,
                "message": f"Could not resolve/create branch {branch}: {e}"}

    commit_kwargs = {
        "repositoryName": repo_name, "branchName": branch,
        "authorName": GIT_AUTHOR_NAME, "email": GIT_AUTHOR_EMAIL,
        "commitMessage": commit_message or default_msg,
        "putFiles": put_files,
    }
    if parent:
        commit_kwargs["parentCommitId"] = parent

    try:
        resp = codecommit.create_commit(**commit_kwargs)
    except (codecommit.exceptions.NoChangeException, codecommit.exceptions.SameFileContentException):
        return {"status": "no_change", "app_id": app_id, "repo": repo_name, "branch": branch,
                "message": "Files already committed and up to date in the repository."}
    except Exception as e:
        return {"status": "error", "app_id": app_id, "repo": repo_name, "branch": branch,
                "message": f"CodeCommit create_commit failed: {e}"}

    commit_id = resp.get("commitId", "")
    committed = [f["filePath"] for f in put_files]
    _record_execution(
        app_id, record_kind, f"codecommit://{repo_name}/{branch}", committed,
        {"repo": repo_name, "branch": branch, "base_branch": default_branch, "commit_id": commit_id, "status": "committed"},
    )
    return {"status": "committed", "app_id": app_id, "repo": repo_name, "branch": branch,
            "base_branch": default_branch, "commit_id": commit_id, "files": committed,
            "message": f"Committed {len(committed)} file(s) to {repo_name}@{branch} ({commit_id[:10]})."}


@registry.tool(
    "commit_cicd_to_repo",
    description="Commit the CI/CD files previously staged to S3 (by generate_cicd) into the application's linked "
                "CodeCommit repository as a single commit on the migration branch. Call AFTER generate_cicd.",
    schema={
        "app_id": {"type": "string", "required": True},
        "branch": {"type": "string", "description": "Target branch (defaults to migration/<app_id>; default branches are refused)"},
        "commit_message": {"type": "string"},
    },
    infer=lambda a: "app_id" in a and str(a.get("_action", "")) == "commit_cicd_to_repo",
)
def commit_cicd_to_repo(args):
    app_id = args["app_id"]
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    provider = (meta.get("cicd_target") or "").lower()
    return _commit_staged_to_repo(
        app_id, f"applications/{app_id}/execution/cicd/",
        lambda rel: _cicd_repo_path(rel.split("/")[-1], provider), "cicd-commit",
        f"chore(cicd): add generated CI/CD pipeline for {app_id}",
        args.get("commit_message"), args.get("branch"),
    )


def _launch_ecs_task(job_id, app_id, mode, td_name, target_version="", task_token="", extra_env=None):  # nosec B107 — empty default = no SFN callback
    """Launch an ECS Fargate task and record the job in DynamoDB."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    item = {
        "PK": f"JOB#{job_id}", "SK": "META",
        "GSI1PK": "TYPE#JOB", "GSI1SK": job_id,
        "job_id": job_id, "app_id": app_id, "mode": mode,
        "td_name": td_name, "status": "PENDING",
        "created_at": now, "target_version": target_version,
    }
    if task_token:
        item["task_token"] = task_token
    table.put_item(Item=item)

    # Fail loudly on misconfiguration so the SFN callback surfaces the error.
    if not ECS_SUBNETS or not ECS_CLUSTER or not ECS_TASK_DEF:
        table.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "FAILED"},
        )
        if task_token:
            _fail_task(task_token, "ECSNotConfigured", "ECS cluster/task-def/subnets not configured for ATX")
        return {"job_id": job_id, "status": "FAILED",
                "message": "ECS not configured. ATX cannot run.", "app_id": app_id, "td_name": td_name}

    env = [
        {"name": "MODE", "value": mode},
        {"name": "APP_ID", "value": app_id},
        {"name": "ATX_CODE_ANALYSIS_TD_NAME", "value": td_name},
        {"name": "JOB_ID", "value": job_id},
        {"name": "ATX_SHELL_TIMEOUT", "value": ATX_SHELL_TIMEOUT},
        {"name": "AWS_REGION", "value": REGION},
        # Force the worker into this environment's data plane (the shared task
        # definition may default elsewhere); the worker reads these env vars.
        {"name": "DYNAMODB_TABLE", "value": TABLE},
        {"name": "S3_BUCKET", "value": BUCKET},
        {"name": "ATX_S3_OUTPUT_BUCKET", "value": BUCKET},
        {"name": "S3_OUTPUT_BUCKET", "value": BUCKET},
        {"name": "PROJECT_PREFIX", "value": PROJECT_PREFIX},
        {"name": "SFN_CALLBACK", "value": "1" if task_token else "0"},
    ]
    if task_token:
        env.append({"name": "SFN_TASK_TOKEN", "value": task_token})
    if target_version:
        env.append({"name": "TARGET_VERSION", "value": target_version})
    for k, v in (extra_env or {}).items():
        env.append({"name": k, "value": v})

    repos_resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("REPO#")
    )
    repo_urls = [f"{r['url']}@{r.get('branch', 'main')}" for r in repos_resp.get("Items", []) if r.get("url")]
    if repo_urls:
        env.append({"name": "REPO_URLS", "value": ",".join(repo_urls)})

    try:
        # ECS RunTask on Fargate is intermittently flaky: it may raise a
        # ServerException ("Internal Error")/ThrottlingException, OR return NO
        # task with a populated `failures` array (transient capacity/ENI
        # placement). Both were previously fatal — the empty-tasks case was
        # silently recorded as RUNNING with an empty task_arn, so the
        # waitForTaskToken never got a callback and LaunchATX/LaunchTransformation
        # hung until timeout. Retry a few times with backoff; if the task still
        # cannot be placed, raise so the except below fails the callback fast.
        task_arn = ""
        _last = None
        last_attempt = RUNTASK_MAX_ATTEMPTS - 1
        for _attempt in range(RUNTASK_MAX_ATTEMPTS):
            try:
                resp = ecs.run_task(
                    cluster=ECS_CLUSTER,
                    taskDefinition=ECS_TASK_DEF,
                    launchType="FARGATE",
                    # Worker tasks carry the task definition's tags (Terraform default_tags).
                    propagateTags="TASK_DEFINITION",
                    enableECSManagedTags=True,
                    overrides={"containerOverrides": [{"name": WORKER_CONTAINER_NAME, "environment": env}]},
                    networkConfiguration={"awsvpcConfiguration": {"subnets": ECS_SUBNETS, "assignPublicIp": ECS_ASSIGN_PUBLIC_IP}},
                )
            except ClientError as ce:
                _code = ce.response.get("Error", {}).get("Code", "")
                _last = f"{_code}: {ce}"
                # Retry only transient server-side/throttle errors; a
                # ClientException (bad params/task-def) is permanent — re-raise.
                if _code in ("ServerException", "ThrottlingException") and _attempt < last_attempt:
                    logger.warning("run_task attempt %d/%d raised %s; retrying",
                                   _attempt + 1, RUNTASK_MAX_ATTEMPTS, _code)
                    time.sleep(RUNTASK_BACKOFF_BASE_SECONDS * (2 ** _attempt))
                    continue
                raise
            if resp.get("tasks"):
                task_arn = resp["tasks"][0]["taskArn"]
                break
            # No task placed (capacity/ENI): capture failures and retry.
            _last = f"no task placed; failures={resp.get('failures')}"
            logger.warning("run_task attempt %d/%d placed no task; failures=%s",
                           _attempt + 1, RUNTASK_MAX_ATTEMPTS, resp.get("failures"))
            if _attempt < last_attempt:
                time.sleep(RUNTASK_BACKOFF_BASE_SECONDS * (2 ** _attempt))
        if not task_arn:
            raise RuntimeError(f"ECS RunTask placed no task after {RUNTASK_MAX_ATTEMPTS} attempts ({_last})")
        table.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression="SET #s = :s, task_arn = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "RUNNING", ":t": task_arn},
        )
        return {"job_id": job_id, "status": "RUNNING", "task_arn": task_arn, "app_id": app_id}
    except Exception as e:
        table.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression="SET #s = :s, error_msg = :e",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "FAILED", ":e": str(e)},
        )
        if task_token:
            _fail_task(task_token, "ECSRunTaskFailed", str(e))
        return {"job_id": job_id, "status": "FAILED", "error": str(e)}


# ── Validation phase: deterministic checks (every app, same rules) ─────────────
# Replaces the Validation Harness prompt, which reported smoke/security/
# performance results it never ran. Static checks gate on what Execution
# produced; live checks run against the deployed stack when it exists and are
# reported as "not_deployed" (warning, not failure) before the first deploy.

def _latest_item(app_id: str, prefix: str) -> dict:
    try:
        items = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(prefix),
            ScanIndexForward=False, Limit=1,
        ).get("Items", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("%s read failed for %s: %s", prefix, app_id, e)
        return {}
    return items[0] if items else {}


def _check(checks: list, name: str, result: str, detail: str, evidence: dict | None = None) -> None:
    row = {"check": name, "result": result, "detail": detail}
    if evidence:
        row["evidence"] = evidence  # request/response behind an L1–L3 verdict (Validation screen drill-down)
    checks.append(row)


# cicd_target -> staged name of its CI file (templates.json output_path basename).
_CI_STAGED_FILE = {"codepipeline": "buildspec.yml"}


def _static_checks(app_id: str, meta: dict, checks: list) -> None:
    infra = _latest_item(app_id, "EXECUTION#infra-validate#")
    status = str(infra.get("status") or "missing")
    findings = infra.get("exposure_findings") or []
    _check(checks, "iac_validate_plan", "pass" if status in ("validated", "exposed") else "fail",
           f"terraform validate+plan status={status}")
    if status in ("validated", "exposed"):
        waived = bool(infra.get("exposure_waived"))
        _check(checks, "iac_exposure_policy", "pass" if not findings else ("warn" if waived else "fail"),
               "no exposure findings" if not findings else
               ("WAIVED (expose_publicly): " if waived else "") + "; ".join(
                   f"{f.get('address')}: {f.get('detail')}" for f in findings[:10]))
    # The generated IaC must build the compute the user approved (a VPC-only stack
    # for an unsupported compute once passed every check).
    approved, compute_error = _resolve_compute(meta)
    iac = _latest_item(app_id, "EXECUTION#iac#")
    module = _COMPUTE_MODULES.get(approved, "")
    if compute_error:
        _check(checks, "compute_module_present", "fail", compute_error)
    elif not iac:
        _check(checks, "compute_module_present", "fail", "no generated IaC record (provision_infrastructure did not run)")
    else:
        built = str(iac.get("target_compute") or "")
        ok = module in (iac.get("modules") or []) and built == approved
        _check(checks, "compute_module_present", "pass" if ok else "fail",
               f"approved target_compute={approved}; IaC built {built or '-'} with modules "
               f"{', '.join(iac.get('modules') or []) or '-'}")
    # CI/CD: the committed CI file must carry its blocking gates.
    cicd_status = str(meta.get("exec_cicd_status") or "missing")
    _check(checks, "cicd_committed", "pass" if cicd_status in ("committed", "no_change") else "fail",
           f"exec_cicd_status={cicd_status}")
    ci_file = _CI_STAGED_FILE.get(str(meta.get("cicd_target") or "").lower(), "buildspec.yml")
    try:
        body = s3.get_object(Bucket=BUCKET, Key=f"applications/{app_id}/execution/cicd/{ci_file}")["Body"].read().decode()
    except Exception:  # noqa: BLE001 — nothing staged yet
        body = ""
    if body:
        threshold = _num(meta.get("coverage_threshold"), COVERAGE_THRESHOLD_DEFAULT, 0, 100)
        reason = str(meta.get("coverage_threshold_reason") or "").strip()
        _check(checks, "cicd_coverage_gate",
               "pass" if threshold > 0 else ("warn" if reason else "fail"),
               f"coverage threshold {threshold}%" if threshold > 0 else
               (f"coverage gate opted out: {reason}" if reason else "coverage gate disabled with no recorded reason"))
    # Code transformation outcome (honest per-step status from the workbench).
    plan = _latest_item(app_id, "PLAN#")
    if plan:
        bad = [s.get("td_name") for s in plan.get("steps") or [] if s.get("status") in ("failed", "error")]
        partial = [s.get("td_name") for s in plan.get("steps") or [] if s.get("status") == "partial"]
        _check(checks, "code_transformation", "fail" if bad else ("warn" if partial else "pass"),
               f"plan {plan.get('status')}; failed={bad or '-'} partial/unverified={partial or '-'}")


def _live_checks(app_id: str, meta: dict, checks: list) -> None:
    """Live checks for the approved compute — skipped (not_deployed) until it exists."""
    if not DEPLOY_ENABLED:
        _check(checks, "live_deployment", "skip", "deploy disabled (enable_deploy = false): static checks only")
        return
    compute, _ = _resolve_compute(meta)
    if compute == "ecs_fargate":
        if _ecs_live_checks(app_id, meta, checks):
            _alb_checks(app_id, meta, checks)
    elif compute == "ec2":
        if _ec2_live_checks(app_id, checks):
            _alb_checks(app_id, meta, checks)
    elif compute == "lambda":
        _lambda_live_checks(app_id, meta, checks)
    else:
        _check(checks, "live_deployment", "skip", f"no live checks for target_compute={compute or '(not set)'}")
        return
    _db_live_checks(app_id, meta, checks)


def _db_live_checks(app_id: str, meta: dict, checks: list) -> None:
    """The approved database exists and is private: RDS/Aurora available, not public, encrypted;
    DynamoDB table ACTIVE. Runs only once the compute is deployed (same apply)."""
    target_db = str(meta.get("target_database") or "").lower()
    if any(c["check"] == "live_deployment" and c["result"] == "not_deployed" for c in checks):
        return
    if target_db in _RDS_ENGINES:
        rds = boto3.client("rds", region_name=REGION)
        ident = f"{app_id.lower()}-db"
        try:
            if target_db.startswith("aurora_"):
                db = rds.describe_db_clusters(DBClusterIdentifier=ident)["DBClusters"][0]
                status, public = db.get("Status"), False
            else:
                db = rds.describe_db_instances(DBInstanceIdentifier=ident)["DBInstances"][0]
                status, public = db.get("DBInstanceStatus"), bool(db.get("PubliclyAccessible"))
            ok = status == "available" and not public and bool(db.get("StorageEncrypted"))
            _check(checks, "database_live", "pass" if ok else "fail",
                   f"{ident}: status={status} public={public} encrypted={bool(db.get('StorageEncrypted'))}")
        except Exception as e:  # noqa: BLE001
            _check(checks, "database_live", "fail", f"{target_db} {ident} not readable: {e}")
    elif target_db == "dynamodb":
        name = str(meta.get("dynamodb_table_name") or f"{app_id}-table")
        try:
            t = boto3.client("dynamodb", region_name=REGION).describe_table(TableName=name)["Table"]
            enc = (t.get("SSEDescription") or {}).get("Status") in ("ENABLED", "ENABLING", None)
            _check(checks, "database_live", "pass" if t.get("TableStatus") == "ACTIVE" and enc else "fail",
                   f"DynamoDB {name}: {t.get('TableStatus')}, {t.get('ItemCount', 0)} item(s)")
        except Exception as e:  # noqa: BLE001
            _check(checks, "database_live", "fail", f"DynamoDB table {name} not readable: {e}")


def _pipeline_check(app_id: str, checks: list) -> None:
    """CodePipeline: latest execution."""
    cp = boto3.client("codepipeline", region_name=REGION)
    try:
        ex = (cp.list_pipeline_executions(pipelineName=f"{app_id}-pipeline", maxResults=1)
              .get("pipelineExecutionSummaries") or [{}])[0]
        st = ex.get("status", "none")
        _check(checks, "pipeline_last_run", "pass" if st == "Succeeded" else ("warn" if st == "InProgress" else "fail"),
               f"{app_id}-pipeline last execution {st}")
    except Exception as e:  # noqa: BLE001
        _check(checks, "pipeline_last_run", "warn", f"pipeline not readable: {e}")


def _ecs_steady(svc: dict) -> bool:
    deps = svc.get("deployments") or []
    return len(deps) == 1 and deps[0].get("rolloutState") in ("COMPLETED", None)


def _wait_for_ecs_rollout(cluster: str, service: str, svc: dict) -> dict:
    """Validation runs right after Deploy, often mid-rollout (the deploy forces a new ECS deployment).
    Give a rollout up to ECS_ROLLOUT_WAIT_SECONDS to finish before judging; a FAILED rollout ends the wait."""
    deadline = time.monotonic() + _ECS_ROLLOUT_WAIT_SECONDS
    while not _ecs_steady(svc) and time.monotonic() < deadline:
        if any(d.get("rolloutState") == "FAILED" for d in svc.get("deployments") or []):
            break
        time.sleep(_ECS_ROLLOUT_POLL_SECONDS)
        try:
            svc = (ecs.describe_services(cluster=cluster, services=[service]).get("services") or [svc])[0]
        except Exception as e:  # noqa: BLE001
            logger.info("ECS describe during rollout wait failed for %s: %s", service, e)
            break
    return svc


def _ecs_live_checks(app_id: str, meta: dict, checks: list) -> bool:
    cluster, service = f"{app_id}-cluster", f"{app_id}-service"
    try:
        svc = (ecs.describe_services(cluster=cluster, services=[service]).get("services") or [{}])[0]
    except Exception as e:  # noqa: BLE001 — cluster not created yet
        svc = {}
        logger.info("ECS describe failed for %s (not deployed?): %s", app_id, e)
    if not svc or svc.get("status") != "ACTIVE":
        _check(checks, "live_deployment", "not_deployed", f"ECS service {cluster}/{service} not found — deploy, then re-validate")
        return False
    _pipeline_check(app_id, checks)
    svc = _wait_for_ecs_rollout(cluster, service, svc)
    running, desired = svc.get("runningCount", 0), svc.get("desiredCount", 0)
    deps = svc.get("deployments") or []
    steady = _ecs_steady(svc)
    _check(checks, "ecs_service_steady", "pass" if running == desired and desired > 0 and steady else "fail",
           f"running {running}/{desired}, deployments={len(deps)} rollout={deps[0].get('rolloutState') if deps else '-'}")
    try:
        td = ecs.describe_task_definition(taskDefinition=svc["taskDefinition"])["taskDefinition"]
        arch = (td.get("runtimePlatform") or {}).get("cpuArchitecture", "X86_64")
        want = _cpu_architecture(meta)
        _check(checks, "ecs_cpu_architecture", "pass" if arch == want else "fail", f"task {arch}, expected {want}")
    except Exception as e:  # noqa: BLE001
        _check(checks, "ecs_cpu_architecture", "warn", f"task definition not readable: {e}")
    if any(n.get("awsvpcConfiguration", {}).get("assignPublicIp") == "ENABLED"
           for n in [svc.get("networkConfiguration") or {}]):
        _check(checks, "ecs_no_public_ip", "fail", "service assigns public IPs to tasks")
    else:
        _check(checks, "ecs_no_public_ip", "pass", "tasks have no public IP")
    return True


def _ec2_live_checks(app_id: str, checks: list) -> bool:
    asg = boto3.client("autoscaling", region_name=REGION)
    try:
        group = (asg.describe_auto_scaling_groups(AutoScalingGroupNames=[f"{app_id}-asg"]).get("AutoScalingGroups") or [{}])[0]
    except Exception as e:  # noqa: BLE001 — not deployed yet
        group = {}
        logger.info("ASG describe failed for %s (not deployed?): %s", app_id, e)
    if not group:
        _check(checks, "live_deployment", "not_deployed", f"Auto Scaling group {app_id}-asg not found — deploy, then re-validate")
        return False
    _pipeline_check(app_id, checks)
    instances = group.get("Instances") or []
    in_service = sum(1 for i in instances if i.get("LifecycleState") == "InService" and i.get("HealthStatus") == "Healthy")
    desired = group.get("DesiredCapacity", 0)
    _check(checks, "ec2_asg_in_service", "pass" if desired > 0 and in_service >= desired else "fail",
           f"{in_service}/{desired} instances InService+Healthy")
    try:
        ec2c = boto3.client("ec2", region_name=REGION)
        ids = [i["InstanceId"] for i in instances]
        res = ec2c.describe_instances(InstanceIds=ids)["Reservations"] if ids else []
        public = [i["InstanceId"] for r in res for i in r.get("Instances", []) if i.get("PublicIpAddress")]
        _check(checks, "ec2_no_public_ip", "fail" if public else "pass",
               "public IPs on: " + ", ".join(public) if public else "instances have no public IP")
    except Exception as e:  # noqa: BLE001
        _check(checks, "ec2_no_public_ip", "warn", f"instances not readable: {e}")
    return True


def _lambda_live_checks(app_id: str, meta: dict, checks: list) -> None:
    lam = boto3.client("lambda", region_name=REGION)
    # Multi-function apps deploy {app_id}-<key> per function — checking only
    # {app_id}-function reported a deployed app as not_deployed.
    names = [f"{app_id}-{k}" for k in _lambda_functions(meta)] or [f"{app_id}-function"]
    cfgs = {}
    for name in names:
        try:
            cfgs[name] = lam.get_function(FunctionName=name)["Configuration"]
        except Exception as e:  # noqa: BLE001 — not deployed yet
            logger.info("Lambda get_function failed for %s (not deployed?): %s", name, e)
    if not cfgs:
        _check(checks, "live_deployment", "not_deployed",
               f"Lambda function(s) {', '.join(names)} not found — deploy, then re-validate")
        return
    _pipeline_check(app_id, checks)
    missing = [n for n in names if n not in cfgs]
    bad = [f"{n}: state={c.get('State')} last_update={c.get('LastUpdateStatus')}" for n, c in cfgs.items()
           if not (c.get("State") == "Active" and c.get("LastUpdateStatus") in ("Successful", None))]
    _check(checks, "lambda_function_active", "fail" if missing or bad else "pass",
           "; ".join([f"{n}: not found" for n in missing] + bad) or f"{len(cfgs)} function(s) Active")
    try:
        undeployed = [n for n in cfgs if not [v for v in lam.list_versions_by_function(FunctionName=n).get("Versions", [])
                                              if v.get("Version") != "$LATEST"]]
        _check(checks, "lambda_code_deployed", "fail" if undeployed else "pass",
               ("only the Terraform placeholder package (no CI/CD deploy published a version): " + ", ".join(undeployed))
               if undeployed else f"every function has a published version ({len(cfgs)})")
    except Exception as e:  # noqa: BLE001
        _check(checks, "lambda_code_deployed", "warn", f"versions not readable: {e}")
    try:
        apis = boto3.client("apigatewayv2", region_name=REGION).get_apis().get("Items", [])
        api = next((x for x in apis if x.get("Name") == f"{app_id}-api"), None)
        _check(checks, "lambda_api_present", "pass" if api else "fail",
               f"HTTP API {api['ApiEndpoint']}" if api else f"HTTP API {app_id}-api not found")
    except Exception as e:  # noqa: BLE001
        _check(checks, "lambda_api_present", "warn", f"API Gateway not readable: {e}")


def _alb_checks(app_id: str, meta: dict, checks: list) -> None:
    elb = boto3.client("elbv2", region_name=REGION)
    ec2c = boto3.client("ec2", region_name=REGION)
    try:
        lb = elb.describe_load_balancers(Names=[f"{app_id}-alb"])["LoadBalancers"][0]
        public = lb.get("Scheme") == "internet-facing"
        waived = _truthy(meta.get("expose_publicly")) is True
        _check(checks, "alb_not_public", "fail" if public and not waived else ("warn" if public else "pass"),
               f"ALB scheme {lb.get('Scheme')}")
        sgs = ec2c.describe_security_groups(GroupIds=lb.get("SecurityGroups") or [])["SecurityGroups"]
        open_rules = [f"{g['GroupId']}:{p.get('FromPort')}" for g in sgs for p in g.get("IpPermissions", [])
                      if any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", []))
                      or any(r.get("CidrIpv6") == "::/0" for r in p.get("Ipv6Ranges", []))]
        _check(checks, "alb_no_open_ingress", "fail" if open_rules else "pass",
               "open to the internet: " + ", ".join(open_rules) if open_rules else "no 0.0.0.0/0 or ::/0 ingress")
        # Every ingress CIDR must be the VPC or one the app approved (allowed_ingress_cidrs).
        approved = {str(meta.get("vpc_cidr") or "")} | {str(c) for c in (meta.get("allowed_ingress_cidrs") or [])}
        seen = sorted({r.get("CidrIp") for g in sgs for p in g.get("IpPermissions", []) for r in p.get("IpRanges", [])})
        extra = [c for c in seen if c not in approved]
        _check(checks, "alb_ingress_scope", "fail" if extra else "pass",
               f"unapproved ingress CIDRs: {', '.join(extra)}" if extra else f"ingress only from {', '.join(seen) or '(none)'}")
        tg = elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
        health = [t["TargetHealth"]["State"] for g in tg
                  for t in elb.describe_target_health(TargetGroupArn=g["TargetGroupArn"])["TargetHealthDescriptions"]]
        # A draining target is the old task/instance leaving during a rolling deploy, not a failure:
        # judge only the targets that are serving (or meant to serve) traffic.
        active = [h for h in health if h != "draining"]
        healthy = active.count("healthy")
        draining = len(health) - len(active)
        _check(checks, "alb_targets_healthy", "pass" if active and healthy == len(active) else "fail",
               f"{healthy}/{len(active)} targets healthy ({', '.join(sorted(set(active))) or 'none registered'})"
               + (f"; {draining} draining (ignored)" if draining else ""))
    except Exception as e:  # noqa: BLE001
        _check(checks, "alb_checks", "fail", f"ALB {app_id}-alb not readable: {e}")


@registry.tool(
    "validate_deployment",
    description=("Deterministic Validation phase: checks Execution's outputs (the approved compute's module was "
                 "generated, terraform validate/plan + exposure policy, CI/CD security & coverage gates, "
                 "transformation outcome) and, when deployed, the live stack for the approved compute (ECS service "
                 "steady + architecture / EC2 ASG InService / Lambda active + a deployed version + HTTP API; pipeline "
                 "run, no public IPs, internal ALB, no open ingress, healthy targets). Stores analysis "
                 "validation-results and META.validation_status."),
    schema={"app_id": {"type": "string", "required": True}},
    infer=lambda a: str(a.get("_action", "")) == "validate_deployment",
)
def validate_deployment(args):
    app_id = args["app_id"]
    if not args.get("_rerun"):
        # The workflow's Validation step: show it on the pipeline (the record otherwise keeps
        # current_phase=deploy from the execution_review approval until the next gate — APP-TE
        # 2026-10-27 showed Execution "Running" during Validation). A screen re-run doesn't move it.
        try:
            table.update_item(Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"},
                              UpdateExpression="SET current_phase = :p, current_step = :s",
                              ConditionExpression="attribute_exists(PK)",
                              ExpressionAttributeValues={":p": "validation", ":s": "validation_running"})
        except Exception as e:  # noqa: BLE001 — cosmetic; never block Validation
            logger.info("workflow phase not updated for %s: %s", app_id, e)
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    checks: list = []
    _static_checks(app_id, meta, checks)
    _live_checks(app_id, meta, checks)
    # L1 smoke / L2 contract / L3 data round-trip against the running app
    # — only once L0 found the stack deployed.
    deployed = not any(c["check"] == "live_deployment" and c["result"] in ("not_deployed", "skip") for c in checks)
    if deployed and not PROJECT_PREFIX:
        # Fail closed: the probe Lambda's name must match the IAM scope `${prefix}-probe-*`.
        _check(checks, "L1:app_checks", "fail",
               "PROJECT_PREFIX is not configured on the tool Lambda — application checks cannot run")
    elif deployed:
        try:
            import validation_probe  # noqa: PLC0415 — packaged next to this module

            validation_probe.run_app_checks(
                app_id, meta, checks, region=REGION, prefix=PROJECT_PREFIX,
                probe_role_arn=VALIDATION_PROBE_ROLE_ARN, s3=s3, table=table, codecommit=codecommit, check=_check)
        except Exception as e:  # noqa: BLE001 — app checks never break the platform verdict
            _check(checks, "L1:app_checks", "warn", f"application checks did not run: {e}")
    failed = [c for c in checks if c["result"] == "fail"]
    pending = [c for c in checks if c["result"] in ("warn", "not_deployed")]
    verdict = "fail" if failed else ("pass_with_warnings" if pending else "pass")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result = {"verdict": verdict, "checks": checks, "validated_at": now,
              "failed": [c["check"] for c in failed], "warnings": [c["check"] for c in pending]}
    md = "# Validation Results — " + app_id + f"\n\n**Verdict:** {verdict}\n\n| Check | Result | Detail |\n|---|---|---|\n" + \
        "\n".join(f"| {c['check']} | {c['result']} | {str(c['detail']).replace('|', '/')} |" for c in checks)
    table.put_item(Item={
        "PK": f"APP#{app_id}", "SK": f"ANALYSIS#validation-results#{now}",
        "GSI1PK": "TYPE#ANALYSIS", "GSI1SK": f"{app_id}#validation-results",
        "app_id": app_id, "analysis_type": "validation-results",
        "result": result, "data": {"report_md": md}, "updated_at": now,
    })
    _set_exec_status(app_id, "validation_status", verdict)
    if BUCKET:
        s3.put_object(Bucket=BUCKET, Key=f"applications/{app_id}/validation/validation-results.json",
                      Body=json.dumps(result, default=str).encode(), ContentType="application/json")
    return json_safe({"status": "ok", "app_id": app_id, **result,
                      "failed_detail": [f"{c['check']}: {c['detail']}" for c in failed]})
