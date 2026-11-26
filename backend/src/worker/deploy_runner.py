"""Deploy / tear-down — worker MODE=infra with INFRA_ACTION=apply|destroy (spec 17).

apply (Deploy state, after the human approved `deploy_review` for one commit):
  a. the migration branch HEAD must still be DEPLOY_COMMIT, else `branch_moved`
     (the human reviewed a different tree — re-plan and ask again);
  b. `infra/` is fetched from the repo AT DEPLOY_COMMIT — the reviewed version,
     never the S3 staging copy;
  c. DEPLOY_ROLE_ARN is assumed and its credentials reach ONLY the terraform
     subprocess env (and a boto3 Session for the pipeline wait) — never this
     process's os.environ. init uses the real S3 backend (the target account's
     state bucket), plan -out, the same exposure policy as the plan step, then
     apply of exactly that plan;
  d. waits for `<app_id>-pipeline` to run DEPLOY_COMMIT (starts it once if no run
     appears within 3 min) and maps the result;
  e. records META exec_deploy_status / exec_deploy_error + ANALYSIS#deploy and
     ALWAYS calls back SendTaskSuccess with the status (Check_Deploy routes on
     it); only a crash sends SendTaskFailure "DeployFailed".

destroy (explicit "Tear down"): fetch `infra/` at the branch HEAD, assume the role,
init + destroy -auto-approve; META exec_deploy_status destroyed|destroy_failed;
no callback (no task token).
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed
import tempfile
import time
from pathlib import Path

import boto3

from backend.src.config import settings
from backend.src.core.exposure_policy import evaluate as evaluate_exposure
from backend.src.worker.argsafe import confined
from backend.src.worker.branches import migration_branch
from backend.src.worker.config import (
    APP_ID,
    ATX_S3_OUTPUT_BUCKET,
    AWS_REGION,
    DEPLOY_COMMIT,
    DEPLOY_PIPELINE_POLL_SECONDS,
    DEPLOY_PIPELINE_START_GRACE_SECONDS,
    DEPLOY_PIPELINE_TIMEOUT_SECONDS,
    DEPLOY_ROLE_ARN,
    RUN_ID,
    logger,
    read_full_app,
    s3_client,
)

_CMD_TIMEOUT = max(settings.terraform_cmd_timeout_seconds, settings.deploy_cmd_timeout_seconds)  # apply/destroy can take long (RDS, NAT)
_PIPELINE_TIMEOUT = DEPLOY_PIPELINE_TIMEOUT_SECONDS
_PIPELINE_START_GRACE = DEPLOY_PIPELINE_START_GRACE_SECONDS
_POLL = DEPLOY_PIPELINE_POLL_SECONDS
_DEPLOY_PREFIX = f"applications/{APP_ID}/execution/deploy/"
_CREDS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
              "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
              "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN")


class DeployError(Exception):
    """An expected deploy outcome other than success: (status, message)."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ── repo + credentials ──────────────────────────────────────────────────────

def primary_codecommit_repo(app: dict) -> str:
    for r in app.get("repos") or []:
        url = str(r.get("url") or "").strip()
        if "git-codecommit." in url or r.get("provider") == "codecommit":
            return url.rstrip("/").split("/")[-1].replace(".git", "")
    raise DeployError("error", "no CodeCommit repository linked (CodePipeline sources CodeCommit)")


def deploy_branch(app: dict, app_id: str) -> str:
    return str(app.get("deploy_branch") or "").strip() or migration_branch(app_id)


def branch_head(cc, repo: str, branch: str) -> str:
    try:
        return cc.get_branch(repositoryName=repo, branchName=branch)["branch"]["commitId"]
    except Exception as exc:  # noqa: BLE001
        raise DeployError("error", f"cannot read {repo}@{branch}: {exc}") from exc


def fetch_infra(cc, repo: str, commit: str, dest: Path, folder: str = "infra") -> int:
    """Copy `<folder>/` of repo@commit into dest (recursively). Returns the file count."""
    count = 0

    def walk(path: str):
        nonlocal count
        f = cc.get_folder(repositoryName=repo, folderPath=path, commitSpecifier=commit)
        for item in f.get("files", []):
            rel = item["absolutePath"][len(folder):].lstrip("/")
            blob = cc.get_file(repositoryName=repo, filePath=item["absolutePath"], commitSpecifier=commit)
            try:  # a repo path must not write outside the workdir
                out = confined(dest, rel)
            except ValueError as exc:
                raise DeployError("error", f"refusing {item['absolutePath']!r}: {exc}") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(blob["fileContent"])
            count += 1
        for sub in f.get("subFolders", []):
            walk(sub["absolutePath"])

    try:
        walk(folder)
    except Exception as exc:  # noqa: BLE001
        raise DeployError("error", f"cannot read {folder}/ at {commit[:8]}: {exc}") from exc
    if not count:
        raise DeployError("error", f"no {folder}/ files at {commit[:8]} — was the IaC committed?")
    return count


def assume_deploy_role(sts, role_arn: str, app_id: str) -> dict:
    if not role_arn:
        raise DeployError("error", "DEPLOY_ROLE_ARN not set")
    try:
        # The worker is itself an assumed role: AWS caps chained sessions at 1 hour.
        c = sts.assume_role(RoleArn=role_arn, RoleSessionName=f"amf-deploy-{app_id}"[:64],
                            DurationSeconds=settings.deploy_role_session_seconds)["Credentials"]
    except Exception as exc:  # noqa: BLE001
        raise DeployError("error", f"cannot assume {role_arn}: {exc}") from exc
    return {"AWS_ACCESS_KEY_ID": c["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"],
            "AWS_SESSION_TOKEN": c["SessionToken"]}


def terraform_env(creds: dict, region: str) -> dict:
    """A fresh env for terraform: the worker's own credentials removed, the deploy role's added."""
    env = {k: v for k, v in os.environ.items() if k not in _CREDS_ENV}
    env.update(creds)
    env.update({"AWS_REGION": region, "AWS_DEFAULT_REGION": region, "TF_IN_AUTOMATION": "1"})
    return env


def deploy_session(creds: dict, region: str):
    return boto3.Session(aws_access_key_id=creds["AWS_ACCESS_KEY_ID"],
                         aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"],
                         aws_session_token=creds["AWS_SESSION_TOKEN"], region_name=region)


def _run(cmd: list[str], cwd: Path, env: dict, runner=None) -> tuple[int, str]:
    runner = runner or subprocess.run
    logger.info("terraform: %s", " ".join(cmd))
    try:
        p = runner(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=_CMD_TIMEOUT)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {_CMD_TIMEOUT}s: {' '.join(cmd)}"


def _tail(text: str, n: int = 40) -> str:
    return "\n".join((text or "").strip().splitlines()[-n:])


# ── pipeline ────────────────────────────────────────────────────────────────

def _failure_summary(cp, pipeline: str, execution_id: str) -> str:
    try:
        acts = cp.list_action_executions(pipelineName=pipeline, filter={"pipelineExecutionId": execution_id})
        failed = [a for a in acts.get("actionExecutionDetails", []) if a.get("status") == "Failed"]
        if failed:
            a = failed[0]
            res = (a.get("output") or {}).get("executionResult") or {}
            return (f"{a.get('stageName')}/{a.get('actionName')} failed: "
                    f"{res.get('externalExecutionSummary') or a.get('status')}")[:1000]
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_action_executions failed: %s", exc)
    try:
        ex = cp.get_pipeline_execution(pipelineName=pipeline, pipelineExecutionId=execution_id)["pipelineExecution"]
        return f"execution {execution_id} {ex.get('status')}: {ex.get('statusSummary') or ''}"[:1000]
    except Exception:  # noqa: BLE001
        return f"execution {execution_id} failed"


def wait_for_pipeline(cp, pipeline: str, commit: str, *, sleep=time.sleep, clock=time.monotonic,
                      timeout: int = _PIPELINE_TIMEOUT, start_grace: int = _PIPELINE_START_GRACE) -> dict:
    """{'status': deployed|pipeline_failed, 'pipeline_execution_id', 'message'} for the run of `commit`."""
    start, started = clock(), False
    # Runs of this commit that had already FAILED before this deploy are stale: re-deploying the
    # same commit (e.g. after a transient failure) must start a fresh run, not report the old
    # failure again (APP-HE 2026-10-27: attempt 2 failed instantly on attempt 1's run).
    stale: set | None = None
    while clock() - start < timeout:
        try:
            runs = cp.list_pipeline_executions(pipelineName=pipeline, maxResults=20).get("pipelineExecutionSummaries", [])
        except Exception as exc:  # noqa: BLE001
            if "PipelineNotFound" in str(exc):
                raise DeployError("pipeline_failed", f"pipeline {pipeline} not found after apply") from exc
            logger.warning("list_pipeline_executions failed: %s", exc)
            runs = []
        mine = [r for r in runs if (r.get("sourceRevisions") or [{}])[0].get("revisionId") == commit]
        if stale is None:
            stale = {r["pipelineExecutionId"] for r in mine if r.get("status") in ("Failed", "Stopped", "Superseded")}
            if stale and not started:
                logger.info("%s: previous run(s) of %s failed — starting a fresh run", pipeline, commit[:8])
                try:
                    cp.start_pipeline_execution(name=pipeline)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("start_pipeline_execution failed: %s", exc)
                started = True
        mine = [r for r in mine if r["pipelineExecutionId"] not in stale]
        if mine:
            run = mine[0]
            st = run.get("status")
            if st == "Succeeded":
                return {"status": "deployed", "pipeline_execution_id": run["pipelineExecutionId"]}
            if st in ("Failed", "Stopped", "Superseded"):
                return {"status": "pipeline_failed", "pipeline_execution_id": run["pipelineExecutionId"],
                        "message": _failure_summary(cp, pipeline, run["pipelineExecutionId"])}
        elif not started and clock() - start >= start_grace:
            logger.info("no %s run for %s after %ss — starting it", pipeline, commit[:8], start_grace)
            try:
                cp.start_pipeline_execution(name=pipeline)
            except Exception as exc:  # noqa: BLE001
                logger.warning("start_pipeline_execution failed: %s", exc)
            started = True
        sleep(_POLL)
    raise DeployError("pipeline_failed", f"{pipeline} did not finish a run of {commit[:8]} within {timeout // 60} min")


# ── apply / destroy ─────────────────────────────────────────────────────────

def _outputs(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return {k: ("(sensitive)" if v.get("sensitive") else v.get("value")) for k, v in data.items()}


def _upload(name: str, body: str) -> None:
    try:
        s3_client.put_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=f"{_DEPLOY_PREFIX}{name}",
                             Body=body.encode("utf-8"), ContentType="text/plain")
    except Exception as exc:  # noqa: BLE001
        logger.warning("deploy artifact upload %s failed: %s", name, exc)


def destructive_changes(plan: dict) -> list[str]:
    """Addresses the plan deletes (destroy, or replace = delete + create)."""
    return [rc.get("address", "?") for rc in plan.get("resource_changes") or []
            if "delete" in ((rc.get("change") or {}).get("actions") or [])]


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def apply(app: dict, app_id: str, commit: str, role_arn: str, *, cc, sts, workdir: Path, runner=None,
          session_factory=deploy_session, waived: bool = False, pipeline_wait=wait_for_pipeline) -> dict:
    """Run steps a–d; returns the callback payload (raises DeployError for non-success outcomes)."""
    repo = primary_codecommit_repo(app)
    branch = deploy_branch(app, app_id)
    head = branch_head(cc, repo, branch)
    if head != commit:
        raise DeployError("branch_moved", f"migration branch moved from {commit[:8]} to {head[:8]}; review the new head")
    fetch_infra(cc, repo, commit, workdir)
    creds = assume_deploy_role(sts, role_arn, app_id)
    env = terraform_env(creds, AWS_REGION)
    log: list[str] = []

    def tf(*args):
        rc, out = _run(["terraform", *args], workdir, env, runner)
        log.append(f"$ terraform {' '.join(args)}\n{out}")
        return rc, out

    try:
        rc, out = tf("init", "-input=false", "-no-color")
        if rc:
            raise DeployError("apply_failed", "terraform init failed:\n" + _tail(out))
        rc, out = tf("plan", "-input=false", "-no-color", "-out=tfplan")
        if rc:
            raise DeployError("apply_failed", "terraform plan failed:\n" + _tail(out))
        rc, show = _run(["terraform", "show", "-json", "tfplan"], workdir, env, runner)
        try:
            plan = json.loads(show) if rc == 0 else None
        except ValueError:
            plan = None
        if plan is None:
            raise DeployError("error", "could not read the deploy plan (terraform show -json) — nothing applied")
        findings = evaluate_exposure(plan)
        if findings and not waived:
            raise DeployError("error", "exposure policy: " + "; ".join(
                f"{f.get('address')}: {f.get('detail')}" for f in findings[:10]))
        # The reviewed plan (Execution) is a dry run on empty state: it never shows deletes.
        # If the real state makes this plan destroy or replace anything, the human did not
        # review that — stop before apply (APP-CLI 2026-10-26: 31 deletes of a stale stack).
        deletes = destructive_changes(plan)
        if deletes and not _truthy(app.get("deploy_allow_destroy")):
            _upload("deploy-plan.txt", out)
            raise DeployError("plan_destroys", f"the deploy plan would destroy/replace {len(deletes)} resource(s) "
                              "not in the reviewed plan — nothing applied. Review deploy/deploy-plan.txt; tear down "
                              "or import the existing stack, or set deploy_allow_destroy=true to accept: "
                              + ", ".join(deletes[:15]) + (" …" if len(deletes) > 15 else ""))
        rc, out = tf("apply", "-input=false", "-no-color", "tfplan")
        if rc:
            raise DeployError("apply_failed", _tail(out))
        _, raw = _run(["terraform", "output", "-json"], workdir, env, runner)
        outputs = _outputs(raw)
    finally:
        _upload("apply.log", "\n\n".join(log))
    _upload("outputs.json", json.dumps(outputs, indent=2, default=str))
    # Fresh 1-hour session for the pipeline wait (up to 60 min) — init/plan/apply used the first.
    cp = session_factory(assume_deploy_role(sts, role_arn, app_id), AWS_REGION).client("codepipeline")
    result = pipeline_wait(cp, f"{app_id}-pipeline", commit)
    return {**result, "outputs": outputs, "commit": commit}


def state_only_root(app: dict, app_id: str, workdir: Path, account_id: str) -> None:
    """A provider-only root on the app's state backend (backend.tf.tpl naming): `terraform destroy`
    then removes everything the state holds without the committed IaC — for a teardown after the
    migration branch is gone (APP-CLI 2026-10-27: archived branch → teardown impossible)."""
    region = str(app.get("target_region") or AWS_REGION)
    prefix = (settings.project_prefix or "").strip()
    if not prefix:
        # Fail closed: a guessed prefix would aim terraform destroy at another estate's state.
        raise DeployError("error", "PROJECT_PREFIX is empty; refusing to guess the app state backend")
    bucket = settings.app_tfstate_bucket_pattern.format(prefix=prefix, account=account_id, region=region)
    lock = settings.app_tfstate_lock_pattern.format(prefix=prefix, account=account_id, region=region)
    provider = settings.app_tf_aws_provider_version
    (workdir / "main.tf").write_text(
        'terraform {\n  backend "s3" {\n'
        f'    bucket         = "{bucket}"\n'
        f'    key            = "{app_id}/terraform.tfstate"\n'
        f'    region         = "{region}"\n    encrypt        = true\n'
        f'    dynamodb_table = "{lock}"\n  }}\n'
        f'  required_providers {{\n    aws = {{ source = "hashicorp/aws", version = "{provider}" }}\n  }}\n}}\n'
        f'provider "aws" {{\n  region = "{region}"\n}}\n')


def destroy(app: dict, app_id: str, role_arn: str, *, cc, sts, workdir: Path, runner=None) -> str:
    repo = primary_codecommit_repo(app)
    log: list[str] = []
    try:
        head = branch_head(cc, repo, deploy_branch(app, app_id))
        fetch_infra(cc, repo, head, workdir)
    except DeployError as exc:
        account = str(app.get("target_account") or "") or sts.get_caller_identity()["Account"]
        state_only_root(app, app_id, workdir, account)
        log.append(f"# committed IaC unavailable ({exc.message}) — destroying from the state alone")
    env = terraform_env(assume_deploy_role(sts, role_arn, app_id), AWS_REGION)
    rc, out = _run(["terraform", "init", "-input=false", "-no-color"], workdir, env, runner)
    log.append(f"$ terraform init\n{out}")
    if rc == 0:
        rc, out = _run(["terraform", "destroy", "-auto-approve", "-input=false", "-no-color"], workdir, env, runner)
        log.append(f"$ terraform destroy\n{out}")
    _upload("destroy.log", "\n\n".join(log))
    if rc:
        raise DeployError("destroy_failed", _tail(out))
    return "destroyed"


def run_deploy(action: str) -> None:
    """Entry point from terraform_runner.run_terraform when INFRA_ACTION is set."""
    from backend.src.worker.dynamo import put_result_record, update_meta_fields
    from backend.src.worker.sfn_callback import finalize_job_once, resolve_token, send_failure, send_success
    from backend.src.worker.terraform_runner import _exposure_waived

    commit, role_arn = DEPLOY_COMMIT, DEPLOY_ROLE_ARN
    cc = boto3.client("codecommit", region_name=AWS_REGION)
    sts = boto3.client("sts", region_name=AWS_REGION)
    token = resolve_token(RUN_ID) if action == "apply" else ""

    with tempfile.TemporaryDirectory(prefix="deploy-") as tmp:
        workdir = Path(tmp)
        try:
            app = read_full_app(APP_ID) or {}
            if action == "destroy":
                try:
                    status, error = destroy(app, APP_ID, role_arn, cc=cc, sts=sts, workdir=workdir), ""
                except DeployError as exc:
                    status, error = "destroy_failed", exc.message
                update_meta_fields(APP_ID, {"exec_deploy_status": status, "exec_deploy_error": error[:2000]})
                logger.info("destroy for %s: %s", APP_ID, status)
                return
            try:
                if len(commit) != 40:
                    raise DeployError("error", f"DEPLOY_COMMIT must be a 40-hex commit id, got {commit!r}")
                payload = apply(app, APP_ID, commit, role_arn, cc=cc, sts=sts, workdir=workdir,
                                waived=_exposure_waived())
            except DeployError as exc:
                payload = {"status": exc.status, "message": exc.message, "commit": commit}
            status = payload["status"]
            update_meta_fields(APP_ID, {"exec_deploy_status": status,
                                        "exec_deploy_error": str(payload.get("message") or "")[:2000]})
            put_result_record(APP_ID, "deploy", {
                "commit": commit, "status": status, "outputs": payload.get("outputs", {}),
                "pipeline": {"execution_id": payload.get("pipeline_execution_id", ""),
                             "message": payload.get("message", "")},
            }, status=status)
            if finalize_job_once(RUN_ID, "COMPLETED"):
                send_success(token, {"app_id": APP_ID, **payload})
        except Exception as exc:  # noqa: BLE001 — a crash: the workflow must not hang
            logger.error("deploy %s crashed for %s: %s", action, APP_ID, exc, exc_info=True)
            try:
                update_meta_fields(APP_ID, {"exec_deploy_status": "error", "exec_deploy_error": str(exc)[:2000]})
            except Exception as _err:  # noqa: BLE001
                logger.debug("best-effort step ignored: %s", _err)
            if action == "apply" and finalize_job_once(RUN_ID, "FAILED", {"error_msg": str(exc)[:500]}):
                send_failure(token, "DeployFailed", str(exc))
