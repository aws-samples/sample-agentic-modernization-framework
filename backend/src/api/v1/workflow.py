"""
Workflow API — Start and manage Step Functions executions.
Handles approval callbacks and task token storage for human-in-the-loop gates.

Architecture:
  - At each approval gate Step Functions invokes the invoke_runtime Lambda
    (mode=store_token, waitForTaskToken), which stores the token in DynamoDB
    (PK: APP#{app_id}, SK: TASK_TOKEN#{step})
  - User clicks Approve → POST /approve reads token → SendTaskSuccess
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import boto3
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from backend.src.auth.rbac import Role, ensure_role, require_role
from backend.src.config import settings
from backend.src.core import aws
from backend.src.core.models import ApiResponse, error_response, internal_error
from backend.src.db import keys
from backend.src.db.helpers import from_dynamo, get_table, query_all, to_dynamo
from backend.src.db.keys import TYPE_WORKFLOW

logger = logging.getLogger(__name__)
router = APIRouter()

# Gates whose reject re-runs the same phase (see the Approval_* Catch in the ASL);
# rejecting any other gate fails the run.
_RERUN_ON_REJECT = {
    "discovery_review": "discovery",
    "assessment_review": "assessment",
    "design_review": "design",
    "handover_review": "handover",
    # Reject at the deploy gate regenerates Execution's IaC / CI fix / CI-CD from the
    # (possibly edited) migration branch and comes back to the gate.
    "execution_review": "execution",
}


# Gates whose approval needs ADMIN: approving execution_review deploys the reviewed
# commit.
_ADMIN_APPROVAL_STEPS = frozenset({"execution_review"})


def _get_sfn_arn() -> str:
    return settings.get_sfn_state_machine_arn()


# ── Request Models ─────────────────────────────────────────────────────


class StartWorkflowRequest(BaseModel):
    app_id: str


def _truthy_input(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def _build_dir_changed(meta: dict) -> bool:
    """META build_dir set and different from the folder the last repo scan analysed."""
    norm = lambda v: str(v or "").strip().strip("/")  # noqa: E731
    bd, detected = norm(meta.get("build_dir")), norm(meta.get("detected_build_dir"))
    return bool(bd) and bd != detected


class ApprovalRequest(BaseModel):
    app_id: str
    step: str
    approved: bool = True
    comment: str = ""
    # Optional: the gate inputs the client submitted. The audit always records
    # what was PERSISTED on META at approval time (the frontend PUTs its inputs
    # before approving); these are kept alongside as submitted_inputs.
    inputs: dict | None = None


# ── Endpoints ──────────────────────────────────────────────────────────


# Run-derived META fields a fresh run must not inherit. Everything here is either
# re-confirmed at a gate of the new run (assessment inputs), re-derived by one of
# its phases (strategy confidence/rationale, risk score, design/exec rollups), or
# a terminal marker of the previous run (completed_at). Human/L2 business inputs
# (rpo, rto, compliance, owners, …) and migration_strategy are NOT listed — they
# describe the application, not a run.
RUN_DERIVED_META_FIELDS = (
    # assessment-gate inputs (re-entered at assessment_review)
    "target_compute", "cicd_target", "target_database", "target_account", "vpc_cidr",
    "ami_id", "db_instance_class",
    # discovery-derived strategy explanation (recommend_strategy rewrites it every
    # run; Validate_Discovery re-checks strategy_confidence after it does)
    "strategy_confidence", "strategy_rationale", "strategy_scores",
    "recommended_strategy", "recommended_confidence", "recommended_rationale",
    # risk score (invoke_runtime's discovery post-hook recomputes it when absent)
    "risk_score", "risk_tier", "risk_scored_at",
    # assessment / design / execution rollups
    "assessment_status", "assessment_error",
    "design_status", "design_version", "design_generated_at", "design_decided_at",
    "exec_database_status", "exec_infra_status", "exec_cicd_status", "exec_infra_validated_at",
    "completed_at", "completeness_detail",
    # deterministic consistency findings written by the gates (tool_documents /
    # tool_execution) — describe the run that produced them
    "assessment_warnings", "design_warnings", "design_contradictions",
    "execution_database_warnings", "exec_cicd_warning",
    # repo scan + CI readiness (code-analysis worker) and the Execution CI fixer —
    # recomputed every run
    "detected_build_dir", "detected_language", "detected_runtime_version", "detected_container_port",
    "detected_health_check_path", "detected_lambda_handler", "detected_lambda_functions",
    "detected_dynamodb_tables", "detected_env_vars", "detected_wiring_sources", "detected_spring_profiles",
    "ci_readiness_status", "exec_ci_fix_status",
    # deploy gate: the reviewed commit and the apply/pipeline outcome
    "exec_head_commit", "deploy_approved_commit", "deploy_approved_by", "deploy_approved_at",
    "exec_deploy_status", "exec_deploy_error",
    # validate_deployment's latest verdict (drives the Validation column before its gate)
    "validation_status",
)


def _is_spec_seeded_steps(steps) -> bool:
    """True when transformation_steps came from the app's units.md, not the UI.

    scripts/seed_transformation_steps.py writes list[dict] ({td_name,
    additional_context, …}) — engineer spec input no phase re-derives, so it must
    survive a new run. The assessment gate writes list[str] TD names — a per-run
    selection that the new run's assessment_review re-enters.
    """
    return isinstance(steps, list) and any(isinstance(s, dict) for s in steps)


def _reset_app_for_new_run(table, app_id: str) -> None:
    """Clear a prior run's derived output so a fresh workflow starts clean.

    Removes every run-derived META field (RUN_DERIVED_META_FIELDS), resets status
    to `discovered`, deletes the ANALYSIS# / DESIGN# / EXECUTION# (etc.) records,
    and clears the corresponding S3 artifacts. `migration_strategy` is left alone —
    Discovery re-decides it (and the human re-confirms it at discovery_review).
    """
    from boto3.dynamodb.conditions import Key as _Key

    # 1. Clear run-derived fields on META.
    #    transformation_steps: cleared only when it is a UI selection (list[str]);
    #    a units.md spec seed (list[dict]) is kept — see _is_spec_seeded_steps.
    #    L2 preferences (META.l2_preferences, written by /portfolio/apps/enrich)
    #    for a cleared field are restored rather than lost, so e.g. an uploaded
    #    cicd_target preference pre-fills the new run's assessment gate again.
    try:
        meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item") or {}
    except Exception as exc:
        logger.warning("reset META read for %s failed: %s", app_id, exc)
        meta = {}
    stale_fields = list(RUN_DERIVED_META_FIELDS)
    if not _is_spec_seeded_steps(meta.get("transformation_steps")):
        stale_fields.append("transformation_steps")
    prefs = meta.get("l2_preferences") if isinstance(meta.get("l2_preferences"), dict) else {}
    restore = {k: v for k, v in prefs.items() if k in stale_fields and v not in (None, "")}
    remove = [f for f in stale_fields if f not in restore]

    names = {"#s": "status", **{f"#f{i}": f for i, f in enumerate(remove)}}
    values = {":d": "discovered"}
    set_parts = ["#s = :d"]
    for i, (k, v) in enumerate(restore.items()):
        names[f"#p{i}"] = k
        values[f":p{i}"] = v
        set_parts.append(f"#p{i} = :p{i}")
    try:
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression="SET " + ", ".join(set_parts) + " REMOVE " + ", ".join(f"#f{i}" for i in range(len(remove))),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        logger.warning("reset META for %s failed: %s", app_id, exc)

    # 2. Delete prior derived records (analysis, design, execution, migration/wave plan).
    #    TASK_TOKEN# is included so a fresh run can't inherit a prior run's gates —
    #    tokens are otherwise deleted only on that exact step's approval, so failed
    #    or superseded runs would leave them behind indefinitely.
    #    APPROVAL_AUDIT# is intentionally preserved (append-only audit trail).
    for prefix in ("ANALYSIS#", "DESIGN#", "EXECUTION#", "MIGRATION_PLAN", "PLAN#", "TOOLCALL#", "TASK_TOKEN#"):
        try:
            resp = table.query(
                KeyConditionExpression=_Key("PK").eq(f"APP#{app_id}") & _Key("SK").begins_with(prefix)
            )
            with table.batch_writer() as batch:
                for it in resp.get("Items", []):
                    batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
        except Exception as exc:
            logger.warning("reset %s records for %s failed: %s", prefix, app_id, exc)

    # 3. Clear prior S3 artifacts (analysis / design / execution).
    try:
        s3 = aws.client("s3")
        bucket = settings.get_s3_bucket()
        for sub in ("analysis", "design", "execution"):
            token = None
            while True:
                kw = {"Bucket": bucket, "Prefix": f"applications/{app_id}/{sub}/"}
                if token:
                    kw["ContinuationToken"] = token
                page = s3.list_objects_v2(**kw)
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objs:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
    except Exception as exc:
        logger.warning("reset S3 artifacts for %s failed: %s", app_id, exc)


@router.post("/start", dependencies=[require_role(Role.ENGINEER)])
def start_workflow(req: StartWorkflowRequest):
    """Start the migration workflow for an application.

    Creates a Step Functions execution for the app lifecycle.
    Prevents duplicate: if app already has a RUNNING workflow, returns error.
    """
    try:
        sfn = aws.client("stepfunctions")
        table = get_table()

        # Check for existing running workflow
        existing = table.get_item(Key={"PK": f"APP#{req.app_id}", "SK": "WORKFLOW#ACTIVE"}).get("Item")
        if existing and existing.get("status") == "RUNNING" and existing.get("execution_arn"):
            # DynamoDB status is a cache; Step Functions is the source of truth. A
            # run that ended in MarkFailed (a Fail state) leaves the cache RUNNING,
            # which blocked restarting it (APP-CLI, 2026-10-12).
            try:
                live = sfn.describe_execution(executionArn=existing["execution_arn"])["status"]
            except Exception as exc:  # noqa: BLE001 — unknown → keep the conservative block
                logger.warning("describe_execution failed for %s: %s", req.app_id, exc)
                live = "RUNNING"
            if live != "RUNNING":
                table.update_item(
                    Key={"PK": f"APP#{req.app_id}", "SK": "WORKFLOW#ACTIVE"},
                    UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": live},
                )
                existing = {**existing, "status": live}
        if existing and existing.get("status") == "RUNNING":
            return error_response(
                f"Workflow already running for {req.app_id}. Cancel it first before starting a new one.",
                code="WORKFLOW_ALREADY_RUNNING",
            )

        # Fresh run — clear the prior run's assessment/design output (DDB records,
        # S3 artifacts, and confirmed META selections) so the UI never shows a
        # previous run's data before this run reproduces it.
        _reset_app_for_new_run(table, req.app_id)

        # Get the app's confirmed strategy for conditional branching in ASL
        app_resp = table.get_item(Key={"PK": f"APP#{req.app_id}", "SK": "META"})
        app_data = app_resp.get("Item", {})
        strategy = app_data.get("migration_strategy", "")

        execution_input = json.dumps(
            {
                "app_id": req.app_id,
                "strategy": strategy,
                "started_at": datetime.now(UTC).isoformat(),
                "started_by": "user",
            }
        )

        # Start execution
        response = sfn.start_execution(
            stateMachineArn=_get_sfn_arn(),
            name=f"{req.app_id}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
            input=execution_input,
        )

        execution_arn = response["executionArn"]

        # Store workflow record in DynamoDB. Keys come from the central key builder
        # (db/keys.py) so the GSI1 attributes (TYPE#WORKFLOW partition) that make
        # get_active_workflows a bounded Query — never a Scan — are always present.
        table = get_table()
        now = datetime.now(UTC).isoformat()
        wf = keys.workflow_active(req.app_id)
        table.put_item(
            Item=to_dynamo(
                {
                    **wf.as_key(),
                    **wf.gsi1_attrs(),
                    "app_id": req.app_id,
                    "execution_arn": execution_arn,
                    "status": "RUNNING",
                    "current_phase": "discovery",
                    "current_step": "discovery_enrichment",
                    "strategy": strategy,
                    "started_at": now,
                    "phases_completed": [],
                }
            )
        )

        invalidate_active_workflows_cache()
        logger.info("Workflow started for %s: %s", req.app_id, execution_arn)

        return ApiResponse(
            data={
                "app_id": req.app_id,
                "execution_arn": execution_arn,
                "status": "RUNNING",
            }
        )

    except Exception as exc:
        logger.error("Start workflow failed: %s", exc)
        return internal_error(exc, code="WORKFLOW_START_FAILED")


class CancelWorkflowRequest(BaseModel):
    app_id: str
    reason: str = "Cancelled by user"


def _stop_running_worker_tasks(table, app_id: str) -> int:
    """Stop any RUNNING ECS worker task for an app and mark its JOB cancelled.

    Ensures a cancel promptly halts Fargate tasks (cost control, R50) instead of
    leaving them running until they finish on their own.
    """
    from boto3.dynamodb.conditions import Key

    stopped = 0
    try:
        ecs = aws.client("ecs")
        resp = table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq("TYPE#JOB") & Key("GSI1SK").begins_with(f"{app_id}#"),
        )
        for job in resp.get("Items", []):
            if job.get("status") != "RUNNING":
                continue
            task_arn = job.get("task_arn", "")
            if task_arn:
                try:
                    ecs.stop_task(
                        cluster=settings.atx_ecs_cluster,
                        task=task_arn,
                        reason="workflow cancelled",
                    )
                    stopped += 1
                except Exception as exc:
                    logger.warning("stop_task failed for %s: %s", task_arn, exc)
            try:
                table.update_item(
                    Key={"PK": job["PK"], "SK": job.get("SK", "META")},
                    UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "CANCELLED"},
                )
            except Exception as _err:
                logger.debug("best-effort step ignored: %s", _err)
    except Exception as exc:
        logger.warning("Worker task stop sweep failed for %s: %s", app_id, exc)
    return stopped


@router.post("/cancel", dependencies=[require_role(Role.ENGINEER)])
def cancel_workflow(req: CancelWorkflowRequest):
    """Cancel a running workflow for an application.

    Stops the Step Functions execution and marks the workflow as CANCELLED.
    """
    try:
        table = get_table()

        # Get active workflow
        wf = table.get_item(Key={"PK": f"APP#{req.app_id}", "SK": "WORKFLOW#ACTIVE"}).get("Item")
        if not wf:
            return error_response(f"No active workflow for {req.app_id}", code="NOT_FOUND")

        if wf.get("status") != "RUNNING":
            return error_response(f"Workflow is not running (status: {wf.get('status')})", code="NOT_RUNNING")

        execution_arn = wf.get("execution_arn", "")

        # Stop Step Functions execution
        if execution_arn:
            sfn = aws.client("stepfunctions")
            try:
                sfn.stop_execution(executionArn=execution_arn, cause=req.reason)
            except Exception as exc:
                # Only "already stopped" is safe to ignore. Marking a still-running
                # execution CANCELLED lets /start launch a second, concurrent run
                # (APP-CLI 2026-10-13: StopExecution was AccessDenied).
                try:
                    live = sfn.describe_execution(executionArn=execution_arn)["status"]
                except Exception:
                    live = "UNKNOWN"
                if live in ("RUNNING", "UNKNOWN"):
                    logger.error("SFN stop failed for %s (%s): %s", req.app_id, live, exc)
                    return error_response(
                        f"Could not stop the workflow for {req.app_id} (Step Functions StopExecution failed).",
                        code="CANCEL_FAILED", status_code=502,
                    )
                logger.info("SFN execution for %s already %s", req.app_id, live)

        # Update DynamoDB. Also set current_step = "cancelled" (and clear the
        # waiting marker) so the UI's review/active step styling clears — otherwise
        # the pipeline keeps rendering the pre-cancel step (e.g. "design_review").
        now = datetime.now(UTC).isoformat()
        table.update_item(
            Key={"PK": f"APP#{req.app_id}", "SK": "WORKFLOW#ACTIVE"},
            UpdateExpression="SET #s = :status, current_step = :cs, cancelled_at = :ca, cancel_reason = :cr REMOVE waiting_since",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "CANCELLED", ":cs": "cancelled", ":ca": now, ":cr": req.reason},
        )

        # Stop any in-flight ECS worker task promptly so it stops billing (R50).
        _stop_running_worker_tasks(table, req.app_id)

        # Clean up any pending task tokens (batch delete)
        from boto3.dynamodb.conditions import Key

        tokens = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{req.app_id}") & Key("SK").begins_with("TASK_TOKEN#"),
        )
        if tokens.get("Items"):
            with table.batch_writer() as batch:
                for token in tokens["Items"]:
                    batch.delete_item(Key={"PK": token["PK"], "SK": token["SK"]})

        invalidate_active_workflows_cache()
        logger.info("Workflow cancelled for %s: %s", req.app_id, req.reason)
        return ApiResponse(data={"app_id": req.app_id, "status": "CANCELLED", "reason": req.reason})

    except Exception as exc:
        logger.error("Cancel workflow failed: %s", exc)
        return internal_error(exc, code="CANCEL_FAILED")


# Mirror of invoke_runtime._resolve_strategy's close_out branch.
_CLOSE_OUT_STRATEGIES = {"retire", "retain", "repurchase"}

# Inputs a gate confirms, recorded on its APPROVAL_AUDIT item (read from META at
# approval time, after the frontend's PUT /portfolio/apps/{id}).
GATE_INPUT_FIELDS = {
    "discovery_review": ("migration_strategy",),
    "assessment_review": (
        "target_compute", "target_database", "cicd_target",
        "target_account", "vpc_cidr", "transformation_steps",
        "ami_id", "db_instance_class",
    ),
}


# Engine confidence for a strategy it found no evidence for (tool_strategy's floor).
_NO_EVIDENCE_CONFIDENCE = settings.strategy_confidence_floor


def _gate_inputs(meta: dict, step: str) -> dict:
    return {f: meta.get(f) for f in GATE_INPUT_FIELDS.get(step, ()) if meta.get(f) not in (None, "", [])}


def _confirmed_strategy_rationale(confirmed: str, meta: dict) -> tuple[str, str] | None:
    """(confidence, rationale) describing a human-confirmed strategy, or None.

    recommend_strategy (tool_strategy Lambda) stores its recommendation as
    recommended_strategy/_confidence/_rationale plus per-strategy strategy_scores.
    When the reviewer confirms a DIFFERENT strategy at discovery_review, META's
    strategy_confidence/strategy_rationale must describe the confirmed strategy,
    not the engine's pick (APP-CLI showed 'relocate' with rehost's 45% and
    'Default: insufficient data'). Same wording as tool_strategy._confirmed_note —
    keep in sync.
    """
    rec = (meta.get("recommended_strategy") or "").strip().lower()
    if not confirmed or not rec or rec == confirmed:
        return None
    scores = meta.get("strategy_scores") if isinstance(meta.get("strategy_scores"), dict) else {}
    conf = scores.get(confirmed)
    confidence = str(conf) if conf not in (None, "") else str(_NO_EVIDENCE_CONFIDENCE)
    rationale = (
        f"Human-confirmed {confirmed}; engine recommended {rec} "
        f"({meta.get('recommended_confidence', '?')}%) because {meta.get('recommended_rationale') or 'n/a'}"
    )
    return confidence, rationale


@router.post("/approve")
def approve_step(req: ApprovalRequest, request: Request, user=require_role(Role.ENGINEER)):
    """Send approval callback to Step Functions.

    Called when user approves/rejects a workflow gate.
    Reads the stored task token from DynamoDB and calls SendTaskSuccess/Failure.
    Guards: verifies the workflow is still RUNNING before allowing approval.
    Records the approver identity in the audit trail (R41.3).

    RBAC: ENGINEER for every gate, ADMIN to approve
    `execution_review` — that approval deploys. The step is the one whose
    TASK_TOKEN# is released below (the token is looked up by `req.step`), so the
    check can't be dodged by naming another step. Rejecting it (which regenerates
    Execution, no deploy) stays ENGINEER.
    """
    # With deploy disabled (the default) execution_review approves the reviewed branch and applies
    # nothing, so it is an ordinary ENGINEER gate and needs no deploy commit.
    deploys = settings.deploy_enabled and req.step == "execution_review"
    if req.approved and req.step in _ADMIN_APPROVAL_STEPS and settings.deploy_enabled:
        ensure_role(user, Role.ADMIN)
    try:
        table = get_table()

        # Approver identity for the audit trail.
        actor = getattr(request.state, "user", None)
        approver = (getattr(actor, "email", "") or getattr(actor, "username", "") or "unknown") if actor else "unknown"

        # Get the active workflow for this app
        resp = table.get_item(Key={"PK": f"APP#{req.app_id}", "SK": "WORKFLOW#ACTIVE"})
        workflow = resp.get("Item")
        if not workflow:
            return error_response(f"No active workflow for {req.app_id}", code="NOT_FOUND")

        # Guard: workflow must be RUNNING to approve/reject
        wf_status = workflow.get("status", "")
        if wf_status != "RUNNING":
            return error_response(
                f"Cannot approve — workflow is {wf_status}. Cancel the current workflow first if you want to restart.",
                code="WORKFLOW_NOT_RUNNING",
            )

        # Get the pending task token for this step
        token_resp = table.get_item(Key={"PK": f"APP#{req.app_id}", "SK": f"TASK_TOKEN#{req.step}"})
        token_item = token_resp.get("Item")
        if not token_item:
            return error_response(f"No pending approval for step '{req.step}'", code="NOT_FOUND")

        task_token = token_item.get("task_token", "")
        if not task_token:
            return error_response("Task token not found", code="TOKEN_MISSING")

        # Deploy gate: approving execution_review applies the
        # infrastructure, so it must name the exact migration-branch commit the human
        # reviewed. Persisted before the token is released; the Deploy worker
        # re-checks it against the live branch HEAD and refuses a moved branch.
        deploy_commit = ""
        if req.approved and deploys:
            deploy_commit = str((req.inputs or {}).get("deploy_commit") or "").strip()
            head = str((from_dynamo(table.get_item(Key={"PK": f"APP#{req.app_id}", "SK": "META"}).get("Item") or {})
                        ).get("exec_head_commit") or "")
            if not re.fullmatch(r"[0-9a-f]{40}", deploy_commit):
                return error_response("Deploy needs the reviewed migration-branch commit (inputs.deploy_commit).",
                                      code="DEPLOY_COMMIT_REQUIRED")
            if head and deploy_commit != head:
                return error_response(f"The migration branch moved: reviewed {deploy_commit[:8]}, Execution generated "
                                      f"{head[:8]}. Review the new head (or Retry to regenerate) before deploying.",
                                      code="DEPLOY_COMMIT_STALE")
            table.update_item(
                Key={"PK": f"APP#{req.app_id}", "SK": "META"},
                UpdateExpression="SET deploy_approved_commit = :c, deploy_approved_by = :b, deploy_approved_at = :t",
                ExpressionAttributeValues={":c": deploy_commit, ":b": approver, ":t": datetime.now(UTC).isoformat()},
            )

        sfn = aws.client("stepfunctions")

        if req.approved:
            sfn.send_task_success(
                taskToken=task_token,
                output=json.dumps(
                    {
                        "approved": True,
                        "comment": req.comment,
                        "approved_at": datetime.now(UTC).isoformat(),
                        "approved_by": approver,
                        "app_id": req.app_id,
                        "deploy_commit": deploy_commit,
                    }
                ),
            )
        else:
            # Rejection — send task failure so ASL Catch block handles it. A rejected
            # Assessment re-runs the code analysis (Route_AssessmentRetry) when asked to,
            # or when build_dir changed since the repo scan — else only the agent re-runs
            # and keeps the stale detected wiring (APP-HE 2026-10-23).
            reanalyze = False
            if req.step == "assessment_review":
                meta = from_dynamo(table.get_item(Key={"PK": f"APP#{req.app_id}", "SK": "META"}).get("Item") or {})
                reanalyze = _truthy_input((req.inputs or {}).get("reanalyze")) or _build_dir_changed(meta)
            sfn.send_task_failure(
                taskToken=task_token,
                error="StepRejected",
                cause=json.dumps(
                    {
                        "approved": False,
                        "step": req.step,
                        "comment": req.comment,
                        "rejected_at": datetime.now(UTC).isoformat(),
                        "rejected_by": approver,
                        "app_id": req.app_id,
                        "reanalyze": reanalyze,
                    }
                ),
            )

        # Update workflow state — advance to next phase
        now = datetime.now(UTC).isoformat()

        # Map: which phase comes after this approval step
        next_phase_map = {
            "discovery_review": "assessment",
            "assessment_review": "design",
            "design_review": "execution",
            "planning_review": "execution",
            "execution_review": "deploy",
            "validation_review": "cutover",
            "cutover_review": "handover",
            "final_approval": "handover",
        }
        next_phase = next_phase_map.get(req.step, "")

        update_expr = (
            "SET last_approved_step = :ls, last_approved_at = :la, "
            "last_approval_comment = :lc, last_approved_by = :lb"
        )
        expr_values = {":ls": req.step, ":la": now, ":lc": req.comment, ":lb": approver}

        if req.approved and next_phase:
            update_expr += ", current_phase = :np, current_step = :ns"
            expr_values[":np"] = next_phase
            expr_values[":ns"] = f"{next_phase}_running"
        elif not req.approved and req.step in _RERUN_ON_REJECT:
            # These gates re-run their phase on reject (ASL Catch → phase). Without
            # this the record kept "<phase>_review" while no token existed, so the UI
            # showed the gate with no approval buttons until the re-run finished.
            update_expr += ", current_step = :ns"
            expr_values[":ns"] = f"{_RERUN_ON_REJECT[req.step]}_running"

        # Inputs persisted at this gate (META after the frontend's PUT). At the
        # discovery gate the human confirms migration_strategy: mirror it onto the
        # WORKFLOW snapshot (which otherwise keeps the start-time value — APP-HE
        # showed WORKFLOW.strategy=retire while META=replatform) and make the
        # strategy explanation describe the confirmed strategy.
        meta = None
        try:
            meta = from_dynamo(
                table.get_item(Key={"PK": f"APP#{req.app_id}", "SK": "META"}).get("Item") or {}
            )
        except Exception as exc:
            logger.warning("Approval META read failed for %s (non-blocking): %s", req.app_id, exc)

        confirmed = ((meta or {}).get("migration_strategy") or "").strip().lower()
        if req.approved and req.step == "discovery_review" and confirmed in _CLOSE_OUT_STRATEGIES:
            # ResolveStrategy routes retire/retain/repurchase straight to the
            # close-out (Handover_CloseOut) — not Assessment. Otherwise the run
            # (and its tool-call traces) showed assessment_running until the end.
            expr_values[":np"] = "handover"
            expr_values[":ns"] = "handover_running"
        if req.approved and req.step == "discovery_review" and confirmed:
            update_expr += ", #st = :st"
            expr_values[":st"] = confirmed
            explained = _confirmed_strategy_rationale(confirmed, meta or {})
            if explained:
                try:
                    table.update_item(
                        Key={"PK": f"APP#{req.app_id}", "SK": "META"},
                        UpdateExpression="SET strategy_confidence = :c, strategy_rationale = :r",
                        ExpressionAttributeValues={":c": explained[0], ":r": explained[1]},
                    )
                except Exception as exc:
                    logger.warning("Strategy rationale sync failed for %s (non-blocking): %s", req.app_id, exc)

        # store_design_document leaves the design `pending_review`; the human
        # decision at the design gate (or the close-out gate, for DESIGN#final)
        # is what moves it to approved / rejected.
        if req.step in ("design_review", "handover_review"):
            try:
                table.update_item(
                    Key={"PK": f"APP#{req.app_id}", "SK": "META"},
                    UpdateExpression="SET design_status = :s, design_decided_at = :t",
                    ExpressionAttributeValues={":s": "approved" if req.approved else "rejected", ":t": now},
                )
            except Exception as exc:
                logger.warning("Design status update failed for %s (non-blocking): %s", req.app_id, exc)

        table.update_item(
            Key={"PK": f"APP#{req.app_id}", "SK": "WORKFLOW#ACTIVE"},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            **({"ExpressionAttributeNames": {"#st": "strategy"}} if "#st" in update_expr else {}),
        )

        # Durable, append-only approval audit record (survives token cleanup).
        # Scoped to the run (execution_arn) so a later run never mistakes this
        # run's sign-off for its own (tool_strategy._human_has_approved), and
        # records the inputs that were persisted at this gate.
        execution_arn = workflow.get("execution_arn", "")
        audit = {
            "PK": f"APP#{req.app_id}",
            "SK": f"APPROVAL_AUDIT#{now}",
            "type": "APPROVAL_AUDIT",
            "app_id": req.app_id,
            "step": req.step,
            "approved": req.approved,
            "actor": approver,
            "comment": req.comment,
            "at": now,
            "execution_arn": execution_arn,
            "execution_name": execution_arn.rsplit(":", 1)[-1] if execution_arn else "",
        }
        if meta is not None:
            audit["inputs"] = _gate_inputs(meta, req.step)
        if req.inputs:
            audit["submitted_inputs"] = req.inputs
        try:
            table.put_item(Item=to_dynamo(audit))
        except Exception as exc:
            logger.warning("Approval audit write failed (non-blocking): %s", exc)

        # Clean up token
        table.delete_item(Key={"PK": f"APP#{req.app_id}", "SK": f"TASK_TOKEN#{req.step}"})

        # Note: design documents are (re)generated by the AgentCore Harness in the
        # Design workflow state — no separate ECS trigger on approval.
        logger.info(
            "Workflow approval for %s/%s: approved=%s by=%s",
            req.app_id, req.step, req.approved, approver,
        )

        return ApiResponse(
            data={
                "app_id": req.app_id,
                "step": req.step,
                "approved": req.approved,
                "approved_by": approver,
            }
        )

    except Exception as exc:
        logger.error("Approval failed: %s", exc)
        return internal_error(exc, code="APPROVAL_FAILED")


@router.get("/migration-plan/{app_id}")
def get_migration_plan(app_id: str):
    """Get the migration plan for an application."""
    try:
        table = get_table()
        resp = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "MIGRATION_PLAN#latest"})
        item = resp.get("Item")
        if not item:
            return error_response(f"No migration plan for {app_id}", code="NOT_FOUND")
        return ApiResponse(data=from_dynamo(item))
    except Exception as exc:
        return internal_error(exc, code="QUERY_FAILED")


@router.get("/handover/{app_id}")
def get_handover_package(app_id: str):
    """Get the handover package (runbook + decommission plan) for an application.

    Returns markdown content from S3 if generated, or status if pending.
    """
    try:
        table = get_table()
        meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {})
        handover_status = meta.get("handover_status", "")
        strategy = meta.get("migration_strategy", "")

        s3_client = aws.client("s3")
        bucket = settings.s3_bucket

        docs = {}
        for doc_name in ["runbook.md", "decommission-plan.md"]:
            key = f"applications/{app_id}/handover/{doc_name}"
            try:
                resp = s3_client.get_object(Bucket=bucket, Key=key)
                docs[doc_name.replace(".md", "").replace("-", "_")] = resp["Body"].read().decode("utf-8")
            except Exception:
                docs[doc_name.replace(".md", "").replace("-", "_")] = None

        return ApiResponse(data={
            "app_id": app_id,
            "status": handover_status or "pending",
            "strategy": strategy,
            "runbook": docs.get("runbook"),
            "decommission_plan": docs.get("decommission_plan"),
            "handover_at": meta.get("handover_at", ""),
        })
    except Exception as exc:
        logger.error("get_handover_package: %s", exc)
        return internal_error(exc, code="HANDOVER_FETCH_FAILED")


_REPORT_PHASE_ALIASES = {"deploy": "execution/deploy", "infrastructure": "execution/infrastructure",
                         "cicd": "execution/cicd", "transformation": "execution/transformation"}


@router.post("/validate/{app_id}")
def rerun_validation(app_id: str, user=require_role(Role.ENGINEER)):
    """Re-run the deterministic Validation (tool_execution.validate_deployment) for a deployed app.

    Asynchronous — a run (probe Lambda in the app VPC, up to ~10 min) outlasts the CloudFront/ALB
    request timeout; the Validation screen polls /analysis/{app}/validation-results for the new run.
    It records a new validation-results run and META.validation_status; it does not move the
    workflow (the workflow's own Validation step still gates Cutover)."""
    table = get_table()
    meta = from_dynamo(table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item") or {})
    if not meta:
        return error_response(f"Unknown app {app_id}", code="NOT_FOUND")
    if meta.get("exec_deploy_status") != "deployed":
        return error_response(f"{app_id} is not deployed (exec_deploy_status={meta.get('exec_deploy_status') or 'none'}).",
                              code="NOT_DEPLOYED")
    try:
        aws.client("lambda").invoke(
            FunctionName=settings.tool_execution_lambda, InvocationType="Event",
            Payload=json.dumps({"tool_name": "validate_deployment", "arguments": {"app_id": app_id, "_rerun": True}}).encode())
    except Exception as exc:  # noqa: BLE001
        logger.error("rerun_validation %s: %s", app_id, exc)
        return internal_error(exc, code="VALIDATION_START_FAILED")
    logger.info("Validation re-run requested for %s by %s", app_id, getattr(user, "email", "unknown"))
    return ApiResponse(data={"app_id": app_id, "status": "started"})


@router.get("/report/{app_id}/{phase}")
def get_phase_report(app_id: str, phase: str, expires: int = settings.presign_ttl_seconds):
    """Return a phase's stored report artifacts from the Report_Store (S3).

    Lists the objects under the phase prefix and returns time-limited presigned
    download URLs plus the markdown content of the primary report when present.
    Consumers read report content from persistence, never from transient output.
    (Requirements 9.5, 33.1, 33.3)
    """
    try:
        # Delegate listing + presigning to the single Report_Store helper so the
        # S3 layout + URL logic lives in one place (backend/src/db/reports.py).
        from backend.src.db import reports

        # Execution sub-artifacts live one level down (applications/{app}/execution/{deploy,…}/).
        phase = _REPORT_PHASE_ALIASES.get(phase, phase)
        files = reports.list_reports(app_id, phase, expires=expires)
        # Read the first markdown file inline as the primary report body.
        primary_md = None
        for f in files:
            if f["key"].endswith(".md"):
                try:
                    primary_md = reports.read(f["key"])
                except Exception:
                    primary_md = None
                break

        return ApiResponse(data={
            "app_id": app_id,
            "phase": phase,
            "s3_prefix": f"s3://{settings.s3_bucket}/{reports.report_prefix(app_id, phase)}",
            "file_count": len(files),
            "files": files,
            "report_md": primary_md,
            "present": len(files) > 0,
        })
    except Exception as exc:
        logger.error("get_phase_report: %s", exc)
        return internal_error(exc, code="REPORT_FETCH_FAILED")


@router.get("/migration-plan/{app_id}/defaults")
def get_migration_plan_defaults(app_id: str):
    """Get default engineer input suggestions based on app data."""
    try:
        table = get_table()
        from backend.src.api.v1.portfolio import _read_full_app

        app_data = _read_full_app(table, app_id) or {}
        strategy = app_data.get("migration_strategy", "")

        from backend.src.core.validation.aws_best_practices import get_default_inputs

        defaults = get_default_inputs(app_data, strategy)

        # Code transformation only applies to strategies that change the code
        # (replatform/refactor/rearchitect). Rehost/relocate are lift-and-shift
        # ("migrate as-is") and close-out strategies do nothing — for those the
        # design must NOT surface transformation steps or gaps, even if the ATX
        # analysis found modernization opportunities (those are deferred, not
        # part of this migration). This keeps design consistent with the
        # assessment's "no transformation required for rehost" conclusion.
        TRANSFORM_STRATEGIES = {"replatform", "refactor", "rearchitect"}
        if (strategy or "").lower() not in TRANSFORM_STRATEGIES:
            defaults["transformation_steps"] = []
            defaults["transformation_gaps"] = []
            return ApiResponse(data=defaults)

        # Surface the assessment's transformation decision. The assessment emits
        # ONE canonical structure (see backend/resources/skills/assessment.md):
        #
        #   result.transformations = {
        #     "recommended": [ {td_name, order, scope, rationale, build_context?} ],  # ATX-executable
        #     "gaps":        [ {requirement, scope, reason, severity} ],              # NO ATX TD → engineer authors it
        #   }
        #
        # "recommended" td_names pre-fill the multiselect (checked); "gaps" render
        # in red. An assessment that has run is authoritative — its lists are used
        # verbatim (empty means "none applicable"), overriding the generic
        # strategy/language heuristic; the heuristic only stands in until an
        # assessment exists.
        try:
            from backend.src.db import accessors

            assessment = accessors.get_latest_analysis(app_id, "assessment-summary")
            if assessment is not None:
                tx = (assessment.get("result") or {}).get("transformations") or {}
                td_names: list[str] = []
                for s in sorted(
                    (s for s in (tx.get("recommended") or []) if isinstance(s, dict) and s.get("td_name")),
                    key=lambda s: s.get("order", 999),
                ):
                    if s["td_name"] not in td_names:
                        td_names.append(s["td_name"])
                defaults["transformation_steps"] = td_names
                defaults["transformation_gaps"] = tx.get("gaps") or []
        except Exception:
            logger.warning("could not read assessment transformations for %s", app_id, exc_info=True)
        return ApiResponse(data=defaults)
    except Exception as exc:
        return internal_error(exc, code="DEFAULTS_FAILED")


class MigrationPlanInputs(BaseModel):
    app_id: str
    # Account & Region
    account: str = "create_new"  # create_new / existing
    account_id: str = ""
    target_regions: list[str] = Field(default_factory=lambda: [settings.aws_region])  # primary + DR
    # Network
    vpc: str = "create_new"  # create_new / existing
    vpc_id: str = ""
    dns_route53: str = "yes_new"  # yes_new / yes_existing / no
    # Compute
    compute_service: str = "ecs_fargate"
    # Database
    target_database: str = "aurora_postgresql"
    migration_tool: str = "dms"  # dms / sct_dms / native_dump
    backup_retention_days: int = 14
    # Deploy
    cicd_provider: str = "codepipeline"
    deployment_strategy: str = "blue_green"
    # HA & Security
    multi_az: str = "yes"
    encryption: str = "aws_managed"  # aws_managed / cmk
    # Transformation steps (ATX TD names in execution order)
    transformation_steps: list[str] = []  # e.g. ["java-upgrade", "spring-boot-migration", "containerization"]
    # Testing (selected strategies)
    testing_strategy: list[str] = []  # e.g. ["unit", "integration", "load", "security", "smoke"]
    # Monitoring
    monitoring: list[str] = ["cloudwatch"]


@router.post("/migration-plan/{app_id}/submit", dependencies=[require_role(Role.ENGINEER)])
def submit_migration_plan_inputs(app_id: str, inputs: MigrationPlanInputs):
    """Submit validated engineer inputs and store them.

    Saves inputs to DynamoDB, updates the migration plan, and triggers design doc update.
    """
    try:
        table = get_table()
        from backend.src.api.v1.portfolio import _read_full_app

        app_data = _read_full_app(table, app_id) or {}

        # Validate first
        from backend.src.core.validation.aws_best_practices import validate_migration_inputs

        validation = validate_migration_inputs(app_data, inputs.model_dump())

        if not validation["valid"]:
            return ApiResponse(status="error", data=validation)

        # Store inputs
        now = datetime.now(UTC).isoformat()
        table.put_item(
            Item=to_dynamo(
                {
                    "PK": f"APP#{app_id}",
                    "SK": "MIGRATION_PLAN#inputs",
                    "app_id": app_id,
                    **inputs.model_dump(),
                    "submitted_at": now,
                    "validated": True,
                }
            )
        )

        # Update META with confirmed choices
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression="SET engineer_inputs = :ei, inputs_submitted_at = :t",
            ExpressionAttributeValues=to_dynamo(
                {
                    ":ei": inputs.model_dump(),
                    ":t": now,
                }
            ),
        )

        logger.info("Migration plan inputs submitted for %s", app_id)
        return ApiResponse(
            data={
                "app_id": app_id,
                "status": "submitted",
                "validation": validation,
                "message": "Inputs validated and saved. Approve to trigger design document update.",
            }
        )

    except Exception as exc:
        return internal_error(exc, code="SUBMIT_FAILED")


class WavePlanConfig(BaseModel):
    app_ids: list[str]
    max_apps_per_wave: int = 5
    team_size: int = 5
    wave_duration_weeks: int = 4
    pilot_wave_size: int = 2
    dependency_handling: str = "sequential"  # sequential / same_wave
    shared_db_strategy: str = "together"  # together / last


def _plan_and_store_waves(table, now: str) -> dict:
    """Run the playbook planner (core/wave_planner) over the whole estate and persist it.

    Reads every app (paginated GSI1) and their machines' CONN# records (parallel, paginated), stores the
    plan as a WAVE_PLAN item, writes META wave_number for planned apps and clears it for not-ready ones."""
    from boto3.dynamodb.conditions import Key as DDBKey

    from backend.src.core.wave_planner import generate_wave_plan as _gen_wave_plan
    from backend.src.db.accessors import list_machine_connections

    apps = from_dynamo(query_all(table, IndexName="GSI1",
                                 KeyConditionExpression=DDBKey("GSI1PK").eq("TYPE#APP")).get("Items", []))
    connections = list_machine_connections([mid for a in apps for mid in a.get("machine_ids") or []], table=table)
    plan = _gen_wave_plan(apps, connections, config=settings.wave_planner_config())
    table.put_item(Item=to_dynamo({
        "PK": "WAVE_PLAN", "SK": f"PLAN#{now}", "GSI1PK": "TYPE#WAVE_PLAN", "GSI1SK": now,
        "generated_at": now, "planner": "playbook-v1",
        "waves": plan["waves"], "not_ready": plan["not_ready"], "conflicts": plan["conflicts"],
        "rules": plan["rules"], "summary": plan["summary"], "app_dependencies": plan["app_dependencies"],
    }))
    for wave in plan["waves"]:
        for app in wave.get("apps", []):
            table.update_item(
                Key={"PK": f"APP#{app['app_id']}", "SK": "META"},
                UpdateExpression="SET wave_number = :w, wave_plan_id = :wp",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":w": str(wave["wave_number"]), ":wp": now},
            )
    for app in plan["not_ready"]:
        table.update_item(
            Key={"PK": f"APP#{app['app_id']}", "SK": "META"},
            UpdateExpression="SET wave_plan_id = :wp REMOVE wave_number",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":wp": now},
        )
    return plan


@router.post("/wave-plan/generate", dependencies=[require_role(Role.ENGINEER)])
def generate_wave_plan(config: WavePlanConfig):
    """Generate wave plan — runs portfolio assessment and assigns waves.

    1. Determines which portfolio TD to run per app (based on strategy)
    2. Triggers portfolio assessment ECS task
    3. Stores wave configuration for the Planning Agent to use
    """
    if not settings.wave_planning_enabled:
        return error_response(
            "Wave planning is unavailable — no graph backend configured (set GRAPH_BACKEND=dynamodb|neptune).",
            code="WAVE_PLANNING_DISABLED",
        )
    try:
        table = get_table()
        now = datetime.now(UTC).isoformat()

        # Store wave plan config
        table.put_item(
            Item=to_dynamo(
                {
                    "PK": "WAVE_PLAN",
                    "SK": f"CONFIG#{now}",
                    "app_ids": config.app_ids,
                    "max_apps_per_wave": config.max_apps_per_wave,
                    "team_size": config.team_size,
                    "wave_duration_weeks": config.wave_duration_weeks,
                    "pilot_wave_size": config.pilot_wave_size,
                    "dependency_handling": config.dependency_handling,
                    "shared_db_strategy": config.shared_db_strategy,
                    "status": "running",
                    "started_at": now,
                }
            )
        )

        # Determine repos per strategy group for portfolio assessment
        from boto3.dynamodb.conditions import Key

        # Group apps by which portfolio TD they need
        modernization_repos = []  # replatform, rehost, repurchase, relocate
        agentic_repos = []  # refactor

        for app_id in config.app_ids:
            # Get app strategy
            app_resp = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"})
            app_item = app_resp.get("Item", {})
            strategy = app_item.get("migration_strategy", "")

            # Get repos
            repos_resp = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("REPO#"),
            )
            for r in repos_resp.get("Items", []):
                url = r.get("url", "")
                if not url:
                    continue
                if strategy == "refactor":
                    if url not in agentic_repos:
                        agentic_repos.append(url)
                elif strategy not in ("retire", "retain"):
                    if url not in modernization_repos:
                        modernization_repos.append(url)

        # Portfolio-level assessment removed — assessment runs per-app via workflow
        task_arns = []
        logger.info("Wave plan generation: skipping portfolio assessment (per-app via workflow)")

        # Update wave plan status for each app — mark as running
        for app_id in config.app_ids:
            try:
                table.update_item(
                    Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"},
                    UpdateExpression="SET wave_plan_status = :s",
                    ExpressionAttributeValues={":s": "running"},
                )
            except Exception as _err:
                logger.debug("best-effort step ignored: %s", _err)  # App may not have workflow yet

        # Generate wave assignments immediately (don't wait for portfolio assessment)
        # Portfolio assessment enriches the plan later, but waves are assigned now
        try:
            plan = _plan_and_store_waves(table, now)
            logger.info(
                "Wave plan generated: %d waves, %d apps", plan["summary"]["total_waves"], plan["summary"]["total_apps"]
            )
        except Exception as exc:
            logger.warning("Wave plan generation (non-blocking) failed: %s", exc)

        return ApiResponse(
            data={
                "status": "running",
                "app_count": len(config.app_ids),
                "task_arns": task_arns,
                "modernization_repos": len(modernization_repos),
                "agentic_repos": len(agentic_repos),
                "config": config.model_dump(),
            }
        )

    except Exception as exc:
        logger.error("Wave plan generation failed: %s", exc)
        return internal_error(exc, code="WAVE_PLAN_FAILED")


class WavePlanCompleteRequest(BaseModel):
    app_ids: list[str]
    wave_assignments: dict[str, int] = {}  # app_id → wave_number


@router.post("/wave-plan/agent", dependencies=[require_role(Role.ENGINEER)])
def generate_wave_plan_agent():
    """Generate a wave plan deterministically from the dependency graph.

    Uses core/wave_planner (dependency-aware topological assignment) over all
    apps + their machine connections, stores the plan, and writes each app's
    wave assignment. Gated on a configured graph backend.
    """
    if not settings.wave_planning_enabled:
        return error_response(
            "Wave planning is unavailable — no graph backend configured (set GRAPH_BACKEND=dynamodb|neptune).",
            code="WAVE_PLANNING_DISABLED",
        )
    try:
        table = get_table()
        now = datetime.now(UTC).isoformat()
        plan = _plan_and_store_waves(table, now)
        logger.info(
            "Wave plan generated: %d waves, %d apps", plan["summary"]["total_waves"], plan["summary"]["total_apps"]
        )
        return ApiResponse(data={"status": "done", "summary": plan["summary"],
                                 "not_ready": plan["not_ready"], "conflicts": plan["conflicts"]})
    except Exception as exc:
        logger.error("Wave plan agent failed: %s", exc)
        return internal_error(exc, code="WAVE_PLAN_AGENT_FAILED")


@router.post("/wave-plan/complete", dependencies=[require_role(Role.ENGINEER)])
def complete_wave_plan(req: WavePlanCompleteRequest):
    """Record the reviewed wave assignments on each app's META.

    No Step Functions state waits on a wave plan (no ASL state stores a
    wave-plan task token), so this only persists ``wave_number``. ``resumed`` is
    kept in the response for client compatibility and is always 0.
    """
    try:
        table = get_table()
        now = datetime.now(UTC).isoformat()

        assigned = 0
        for app_id in req.app_ids:
            wave_num = req.wave_assignments.get(app_id, 0)
            if not wave_num:
                continue
            table.update_item(
                Key={"PK": f"APP#{app_id}", "SK": "META"},
                UpdateExpression="SET wave_number = :w, wave_plan_id = :wp",
                ExpressionAttributeValues={":w": str(wave_num), ":wp": now},
            )
            assigned += 1

        return ApiResponse(
            data={
                "assigned": assigned,
                "resumed": 0,
                "total": len(req.app_ids),
                "message": f"Wave plan saved. {assigned} app(s) assigned to waves.",
            }
        )

    except Exception as exc:
        logger.error("Wave plan complete failed: %s", exc)
        return internal_error(exc, code="WAVE_PLAN_COMPLETE_FAILED")


@router.get("/wave-plan/portfolio-report")
def get_portfolio_assessment_report():
    """Get the latest portfolio assessment report (modernization-readiness) from S3."""
    try:
        s3 = aws.client("s3")
        bucket = settings.get_s3_bucket()
        prefix = "applications/portfolio/wave-plan/portfolio-modernization-readiness-analysis/"

        # List to find latest report.md
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
        ts_prefixes = sorted(
            [p["Prefix"] for p in resp.get("CommonPrefixes", [])],
            reverse=True,
        )

        for ts_prefix in ts_prefixes:
            # List run dirs under this timestamp
            runs_resp = s3.list_objects_v2(Bucket=bucket, Prefix=ts_prefix, Delimiter="/")
            for run_prefix in runs_resp.get("CommonPrefixes", []):
                report_key = run_prefix["Prefix"] + "report.md"
                try:
                    obj = s3.get_object(Bucket=bucket, Key=report_key)
                    content = obj["Body"].read().decode("utf-8")
                    return ApiResponse(
                        data={
                            "content": content,
                            "s3_key": report_key,
                            "last_modified": obj["LastModified"].isoformat(),
                            "size": obj["ContentLength"],
                        }
                    )
                except Exception as _err:
                    logger.debug("best-effort step skipped: %s", _err)
                    continue

        return error_response("No portfolio assessment report found. Run 'Generate Wave Plan' first.", code="NOT_FOUND")

    except Exception as exc:
        logger.error("Portfolio report fetch failed: %s", exc)
        return internal_error(exc, code="S3_ERROR")


@router.get("/execution/{app_id}")
def get_execution_details(app_id: str):
    """Consolidated execution-phase view for the Execution screen.

    Running view: ECS jobs (task id + status) and per-transformation-step
    progress. Review view: which execution reports (database/infra/cicd) exist
    plus the sub-phase status flags. One round-trip for the whole screen.
    """
    from boto3.dynamodb.conditions import Key

    try:
        table = get_table()
        meta = from_dynamo(table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {})
        wf = from_dynamo(table.get_item(Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"}).get("Item", {}) or {})

        # ECS jobs live in their own JOB#<id> partition; find this app's via the
        # TYPE#JOB GSI and filter by the app_id attribute (GSI1SK is the job_id,
        # so a begins_with(app_id) filter would miss them). ECS tasks are pruned
        # from the console after ~1h, so we surface the (durable) CloudWatch log
        # group + stream instead of a task-console link.
        log_group = settings.ecs_log_group
        jobs = []
        try:
            jresp = table.query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TYPE#JOB"))
            for j in from_dynamo(jresp.get("Items", [])):
                if j.get("app_id") != app_id:
                    continue
                arn = j.get("task_arn", "")
                task_id = arn.split("/")[-1] if arn else ""
                jobs.append({
                    "job_id": j.get("job_id", ""),
                    "task_arn": arn,
                    "task_id": task_id,
                    "status": j.get("status", ""),
                    "mode": j.get("mode", ""),
                    "td_name": j.get("td_name", ""),
                    "created_at": j.get("created_at", ""),
                    "log_group": log_group,
                    # awslogs stream = <stream-prefix>/<container>/<task-id>
                    "log_stream": f"assessment/migration-worker/{task_id}" if task_id else "",
                })
        except Exception as exc:
            logger.warning("execution jobs query failed: %s", exc)
        jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        # Per-step transform progress from the newest transform PLAN# record.
        steps: list = []
        plan_status, plan_id = "", ""
        try:
            presp = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("PLAN#"),
                ScanIndexForward=False,
            )
            plans = from_dynamo(presp.get("Items", []))

            # Legacy plans may lack a plan_id attribute — it's always encoded in
            # the SK (PLAN#<plan_id>), so fall back to that.
            def _pid(p: dict) -> str:
                return p.get("plan_id") or str(p.get("SK", "")).replace("PLAN#", "")

            transform_plans = [p for p in plans if _pid(p).startswith("transform-")] or plans
            if transform_plans:
                plan = max(transform_plans, key=lambda p: p.get("created_at") or "")
                plan_id = _pid(plan)
                plan_status = plan.get("status", "")
                for s in sorted(plan.get("steps", []), key=lambda x: x.get("step_number", 0) or 0):
                    steps.append({
                        "step_number": s.get("step_number"),
                        "td_name": s.get("td_name", ""),
                        "repo_name": s.get("repo_name", ""),
                        "repo_branch": s.get("repo_branch", ""),
                        "status": s.get("status", "pending"),
                        "message": s.get("message", ""),
                        "updated_at": s.get("updated_at", ""),
                        # Per-step S3 report (recorded by the worker on newer runs);
                        # presigned below. Absent for older runs → UI falls back to
                        # the transform-level artifact list.
                        "s3_report_key": s.get("s3_report", ""),
                    })
        except Exception as exc:
            logger.warning("execution plan query failed: %s", exc)

        # Transform ECS task + its S3 artifacts. The transform runs as ONE ECS
        # task executing every step sequentially, so the task/logs belong to the
        # whole run; per-step artifacts (validation summaries) live in S3.
        from backend.src.db import reports as _reports

        transform_job = next((j for j in jobs if j.get("mode") == "workbench"), None)
        artifacts: list = []
        if plan_id:
            try:
                s3c = aws.client("s3")
                pfx = f"applications/{app_id}/execution/transformation/{plan_id}/"
                listing = s3c.list_objects_v2(Bucket=settings.s3_bucket, Prefix=pfx, MaxKeys=500)
                for obj in listing.get("Contents", []):
                    k = obj["Key"]
                    name = k.split("/")[-1]
                    if name not in ("validation_summary.md", "git_instructions.md", "worklog.log"):
                        continue
                    parts = k[len(pfx):].split("/")
                    run_id = parts[1] if len(parts) >= 3 and parts[0] == "artifacts" else ""
                    artifacts.append({
                        "key": k, "name": name, "run_id": run_id,
                        "size": obj.get("Size", 0), "url": _reports.presign(k),
                    })
                artifacts.sort(key=lambda a: (a["run_id"], a["name"]))
            except Exception as exc:
                logger.warning("transform artifact listing failed: %s", exc)

        # Presign per-step reports that the worker recorded (newer runs).
        for s in steps:
            key = s.pop("s3_report_key", "")
            s["s3_report"] = _reports.presign(key) if key else ""

        transform = {
            "plan_id": plan_id,
            "plan_status": plan_status,
            "s3_prefix": f"applications/{app_id}/execution/transformation/{plan_id}/" if plan_id else "",
            "task_id": (transform_job or {}).get("task_id", ""),
            "task_arn": (transform_job or {}).get("task_arn", ""),
            "status": (transform_job or {}).get("status", ""),
            "created_at": (transform_job or {}).get("created_at", ""),
            "log_group": (transform_job or {}).get("log_group", ""),
            "log_stream": (transform_job or {}).get("log_stream", ""),
            "artifacts": artifacts,
        }
        analysis_run_count = sum(1 for j in jobs if j.get("mode") == "code-analysis")

        # EXECUTION# records (newest first) — written by the deterministic
        # infra/CI-CD path in tool_execution and the infra worker. Used for the
        # report fallback below and the repository-commit links.
        try:
            eresp = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("EXECUTION#"),
                ScanIndexForward=False,
            )
            exec_items = from_dynamo(eresp.get("Items", []))
        except Exception as exc:
            logger.warning("execution record query failed: %s", exc)
            exec_items = []

        # Which execution reports exist (latest per type) — the review view.
        reports = []
        for rtype in ("execution-database", "execution-infra", "execution-cicd"):
            try:
                rresp = table.query(
                    KeyConditionExpression=(
                        Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(f"ANALYSIS#{rtype}#")
                    ),
                    ScanIndexForward=False,
                )
                items = from_dynamo(rresp.get("Items", []))
            except Exception:
                items = []
            latest = items[0] if items else None
            if latest:
                reports.append({
                    "type": rtype,
                    "present": True,
                    "status": latest.get("status") or "",
                    "timestamp": latest.get("timestamp") or latest.get("updated_at") or "",
                    "run_id": latest.get("run_id") or "",
                })
                continue
            # Infra/CI-CD are generated deterministically (no LLM report), so build
            # the report from their EXECUTION# records instead of "Not generated".
            synthesized = _execution_report_from_records(rtype, exec_items)
            reports.append(synthesized or {
                "type": rtype, "present": False, "status": "", "timestamp": "", "run_id": "",
            })

        # Repository commits — clickable CodeCommit console links so the engineer
        # can review what the execution pushed (IaC under infra/, CI/CD files,
        # and the transformed code branch). IaC/CI-CD commits carry a commit_id
        # (EXECUTION#iac-commit / #cicd-commit); the transformation is linked to
        # its pushed branch (the worker's commit id is not recorded structurally).
        region = settings.aws_region

        def _commit_url(repo: str, commit_id: str) -> str:
            return (f"https://{region}.console.aws.amazon.com/codesuite/codecommit/repositories/"
                    f"{repo}/commit/{commit_id}?region={region}")

        def _branch_url(repo: str, branch: str) -> str:
            return (f"https://{region}.console.aws.amazon.com/codesuite/codecommit/repositories/"
                    f"{repo}/browse/refs/heads/{branch or 'main'}?region={region}")

        commits: list = []
        try:
            seen_kinds: set = set()
            for it in exec_items:  # newest first → keep latest per kind
                kind, cid, repo = it.get("kind", ""), it.get("commit_id", ""), it.get("repo", "")
                if kind in ("iac-commit", "cicd-commit") and cid and repo and kind not in seen_kinds:
                    seen_kinds.add(kind)
                    commits.append({
                        "kind": "Infrastructure" if kind == "iac-commit" else "CI/CD",
                        "repo": repo, "branch": it.get("branch", ""), "commit_id": cid,
                        "url": _commit_url(repo, cid), "created_at": it.get("created_at", ""),
                    })
        except Exception as exc:
            logger.warning("execution commit query failed: %s", exc)

        # Transformation: code pushed to the migration branch by the workbench worker.
        tx_repo = next((s.get("repo_name") for s in steps if s.get("repo_name")), "")
        tx_branch = next((s.get("repo_branch") for s in steps if s.get("repo_name")), "")
        if tx_repo:
            commits.append({
                "kind": "Transformation", "repo": tx_repo, "branch": tx_branch, "commit_id": "",
                "url": _branch_url(tx_repo, tx_branch), "created_at": "",
            })

        return ApiResponse(data={
            "app_id": app_id,
            "execution_arn": wf.get("execution_arn", ""),
            "current_phase": wf.get("current_phase", ""),
            "current_step": wf.get("current_step", ""),
            # Deterministic execution findings (provider/repo-host mismatch, DB
            # report vs confirmed target) — shown above the progress pills.
            "warnings": [w for w in (
                meta.get("exec_cicd_warning") or "",
                *(meta.get("execution_database_warnings") or []),
            ) if w],
            "sub_status": {
                "database": meta.get("exec_database_status", ""),
                "infra": meta.get("exec_infra_status", ""),
                "cicd": meta.get("exec_cicd_status", ""),
            },
            "jobs": jobs[:10],
            "steps": steps,
            "plan_id": plan_id,
            "plan_status": plan_status,
            "transform": transform,
            "analysis_run_count": analysis_run_count,
            "reports": reports,
            "commits": commits,
        })
    except Exception as exc:
        logger.error("get_execution_details failed: %s", exc)
        return internal_error(exc, code="EXECUTION_DETAILS_FAILED")


_EXEC_REPORT_KINDS = {
    "execution-infra": ("Infrastructure (IaC)", "iac", "iac-commit", "infra-validate"),
    "execution-cicd": ("CI/CD Pipeline", "cicd", "cicd-commit", None),
}


def _execution_report_from_records(rtype: str, exec_items: list) -> dict | None:
    """Build an execution report entry (with inline markdown) from the latest
    EXECUTION# records of a deterministic sub-step. None if nothing was staged."""
    spec = _EXEC_REPORT_KINDS.get(rtype)
    if not spec:
        return None
    title, stage_kind, commit_kind, validate_kind = spec

    def latest(kind):
        return next((it for it in exec_items if kind and it.get("kind") == kind), None)

    staged, commit, validate = latest(stage_kind), latest(commit_kind), latest(validate_kind)
    if not staged:
        return None

    status = (validate or {}).get("status") or (commit or {}).get("status") or staged.get("status") or "staged"
    timestamp = max(r.get("created_at", "") for r in (staged, commit, validate) if r)

    lines = [f"# {title}", "", "| Field | Value |", "|---|---|", f"| Status | {status} |"]
    for label, key in (("Target compute", "target_compute"), ("Provider", "provider")):
        if staged.get(key):
            lines.append(f"| {label} | {staged[key]} |")
    lines.append(f"| Staged at | {staged.get('created_at', '')} |")
    lines.append(f"| S3 prefix | `{staged.get('s3_prefix', '')}` |")
    if commit:
        lines += [
            f"| Repository | {commit.get('repo', '')} |",
            f"| Branch | {commit.get('branch', '')} |",
            f"| Commit | `{commit.get('commit_id', '')}` |",
            f"| Committed at | {commit.get('created_at', '')} |",
        ]
    if validate:
        lines += [
            f"| terraform validate | {'passed' if validate.get('validate_ok') else 'failed'} |",
            f"| terraform plan exit code | {validate.get('plan_exit', '')} |",
        ]
    files = (commit or {}).get("files") or staged.get("files") or []
    if files:
        lines += ["", f"## Files ({len(files)})", ""] + [f"- `{f}`" for f in files]
    warnings = staged.get("warnings") or []
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    unresolved = staged.get("unresolved_vars") or []
    if unresolved:
        lines += ["", "## Unresolved template variables", ""] + [f"- `{v}`" for v in unresolved]
    notes = staged.get("notes")
    if notes:
        lines += ["", "## Notes", ""] + ([f"- {n}" for n in notes] if isinstance(notes, list) else [str(notes)])

    return {
        "type": rtype, "present": True, "status": status, "timestamp": timestamp,
        "run_id": "", "content": "\n".join(lines),
    }


def current_run_pending_tokens(token_items: list[dict], started_at: str) -> tuple[list[dict], int]:
    """Filter task-token records down to the CURRENT execution's gates.

    Task tokens (``TASK_TOKEN#<step>``) are deleted only when that step is
    approved, so a run that fails, is aborted, or is superseded leaves its gates
    behind indefinitely. Offering all of them lets the UI surface gates from
    previous runs — clicking one sends SendTaskSuccess against a token whose
    execution is long gone and the workflow looks broken.

    A token carries no execution reference, so the run's start time is the
    discriminator: a token received before this execution began cannot belong to
    it. Compare on the first 19 chars of the ISO timestamp — tokens are written
    as '...Z' and the workflow record as '...+00:00' with microseconds, but both
    are UTC. Fails OPEN: no ``started_at``, no ``received_at``, or a same-second
    boundary keeps the gate — hiding a live gate would hang the workflow with no
    way to approve it, far worse than one extra row.

    Returns ``(kept_pending, stale_hidden_count)`` with pending newest-first.
    """
    start = str(started_at or "")[:19]
    kept, stale = [], 0
    for item in token_items:
        received = str(item.get("received_at") or "")[:19]
        if start and received and received < start:
            stale += 1
            continue
        entry = {
            "step": item.get("step", ""),
            "message": item.get("message", ""),
            "received_at": item.get("received_at", ""),
        }
        # R1/R2 (amf-decision-support): if the requesting phase attached a
        # standardized decision_summary to the gate token, carry it through the
        # reshape untouched. This function rebuilds a trimmed dict per token, so
        # without this the field would be silently dropped here even though it is
        # stored on the item. Omitted when absent — backward compatible.
        summary = item.get("decision_summary")
        if isinstance(summary, dict) and summary:
            entry["decision_summary"] = summary
        kept.append(entry)
    kept.sort(key=lambda p: str(p.get("received_at") or ""), reverse=True)
    return kept, stale


@router.get("/pending/{app_id}")
def get_pending_approvals(app_id: str):
    """Get the pending approval steps for an application's CURRENT execution.

    See ``current_run_pending_tokens`` for why gates from prior runs are hidden
    and why the filter deliberately fails open.

    If the phase that requested this gate attached a ``decision_summary``
    (see the decision-support design notes R1/R2), it is stored
    verbatim on the TASK_TOKEN# item and carried through to the response by
    ``current_run_pending_tokens`` (which rebuilds a trimmed dict per token and
    copies ``decision_summary`` when present).
    Callers should treat ``decision_summary`` as optional: older/other gates
    that never set it simply omit the key.
    """
    try:
        from boto3.dynamodb.conditions import Key

        table = get_table()
        resp = table.query(
            KeyConditionExpression=(Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("TASK_TOKEN#")),
        )
        items = from_dynamo(resp.get("Items", []))

        started_at = ""
        try:
            wf = table.get_item(
                Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"}
            ).get("Item") or {}
            started_at = str(wf.get("started_at") or "")
        except Exception as exc:  # noqa: BLE001 — degrade to showing everything
            logger.warning("could not read active workflow for %s: %s", app_id, exc)

        pending, stale = current_run_pending_tokens(items, started_at)
        if stale:
            logger.info("app=%s hid %d pending approval(s) from executions before %s", app_id, stale, started_at[:19])
        return ApiResponse(data=pending, meta={"stale_hidden": stale})
    except Exception as exc:
        return internal_error(exc, code="QUERY_FAILED")


def _terraform_errors(app_id: str, limit: int = 5) -> list[str]:
    """First `Error:` blocks from the worker's terraform validate/plan output (best-effort)."""
    s3 = aws.client("s3")
    errors: list[str] = []
    for name in ("validate.txt", "plan.txt"):
        try:
            body = s3.get_object(
                Bucket=settings.get_s3_bucket(), Key=f"applications/{app_id}/execution/infrastructure/{name}",
            )["Body"].read().decode("utf-8", "replace")
        except Exception as _err:
            logger.debug("best-effort step skipped: %s", _err)
            continue
        for block in body.split("\nError: ")[1:]:
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            title = lines[0] if lines else ""
            where = next((ln for ln in lines[1:3] if ln.startswith("on ")), "")
            errors.append(f"terraform {name.split('.')[0]}: {title}" + (f" ({where})" if where else ""))
            if len(errors) >= limit:
                return errors
    return errors


@router.get("/failure/{app_id}")
def get_failure_reason(app_id: str):
    """Why the app's current run failed — the step, error and blockers.

    The ASL ends every failure in a generic Fail state, so the reason is
    reconstructed from the execution history (core/failure_reason.py).
    """
    from backend.src.core.failure_reason import explain_failure

    try:
        wf = get_table().get_item(Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"}).get("Item") or {}
        execution_arn = wf.get("execution_arn", "")
        if not execution_arn:
            return error_response(f"No workflow run for {app_id}", code="NOT_FOUND", status_code=404)
        sfn = aws.client("stepfunctions")
        status = sfn.describe_execution(executionArn=execution_arn).get("status", "")
        if status not in ("FAILED", "ABORTED", "TIMED_OUT"):
            return ApiResponse(data={"failed": False, "status": status, "execution_arn": execution_arn})
        # The failing state is always among the most recent events.
        events = sfn.get_execution_history(
            executionArn=execution_arn, maxResults=settings.failure_history_events, reverseOrder=True,
        ).get("events", [])
        detail = explain_failure(events)
        if detail.get("failed_state") == "Check_InfrastructureValidate":
            detail["reasons"].extend(_terraform_errors(app_id))
        return ApiResponse(data={"failed": True, "status": status, "execution_arn": execution_arn, **detail})
    except Exception as exc:
        logger.error("failure reason for %s: %s", app_id, exc)
        return internal_error(exc, code="FAILURE_LOOKUP_FAILED")


@router.get("/tool-calls/{app_id}")
def get_tool_calls(app_id: str, phase: str = ""):
    """Return the captured tool-call traces for an app (optionally one phase).

    Each MCP tool call is persisted (name, target, input, output, latency, phase)
    so the UI can show per-phase tool logs/reasoning without leaving the app.
    """
    try:
        from boto3.dynamodb.conditions import Key

        table = get_table()
        sk_prefix = f"TOOLCALL#{phase}" if phase else "TOOLCALL#"
        resp = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(sk_prefix),
            ScanIndexForward=False,  # newest first
        )
        calls = []
        for it in from_dynamo(resp.get("Items", [])):
            # input/output are stored as JSON strings — parse for the UI, fall back to raw.
            def _parse(v):
                try:
                    return json.loads(v) if isinstance(v, str) else v
                except Exception:
                    return v

            calls.append(
                {
                    "tool": it.get("tool", ""),
                    "target": it.get("target", ""),
                    # Recorded by agent_tools since 2026-10-13; older traces fall
                    # back to the UI's static phase plan.
                    "caller": it.get("caller", ""),
                    "phase": it.get("phase", ""),
                    "status": it.get("status", ""),
                    "latency_ms": it.get("latency_ms", 0),
                    "at": it.get("at", ""),
                    "input": _parse(it.get("input")),
                    "output": _parse(it.get("output")),
                }
            )
        return ApiResponse(data=calls, meta={"count": len(calls)})
    except Exception as exc:
        logger.error("get_tool_calls failed: %s", exc)
        return internal_error(exc, code="QUERY_FAILED")


# Terminal statuses as stored in WORKFLOW#ACTIVE (and raw SFN names): an execution
# in one of these never changes again, so /active doesn't DescribeExecution it.
_TERMINAL_WORKFLOW_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "SUCCEEDED", "TIMED_OUT", "ABORTED"})
_active_cache: dict = {"at": 0.0, "data": None}
_active_cache_lock = threading.Lock()


def invalidate_active_workflows_cache() -> None:
    """Drop the /active cache (a run started / cancelled — the list must refresh now)."""
    with _active_cache_lock:
        _active_cache["at"] = 0.0
        _active_cache["data"] = None


def _live_status(sfn, exec_arn: str, stored: str) -> str:
    """Execution status from SFN (authority), mapped onto the stored vocabulary."""
    try:
        sfn_status = sfn.describe_execution(executionArn=exec_arn)["status"]
    except Exception:
        return stored  # SFN unreachable/expired — fall back to cache
    return "RUNNING" if sfn_status == "RUNNING" else "COMPLETED" if sfn_status == "SUCCEEDED" else "FAILED"


def _build_active_workflows() -> list[dict]:
    sfn = aws.client("stepfunctions")
    table = get_table()

    from boto3.dynamodb.conditions import Key

    # Single bounded GSI Query for every workflow record (no Scan).
    wf_items = []
    query_kwargs = {
        "IndexName": "GSI1",
        "KeyConditionExpression": Key("GSI1PK").eq(TYPE_WORKFLOW),
    }
    while True:
        wf_resp = table.query(**query_kwargs)
        wf_items.extend(wf_resp.get("Items", []))
        if "LastEvaluatedKey" not in wf_resp:
            break
        query_kwargs["ExclusiveStartKey"] = wf_resp["LastEvaluatedKey"]

    records: list[tuple[str, dict]] = []
    seen_apps: set[str] = set()
    for wf_item in from_dynamo(wf_items):
        app_id = wf_item.get("app_id") or wf_item.get("PK", "").replace("APP#", "")
        if not app_id or app_id in seen_apps:
            continue
        seen_apps.add(app_id)
        records.append((app_id, wf_item))

    # Live status from SFN only for records not already terminal in DynamoDB
    # (never override a user CANCELLED), in parallel with a bounded pool.
    to_describe = [
        (app_id, wf_item) for app_id, wf_item in records
        if wf_item.get("execution_arn") and str(wf_item.get("status", "")).upper() not in _TERMINAL_WORKFLOW_STATUSES
    ]
    live: dict[str, str] = {}
    if to_describe:
        workers = max(1, min(settings.active_workflows_max_workers, len(to_describe)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                app_id: pool.submit(_live_status, sfn, wf_item["execution_arn"], wf_item.get("status", ""))
                for app_id, wf_item in to_describe
            }
            live = {app_id: f.result() for app_id, f in futures.items()}

    result = []
    for app_id, wf_item in records:
        exec_arn = wf_item.get("execution_arn", "")
        stored = wf_item.get("status", "")
        status = live.get(app_id, stored)
        # Opportunistic cache refresh (best-effort; correctness independent).
        if status != stored:
            try:
                table.update_item(
                    Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"},
                    UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": status},
                )
            except Exception as _err:
                logger.debug("best-effort step ignored: %s", _err)

        pending = []
        if status == "RUNNING":
            token_resp = table.query(
                KeyConditionExpression=(Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("TASK_TOKEN#")),
            )
            # Same current-run filter as /pending: this is the list the UI
            # actually renders, so stale gates from prior runs must be hidden
            # here too (fail-open — see current_run_pending_tokens).
            pending, stale = current_run_pending_tokens(
                from_dynamo(token_resp.get("Items", [])), wf_item.get("started_at", "")
            )
            if stale:
                logger.info("app=%s hid %d stale pending approval(s) from /active", app_id, stale)

        result.append(
            {
                "app_id": app_id,
                "status": status or "COMPLETED",
                "execution_arn": exec_arn,
                "current_phase": wf_item.get("current_phase", "discovery" if status == "RUNNING" else "completed"),
                "current_step": wf_item.get("current_step", ""),
                "started_at": wf_item.get("started_at", ""),
                "waiting_since": wf_item.get("waiting_since", "") if status == "RUNNING" else "",
                "pending": pending,
            }
        )
    return result


@router.get("/active")
def get_active_workflows():
    """List all workflows, with Step Functions as the live status authority.

      * WORKFLOW#ACTIVE records are read via a paginated GSI1 Query on the
        TYPE#WORKFLOW partition (no Scan).
      * Execution status is resolved live from SFN describe_execution — SFN is
        the source of truth, the DDB status column a cache refreshed when it
        disagrees. Records already terminal in DynamoDB are not described (a
        finished execution never changes); the rest are described in parallel.
      * The whole list is cached for `active_workflows_cache_seconds` (the UI
        polls it); starting / cancelling a run invalidates the cache.

    Sync handler: FastAPI runs it in its threadpool, so the boto3 calls don't
    block the event loop.
    """
    try:
        ttl = settings.active_workflows_cache_seconds
        now = time.monotonic()
        with _active_cache_lock:
            cached = _active_cache["data"]
            if cached is not None and ttl > 0 and now - _active_cache["at"] < ttl:
                return ApiResponse(data=cached)
        result = _build_active_workflows()
        with _active_cache_lock:
            _active_cache["data"] = result
            _active_cache["at"] = time.monotonic()
        return ApiResponse(data=result)
    except Exception as exc:
        logger.error("get_active_workflows failed: %s", exc)
        return internal_error(exc, code="ACTIVE_FAILED")


class TeardownRequest(BaseModel):
    app_id: str
    confirm: str  # must equal app_id — destroying a stack is never a single click


@router.post("/teardown")
def teardown_app(req: TeardownRequest, user=require_role(Role.ADMIN)):
    """Tear down an app's deployed stack (terraform destroy via the infra worker).

    Explicit and separate from the journey: Admin only, the caller
    types the app id, and tool_execution re-checks the deploy allowlist.
    """
    if not settings.deploy_enabled:
        return error_response("Deploy is disabled (enable_deploy = false): AMF deployed nothing, so there is "
                              "nothing to tear down.", code="DEPLOY_DISABLED")
    if req.confirm != req.app_id:
        return error_response("Type the app id to confirm the teardown.", code="TEARDOWN_NOT_CONFIRMED")
    try:
        resp = boto3.client("lambda", region_name=settings.aws_region).invoke(
            FunctionName=settings.tool_execution_lambda,
            Payload=json.dumps({"tool_name": "destroy_infrastructure",
                                "arguments": {"app_id": req.app_id, "confirm": req.confirm}}).encode(),
        )
        out = json.loads(resp["Payload"].read() or b"{}")
        out = json.loads(out["body"]) if isinstance(out, dict) and isinstance(out.get("body"), str) else out
        if (out or {}).get("status") == "error":
            return error_response(out.get("message") or "teardown refused", code="TEARDOWN_REFUSED")
        logger.info("Teardown started for %s by %s", req.app_id, getattr(user, "email", "unknown"))
        return ApiResponse(data={"app_id": req.app_id, "status": "destroying", "job": out})
    except Exception as exc:  # noqa: BLE001
        logger.error("teardown %s: %s", req.app_id, exc)
        return internal_error(exc, code="TEARDOWN_FAILED")

