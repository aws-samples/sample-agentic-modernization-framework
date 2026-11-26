"""
Shared Step Functions callback helper for ECS worker modes launched via
`invoke.waitForTaskToken` (ATX code analysis and code transformation).

Single source of truth for: resolving the task token, the finalize-once
idempotency guard (Requirement 36.2 / Property 10), and sending
SendTaskSuccess / SendTaskFailure. Used by both the code-analysis and
workbench worker modes so there is one implementation, not two.
"""

from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError

from backend.src.worker.config import AWS_REGION, ddb_table, logger, sfn_task_token

_sfn = boto3.client("stepfunctions", region_name=AWS_REGION)

FINAL_STATUSES = ("COMPLETED", "FAILED")


def resolve_token(job_id: str) -> str:
    """Token from SFN_TASK_TOKEN env (primary) or the JOB record (fallback)."""
    env_token = sfn_task_token()
    if env_token:
        return env_token
    if not job_id:
        return ""
    try:
        item = ddb_table.get_item(Key={"PK": f"JOB#{job_id}", "SK": "META"}).get("Item", {})
        return item.get("task_token", "")
    except Exception as exc:
        logger.warning("Could not read the SFN callback record for job %s: %s", job_id, type(exc).__name__)
        return ""


def finalize_job_once(job_id: str, status: str, extra: dict | None = None) -> bool:
    """Atomically move the JOB record to a final status.

    Returns True iff THIS invocation performed the transition (i.e. the job was
    not already finalized). A duplicate invocation gets False and MUST skip its
    callback — the idempotency guard.
    """
    if not job_id:
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
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_not_exists(#s) OR (#s <> :c AND #s <> :f)",
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("Job %s already finalized — skipping duplicate callback", job_id)
            return False
        logger.warning("finalize_job_once failed for %s: %s", job_id, exc)
        return False
    except Exception as exc:
        logger.warning("finalize_job_once failed for %s: %s", job_id, exc)
        return False


def send_success(token: str, payload: dict) -> None:
    # A resolved token is authoritative: if we have one, this run was launched via
    # waitForTaskToken and MUST call back (otherwise the workflow hangs). We no
    # longer gate solely on the SFN_CALLBACK flag, which caused stuck executions.
    if not token:
        logger.info("No SFN task token — skipping SendTaskSuccess (not a callback run)")
        return
    try:
        _sfn.send_task_success(taskToken=token, output=json.dumps(payload))
        logger.info("SendTaskSuccess sent")
    except Exception as exc:
        logger.error("SendTaskSuccess failed: %s", exc)


def send_failure(token: str, error: str, cause: str) -> None:
    if not token:
        logger.info("No SFN task token — skipping SendTaskFailure (not a callback run)")
        return
    try:
        _sfn.send_task_failure(taskToken=token, error=error[:256], cause=cause[:32000])
        logger.info("SendTaskFailure sent")
    except Exception as exc:
        logger.error("SendTaskFailure failed: %s", exc)
