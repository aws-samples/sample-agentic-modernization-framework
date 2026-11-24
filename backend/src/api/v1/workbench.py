"""
Workbench API.

  GET /transformations → list available ATX transformations (atx custom def list).

Transformation plans (PLAN#) are created by the tool_execution Lambda when the
Step Functions Execution phase launches the ATX workbench worker; this module
only reconciles stuck ones on startup (recover_stuck_plans).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from boto3.dynamodb.conditions import Key
from fastapi import APIRouter

from backend.src.config import settings
from backend.src.core import aws
from backend.src.core.models import ApiResponse, generic_error_message
from backend.src.db.helpers import from_dynamo, get_table, query_all, to_dynamo

logger = logging.getLogger(__name__)
router = APIRouter()


# ── GET /transformations — list available ATX transformations ──────────


_TD_CACHE: dict = {"at": 0.0, "items": []}


def reset_atx_list_throttle() -> None:
    """Forget the cached ATX catalog (tests)."""
    _TD_CACHE["at"] = 0.0
    _TD_CACHE["items"] = []


def _atx_list_due(refresh: bool, now: float) -> bool:
    """Re-run `atx custom def list`? On an empty / expired cache, or on refresh=true —
    but a refresh at most once per `atx_list_min_refresh_seconds` (each run is a slow CLI call)."""
    if not _TD_CACHE["items"]:
        return True
    age = now - _TD_CACHE["at"]
    if age > settings.atx_list_cache_ttl_seconds:
        return True
    return refresh and age >= settings.atx_list_min_refresh_seconds


def _is_managed(name: str) -> bool:
    """AWS-managed and generally available to customers — not AWS-internal, not user-published."""
    return name.startswith("AWS/") and not name.startswith("AWS/internal-")


def _atx_list() -> list[dict]:
    """`atx custom def list --json` → raw items (raises RuntimeError with a user-facing message)."""
    import os
    import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed

    # AWS Transform is only available in certain regions; pin the CLI to the
    # configured region so it does not inherit an unsupported shell default.
    env = {**os.environ, "AWS_REGION": settings.aws_region, "AWS_DEFAULT_REGION": settings.aws_region}
    try:
        result = subprocess.run(  # nosec B603 B607 — literal argv (no user input), no shell, config timeout
            ["atx", "custom", "def", "list", "--json"],
            capture_output=True, text=True, timeout=settings.atx_list_timeout_seconds, env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ATX CLI not installed. Install from https://transform-cli.awsstatic.com/install.sh") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ATX CLI timed out after {settings.atx_list_timeout_seconds}s") from exc
    # Raw CLI output is logged, not put in the (client-facing) error message.
    if result.returncode != 0 or not result.stdout.strip():
        logger.error("atx custom def list failed (rc=%s): %s", result.returncode, result.stderr[:2000])
        raise RuntimeError(f"ATX CLI failed (rc={result.returncode}); see the API log")
    # The ATX CLI prints status text / Node version warnings to stdout
    # before the JSON payload, so parse only the JSON object/array
    # rather than the whole stream.
    raw = _extract_json(result.stdout)
    if raw is None:
        logger.error("atx custom def list returned no JSON: %s", result.stdout.strip()[:2000])
        raise RuntimeError("ATX returned no JSON payload; see the API log")
    # ATX returns {"transformations": [...]}
    items = raw.get("transformations", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    if not items:
        raise RuntimeError("ATX returned empty transformation list")
    return items


@router.get("/transformations")
async def list_transformations(scope: str = "managed", refresh: bool = False):
    """
    List available ATX transformation definitions (live: `atx custom def list --json`, cached).

    scope=managed (default) keeps the AWS-managed definitions; the registry also returns
    hundreds of definitions other users published (owner = their alias), which are not
    offered as migration steps. scope=all returns everything.
    """
    import asyncio
    import time

    try:
        if _atx_list_due(refresh, time.time()):
            _TD_CACHE["items"] = await asyncio.to_thread(_atx_list)
            _TD_CACHE["at"] = time.time()
    except RuntimeError as exc:
        if not _TD_CACHE["items"]:
            # Curated _atx_list messages (no raw CLI output): safe and actionable.
            return ApiResponse(status="error", data={"message": str(exc)})
        logger.warning("ATX list refresh failed, serving the cached list: %s", exc)
    except Exception as exc:
        return ApiResponse(status="error", data={"message": generic_error_message(exc, "ATX_CLI_ERROR")})

    transformations = [
        {
            "name": item.get("name", ""),
            "display_name": item.get("name", "").replace("AWS/", "").replace("-", " ").title(),
            "description": item.get("description", ""),
            "version": item.get("version", ""),
            "category": _categorize_transformation(item.get("name", "")),
        }
        for item in _TD_CACHE["items"]
        if scope == "all" or _is_managed(item.get("name", ""))
    ]
    return ApiResponse(data=transformations)


def _extract_json(text: str):
    """Extract the first JSON object/array from a stream that may be prefixed
    with non-JSON noise (status lines, ANSI-coloured Node version warnings).
    Returns the parsed value or None if no valid JSON can be found."""
    decoder = json.JSONDecoder()
    # The CLI emits the payload on its own line, so try whole lines first —
    # this avoids matching '[' inside ANSI escape codes (e.g. "\x1b[33m").
    for line in text.splitlines():
        stripped = line.strip()
        if stripped[:1] in ("{", "["):
            try:
                obj, _ = decoder.raw_decode(stripped)
                return obj
            except json.JSONDecodeError:
                continue
    # Fallback for pretty-printed (multi-line) JSON: strip ANSI escapes, then
    # decode from the first real opener to the end of the stream.
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    candidates = [i for i in (clean.find("{"), clean.find("[")) if i != -1]
    if candidates:
        try:
            obj, _ = decoder.raw_decode(clean[min(candidates) :])
            return obj
        except json.JSONDecodeError:
            pass
    return None


def _categorize_transformation(name: str) -> str:
    """Categorize a transformation by its name."""
    n = name.lower()
    if "analysis" in n or "codebase" in n:
        return "analysis"
    elif "upgrade" in n or "version" in n:
        return "upgrade"
    elif "moderniz" in n or "containeriz" in n:
        return "modernize"
    elif "security" in n or "remediat" in n:
        return "security"
    elif "sdk" in n or "migration" in n:
        return "migration"
    return "transform"


# ── Startup recovery ──────────────────────────────────────────────────
# PLAN# records are written by tool_execution (Execution's LaunchTransformation
# workbench run) and updated by the worker; this reconciles any left "running"
# after their ECS task is gone.


def recover_stuck_plans() -> None:
    """
    Called on app startup. Finds plans stuck in "running" and checks
    if their ECS tasks are actually running. If not, marks them as failed.
    """
    try:
        table = get_table()

        # Query all plans via GSI1
        resp = query_all(
            table,
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq("TYPE#PLAN"),
        )
        plans = from_dynamo(resp.get("Items", []))
        stuck_plans = [p for p in plans if p.get("status") == "running"]

        if not stuck_plans:
            logger.info("Plan recovery: no stuck plans found")
            return

        logger.info(
            "Plan recovery: found %d stuck plan(s): %s",
            len(stuck_plans),
            [p.get("plan_id") for p in stuck_plans],
        )

        ecs = aws.client("ecs")

        for plan in stuck_plans:
            plan_id = plan.get("plan_id", "")
            task_arns = plan.get("task_arns", [])

            if not task_arns:
                # No task ARNs stored — mark as failed
                logger.warning("Plan recovery: %s has no task_arns, marking failed", plan_id)
                _mark_plan_failed(plan, "No ECS task information available (server restarted)")
                continue

            try:
                ecs_resp = ecs.describe_tasks(
                    cluster=settings.atx_ecs_cluster,
                    tasks=task_arns if isinstance(task_arns, list) else [task_arns],
                )
                tasks = ecs_resp.get("tasks", [])

                if not tasks:
                    # Tasks not found (expired from ECS, older than 1hr)
                    logger.warning("Plan recovery: %s tasks not found in ECS, marking failed", plan_id)
                    _mark_plan_failed(plan, "ECS tasks no longer exist (expired or server restarted)")
                    continue

                all_stopped = all(t.get("lastStatus") == "STOPPED" for t in tasks)
                any_running = any(t.get("lastStatus") in ("RUNNING", "PENDING") for t in tasks)

                if all_stopped:
                    # Tasks finished but poller died — check exit codes
                    all_success = True
                    for t in tasks:
                        for c in t.get("containers", []):
                            if c.get("exitCode", -1) != 0:
                                all_success = False

                    if all_success:
                        logger.info("Plan recovery: %s tasks completed successfully", plan_id)
                        _mark_plan_completed(plan)
                    else:
                        logger.warning("Plan recovery: %s tasks failed", plan_id)
                        _mark_plan_failed(plan, "ECS task exited with non-zero code")
                elif any_running:
                    # Tasks still running — leave as-is, they'll callback when done
                    logger.info("Plan recovery: %s tasks still running, leaving as-is", plan_id)
                else:
                    _mark_plan_failed(plan, "ECS tasks in unknown state")

            except Exception as ecs_exc:
                logger.warning("Plan recovery: ECS check failed for %s: %s", plan_id, ecs_exc)
                _mark_plan_failed(plan, f"Could not verify ECS task status: {ecs_exc}")

    except Exception as exc:
        logger.error("Plan recovery error: %s", exc)


def _mark_plan_failed(plan: dict, reason: str) -> None:
    """Mark a plan as failed with reason."""
    try:
        table = get_table()
        now = datetime.now(UTC).isoformat()
        app_id = plan.get("app_id", "")
        plan_id = plan.get("plan_id", "")

        # Update step statuses
        steps = plan.get("steps", [])
        for s in steps:
            if s.get("status") != "completed":
                s["status"] = "failed"

        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": f"PLAN#{plan_id}"},
            UpdateExpression=("SET #st = :s, stopped_reason = :sr, steps = :steps, updated_at = :ua"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=to_dynamo(
                {
                    ":s": "failed",
                    ":sr": reason,
                    ":steps": steps,
                    ":ua": now,
                }
            ),
        )
    except Exception as exc:
        logger.error("Failed to mark plan %s as failed: %s", plan.get("plan_id"), exc)


def _mark_plan_completed(plan: dict) -> None:
    """Mark a plan as completed."""
    try:
        table = get_table()
        now = datetime.now(UTC).isoformat()
        app_id = plan.get("app_id", "")
        plan_id = plan.get("plan_id", "")

        steps = plan.get("steps", [])
        for s in steps:
            s["status"] = "completed"

        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": f"PLAN#{plan_id}"},
            UpdateExpression=("SET #st = :s, steps = :steps, completed_at = :ca, updated_at = :ua"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=to_dynamo(
                {
                    ":s": "completed",
                    ":steps": steps,
                    ":ca": now,
                    ":ua": now,
                }
            ),
        )
    except Exception as exc:
        logger.error("Failed to mark plan %s as completed: %s", plan.get("plan_id"), exc)
