"""
Portfolio / program-level views (Requirements 65–68).

Program-wide, cross-application views that complement the per-application
workflow screens: a portfolio dashboard, wave-level progress, an at-risk view,
and burndown/velocity metrics. All data is derived from the Portfolio_Table.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Query

from backend.src.config import settings
from backend.src.core.models import ApiResponse, internal_error
from backend.src.db import accessors, keys

logger = logging.getLogger(__name__)
router = APIRouter()

# Approval considered "aging" (at-risk) beyond this many hours.
APPROVAL_AGING_HOURS_DEFAULT = settings.approval_aging_hours

FINAL_DONE = {"completed"}
FAILED = {"failed", "error"}
CANCELLED = {"cancelled", "aborted", "canceled"}


def _status_label(app: dict, wf: dict | None, pending: list[dict]) -> str:
    """Single status vocabulary matching the frontend Status_Label_Source.

    Only returns: Running, Awaiting Approval, Done, Cancelled, Failed.
    """
    raw = (wf or {}).get("status", "") or app.get("status", "")
    raw_l = str(raw).lower()
    step = str((wf or {}).get("current_step", "")).lower()
    phase = str((wf or {}).get("current_phase", "")).lower()

    if raw_l in FAILED:
        return "Failed"
    if raw_l in CANCELLED:
        return "Cancelled"
    if pending or "review" in step or "approval" in step:
        return "Awaiting Approval"
    if raw_l in FINAL_DONE or phase == "completed" or app.get("status") == "completed":
        return "Done"
    return "Running"


def _load_program() -> list[dict]:
    """Load every application with its workflow state and pending approvals."""
    # Use the typed single-table accessors (keys + GSI1 accessors) so the
    # physical layout lives in one place (backend/src/db/keys.py, accessors.py).
    apps = accessors.list_all_applications()
    rows = []
    for app in apps:
        app_id = app.get("app_id") or keys.app_id_from_pk(app.get("PK", ""))
        if not app_id:
            continue
        wf = accessors.get_active_workflow(app_id)
        pending = accessors.list_app_children(app_id, sk_prefix="TASK_TOKEN#")
        rows.append({
            "app_id": app_id,
            "app_name": app.get("app_name", app_id),
            "business_unit": app.get("business_unit", ""),
            "strategy": app.get("migration_strategy", ""),
            "wave": app.get("wave_number"),
            "phase": (wf or {}).get("current_phase", ""),
            "step": (wf or {}).get("current_step", ""),
            "status": _status_label(app, wf, pending),
            "pending": pending,
            "completed_at": app.get("completed_at") or app.get("handover_at", ""),
            "_app": app,
            "_wf": wf,
        })
    return rows


@router.get("/dashboard")
def portfolio_dashboard():
    """All applications with current phase, status, strategy, and wave (R65)."""
    try:
        rows = _load_program()
        for r in rows:
            r.pop("_app", None)
            r.pop("_wf", None)
            r.pop("pending", None)
        return ApiResponse(data={"applications": rows, "count": len(rows)})
    except Exception as exc:
        logger.error("portfolio_dashboard: %s", exc)
        return internal_error(exc, code="DASHBOARD_FAILED")


def _rollup_waves(rows: list[dict]) -> list[dict]:
    """Pure per-wave rollup by status + completion % (R66)."""
    waves: dict = {}
    for r in rows:
        w = r.get("wave")
        key = f"Wave {w}" if w not in (None, "") else "Unassigned"
        bucket = waves.setdefault(key, {"wave": w, "label": key, "total": 0,
                                        "Running": 0, "Awaiting Approval": 0, "Done": 0, "Cancelled": 0, "Failed": 0})
        bucket["total"] += 1
        bucket[r["status"]] = bucket.get(r["status"], 0) + 1
    for b in waves.values():
        b["completion_pct"] = round(b["Done"] / b["total"] * 100) if b["total"] else 0
    return sorted(waves.values(), key=lambda b: (b["wave"] is None, b["wave"] if b["wave"] is not None else 0))


@router.get("/waves")
def wave_progress():
    """Per-wave counts by status and completion ratio (R66)."""
    try:
        return ApiResponse(data={"waves": _rollup_waves(_load_program())})
    except Exception as exc:
        logger.error("wave_progress: %s", exc)
        return internal_error(exc, code="WAVES_FAILED")


def _classify_at_risk(rows: list[dict], aging_hours: int, now: datetime | None = None) -> list[dict]:
    """Pure at-risk classification (failed / blocked / approval-aging) (R67)."""
    now = now or datetime.now(UTC)
    at_risk_list = []
    for r in rows:
        reasons = []
        if r["status"] == "Failed":
            reasons.append(f"Phase failed: {(r['_wf'] or {}).get('error_msg', r.get('phase') or 'unknown')}")
        app = r.get("_app") or {}
        if str(app.get("exec_infra_status", "")).lower() == "blocked":
            reasons.append("Infrastructure blocked — missing required inputs")
        for p in r.get("pending", []):
            recv = p.get("received_at", "")
            try:
                age = (now - datetime.fromisoformat(recv.replace("Z", "+00:00"))).total_seconds() / 3600
                if age >= aging_hours:
                    reasons.append(f"Approval '{p.get('step')}' pending {int(age)}h (> {aging_hours}h)")
            except Exception as _err:
                logger.debug("best-effort step ignored: %s", _err)
        if reasons:
            at_risk_list.append({
                "app_id": r["app_id"], "app_name": r["app_name"], "phase": r["phase"],
                "status": r["status"], "wave": r["wave"], "reasons": reasons,
            })
    return at_risk_list


@router.get("/at-risk")
def at_risk(aging_hours: int = Query(APPROVAL_AGING_HOURS_DEFAULT, ge=1, le=720)):
    """Applications that are failed, blocked, or awaiting approval too long (R67)."""
    try:
        at_risk_list = _classify_at_risk(_load_program(), aging_hours)
        return ApiResponse(data={"at_risk": at_risk_list, "count": len(at_risk_list), "aging_hours": aging_hours})
    except Exception as exc:
        logger.error("at_risk: %s", exc)
        return internal_error(exc, code="AT_RISK_FAILED")


def _compute_metrics(rows: list[dict], weeks: int, now: datetime | None = None) -> dict:
    """Pure completion + burndown + velocity derivation (R68)."""
    now = now or datetime.now(UTC)
    total = len(rows)
    done = [r for r in rows if r["status"] == "Done"]
    completed = len(done)

    by_week_completed: dict[int, int] = {}
    parsed_completions = 0
    for r in done:
        ts = r.get("completed_at") or ""
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            wk = int((now - dt).days // 7)
            if 0 <= wk < weeks:
                by_week_completed[wk] = by_week_completed.get(wk, 0) + 1
                parsed_completions += 1
        except Exception as _err:
            logger.debug("best-effort step ignored: %s", _err)

    # Burndown: remaining apps at the end of each of the last `weeks` weeks.
    burndown = []
    for wk in range(weeks - 1, -1, -1):
        remaining_at_week = total - sum(c for w, c in by_week_completed.items() if w >= wk)
        burndown.append({"weeks_ago": wk, "remaining": max(remaining_at_week, 0)})
    velocity = round(parsed_completions / weeks, 2) if weeks else 0

    counts = {"Running": 0, "Awaiting Approval": 0, "Done": 0, "Cancelled": 0, "Failed": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "total": total,
        "completed": completed,
        "completion_pct": round(completed / total * 100) if total else 0,
        "status_counts": counts,
        "velocity_per_week": velocity,
        "burndown": burndown,
        "insufficient_data": parsed_completions == 0 and completed > 0,
    }


@router.get("/metrics")
def program_metrics(weeks: int = Query(settings.program_metrics_default_weeks, ge=1,
                                       le=settings.program_metrics_max_weeks)):
    """Portfolio completion + burndown + velocity (R68)."""
    try:
        return ApiResponse(data=_compute_metrics(_load_program(), weeks))
    except Exception as exc:
        logger.error("program_metrics: %s", exc)
        return internal_error(exc, code="METRICS_FAILED")
