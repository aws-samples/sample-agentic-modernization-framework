"""
Code-analysis pipeline — ATX-only worker for the AgentCore flow.

This is a lightweight worker mode used by the Step Functions callback pattern
(LaunchATX state, invoke.waitForTaskToken). It does NOT run the full assessment
synthesis (that is done by the Assessment Harness afterwards). It ONLY:

  1. Clones the app's repos and runs ATX code analysis
  2. Uploads ATX documentation to S3
  3. Writes an ANALYSIS# record to DynamoDB (so the Harness can read it)
  4. Detects the app's runtime wiring + CI readiness on the primary repo's clone
     (specs 13/14): META detected_* fields + ANALYSIS#ci-readiness — advisory,
     never fails the analysis
  5. Resumes the Step Functions workflow via SendTaskSuccess / SendTaskFailure

The SFN task token is read from the JOB#{job_id} DynamoDB record that the
execution Lambda created when it launched this task.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from backend.src.worker.atx import run_atx_analysis
from backend.src.worker.config import (
    APP_ID,
    AWS_REGION,
    JOB_ID,
    SFN_CALLBACK,
    ddb_table,
    logger,
    read_full_app,
    selected_repos_env,
    sfn_task_token,
)
from backend.src.worker.scope import app_build_dir, primary_repo_name

FINAL_STATUSES = ("COMPLETED", "FAILED")

sfn = boto3.client("stepfunctions", region_name=AWS_REGION)


def _get_task_token() -> str:
    """Resolve the SFN task token.

    Primary source is the SFN_TASK_TOKEN env var (injected by the execution
    Lambda). Falls back to the JOB#{job_id} DynamoDB record for resilience.
    """
    env_token = sfn_task_token()
    if env_token:
        return env_token
    if not JOB_ID:
        return ""
    try:
        item = ddb_table.get_item(Key={"PK": f"JOB#{JOB_ID}", "SK": "META"}).get("Item", {})
        return item.get("task_token", "")
    except Exception as exc:
        logger.warning("Could not read the SFN callback record for job %s: %s", JOB_ID, type(exc).__name__)
        return ""


def _finalize_job_once(status: str, extra: dict | None = None) -> bool:
    """Atomically transition the JOB record to a final status (COMPLETED/FAILED).

    Returns True iff THIS invocation performed the transition (i.e. the job was
    not already finalized). A duplicate ATX invocation for the same job will find
    the job already final and get False, so it MUST NOT send a second callback.
    This is the idempotency guard (Requirement 36.2, Property 10). When there is
    no JOB_ID (e.g. direct/non-callback run), it returns True.
    """
    if not JOB_ID:
        return True
    expr = "SET #s = :s"
    names = {"#s": "status"}
    values = {":s": status, ":c": "COMPLETED", ":f": "FAILED"}
    for i, (k, v) in enumerate((extra or {}).items()):
        expr += f", #k{i} = :v{i}"
        names[f"#k{i}"] = k
        values[f":v{i}"] = v
    try:
        ddb_table.update_item(
            Key={"PK": f"JOB#{JOB_ID}", "SK": "META"},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            # Only finalize if not already finalized by a prior invocation.
            ConditionExpression="attribute_not_exists(#s) OR (#s <> :c AND #s <> :f)",
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("Job %s already finalized — skipping duplicate callback", JOB_ID)
            return False
        logger.warning("Failed to finalize job %s: %s", JOB_ID, exc)
        return False
    except Exception as exc:
        logger.warning("Failed to finalize job %s: %s", JOB_ID, exc)
        return False


def run_code_analysis():
    """Run ATX code analysis and resume the SFN workflow when done."""
    logger.info("Code-analysis starting for %s (job=%s, callback=%s)", APP_ID, JOB_ID, SFN_CALLBACK)
    start_time = time.time()
    token = _get_task_token()

    try:
        app_data = read_full_app(APP_ID)
        if not app_data:
            raise RuntimeError(f"Application {APP_ID} not found in DynamoDB")

        repos = [{**r, "url": str(r.get("url") or "").strip()} for r in app_data.get("repos", [])]
        repos = [r for r in repos if r["url"]]

        # Optional repo filtering
        selected = selected_repos_env()
        if selected:
            selected_urls = {u.strip() for u in selected.split(",") if u.strip()}
            repos = [r for r in repos if r.get("url") in selected_urls]

        if not repos:
            # No repos linked — ATX has nothing to analyze. Record an empty analysis
            # so the Harness knows code analysis was attempted, then succeed. For
            # relocate/rehost there is often no source, only a third-party deliverable
            # (Docker image / binary) to deploy — surface that gracefully.
            deliverables = [d for d in app_data.get("deliverables", []) if d.get("ref")]
            if deliverables:
                refs = ", ".join(d.get("ref", "") for d in deliverables)
                message = (
                    "No source repositories linked; deliverable(s) provided: "
                    f"{refs}. Code analysis skipped (deliverable-based rehost/relocate)."
                )
                logger.warning("No repos for %s — deliverable-based, ATX skipped (%s)", APP_ID, refs)
            else:
                message = "No repositories linked to this application."
                logger.warning("No repositories linked for %s — ATX skipped (no code to scan)", APP_ID)
            atx_output = {
                "status": "no_repos",
                "message": message,
                "deliverables": deliverables,
                "file_count": 0,
                "atx_run_id": "",
            }
            _store_analysis_record(APP_ID, atx_output)
            if _finalize_job_once("COMPLETED", {"file_count": 0, "atx_run_id": ""}):
                _succeed(token, atx_output)
            return

        logger.info("Running ATX code analysis on %d repo(s)...", len(repos))
        atx_output = run_atx_analysis(APP_ID, repos, build_dir=app_build_dir(app_data),
                                      primary_repo=primary_repo_name(app_data))
        run_id = atx_output.get("atx_run_id", "")
        file_count = atx_output.get("file_count", 0)
        logger.info("ATX complete: run_id=%s files=%d", run_id, file_count)
        # Repos ARE linked but nothing was analysed (clone failed, ATX missing / no
        # output, 0 files): fail the callback instead of succeeding with an empty
        # result, which let Assessment run blind and invent findings (APP-CLI).
        if not run_id or not file_count:
            raise RuntimeError(
                f"ATX analysed nothing for {APP_ID} (status={atx_output.get('status', 'unknown')}: "
                f"{atx_output.get('message', 'no run id / 0 files')}). Check the linked repo URL(s) "
                f"and branch: {', '.join(r['url'] for r in repos)}"
            )

        # Persist an ANALYSIS# record so the Assessment Harness can read real findings
        _store_analysis_record(APP_ID, atx_output)
        record_ci_readiness(app_data)

        elapsed = time.time() - start_time
        logger.info("Code-analysis COMPLETED for %s in %.1fs", APP_ID, elapsed)

        if _finalize_job_once("COMPLETED", {"file_count": file_count, "atx_run_id": run_id}):
            _succeed(token, atx_output)

    except Exception as exc:
        elapsed = time.time() - start_time
        logger.error("Code-analysis FAILED for %s after %.1fs: %s", APP_ID, elapsed, exc, exc_info=True)
        if _finalize_job_once("FAILED", {"error_msg": str(exc)}):
            _fail(token, "CodeAnalysisFailed", str(exc))
        sys.exit(1)


def _succeed(token: str, atx_output: dict):
    """Resume the SFN workflow with the ATX result."""
    if not (SFN_CALLBACK and token):
        logger.info("No SFN callback token — skipping SendTaskSuccess")
        return
    payload = {
        "status": atx_output.get("status", "completed"),
        "atx_run_id": atx_output.get("atx_run_id", ""),
        "s3_prefix": atx_output.get("s3_prefix", ""),
        "file_count": atx_output.get("file_count", 0),
    }
    try:
        sfn.send_task_success(taskToken=token, output=json.dumps(payload))
        logger.info("SendTaskSuccess sent for job %s", JOB_ID)
    except Exception as exc:
        logger.error("SendTaskSuccess failed for job %s: %s", JOB_ID, exc)


def _fail(token: str, error: str, cause: str):
    """Signal the SFN workflow that ATX failed."""
    if not (SFN_CALLBACK and token):
        return
    try:
        sfn.send_task_failure(taskToken=token, error=error[:256], cause=cause[:32000])
        logger.info("SendTaskFailure sent for job %s", JOB_ID)
    except Exception as exc:
        logger.error("SendTaskFailure failed for job %s: %s", JOB_ID, exc)


def _store_analysis_record(app_id: str, atx_output: dict):
    """Create an ANALYSIS# record in DynamoDB for this ATX run.

    Delegates to the single shared implementation in worker/dynamo.py so the
    Harness / frontend can read ATX documentation consistently.
    """
    from backend.src.worker.dynamo import store_analysis_record

    store_analysis_record(app_id, atx_output or {})


# META fields written from repo_scan.scan_wiring (spec 13); read by invoke_runtime /
# tool_execution. Human fields (container_port, lambda_handler, …) are never touched.
DETECTED_FIELDS = {
    "build_dir": "detected_build_dir", "language": "detected_language", "runtime_version": "detected_runtime_version",
    "container_port": "detected_container_port", "health_check_path": "detected_health_check_path",
    "lambda_handler": "detected_lambda_handler", "lambda_functions": "detected_lambda_functions",
    "dynamodb_tables": "detected_dynamodb_tables", "env_vars": "detected_env_vars",
    "spring_profiles": "detected_spring_profiles",
    "sources": "detected_wiring_sources",
}


def record_ci_readiness(app_data: dict, workspace: Path = Path("/atx-workspace")) -> dict:
    """Wiring + CI readiness on the primary repo clone; persisted, never raises."""
    from backend.src.worker.ci_readiness import DEFAULT_COVERAGE_THRESHOLD, check
    from backend.src.worker.dynamo import put_result_record, update_meta_fields
    from backend.src.worker.repo_scan import scan_wiring

    try:
        name = primary_repo_name(app_data)
        repo_dir = workspace / name if name else None
        if not repo_dir or not repo_dir.is_dir():
            dirs = [d for d in workspace.iterdir() if d.is_dir() and not d.name.startswith(".")] if workspace.is_dir() else []
            repo_dir = dirs[0] if len(dirs) == 1 else None
        if not repo_dir:
            raise RuntimeError("primary repo clone not found in the ATX workspace")
        build_dir = app_build_dir(app_data)
        wiring = scan_wiring(repo_dir, build_dir)
        scan_dir = build_dir or ("" if wiring.get("build_dir") in (None, ".") else wiring["build_dir"])
        # 0 is a recorded per-app opt-out (coverage_threshold_reason), not "unset".
        raw = app_data.get("coverage_threshold")
        threshold = DEFAULT_COVERAGE_THRESHOLD if raw in (None, "") else int(raw)
        report = check(repo_dir, scan_dir, wiring, threshold=threshold,
                       generate_dockerfile=str(app_data.get("generate_dockerfile")).lower() in ("true", "1", "yes"))
        detected = {meta: wiring[k] for k, meta in DETECTED_FIELDS.items() if wiring.get(k) not in (None, "", [], {})}
        update_meta_fields(app_data.get("app_id") or APP_ID,
                           {**detected, "ci_readiness_status": report["status"]},
                           remove_fields=[m for m in DETECTED_FIELDS.values() if m not in detected])
        put_result_record(app_data.get("app_id") or APP_ID, "ci-readiness", {"wiring": wiring, "readiness": report},
                          status=report["status"])
        logger.info("CI readiness for %s: %s (%d items)", APP_ID, report["status"], len(report["items"]))
        return report
    except Exception as exc:  # noqa: BLE001 — advisory: the analysis callback must still succeed
        logger.warning("CI-readiness check failed for %s: %s", APP_ID, exc, exc_info=True)
        try:
            from backend.src.worker.dynamo import put_result_record, update_meta_fields

            update_meta_fields(app_data.get("app_id") or APP_ID, {"ci_readiness_status": "error"})
            put_result_record(app_data.get("app_id") or APP_ID, "ci-readiness",
                              {"wiring": {}, "readiness": {"status": "error", "items": [], "error": str(exc)}},
                              status="error")
        except Exception as rec_exc:  # noqa: BLE001
            logger.warning("CI-readiness error record failed: %s", rec_exc)
        return {"status": "error", "error": str(exc)}
