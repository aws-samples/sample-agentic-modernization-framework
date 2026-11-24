"""
Governance API — auditable trail assembled from authoritative sources.

Nothing here is a hand-maintained log. The trail is reconstructed on read from
two systems of record:

  * Step Functions execution history — the authoritative record of RUNS and
    APPROVAL GATES. Approval states are named ``Approval_*`` and pause on
    ``waitForTaskToken``; the following ``TaskSucceeded`` / ``TaskFailed`` event
    tells us whether a gate was approved or rejected. A gate that was entered
    but never resolved is reported as pending — never as approved.
  * DynamoDB single table — the artifacts and events each stage produced:
    ``ANALYSIS#`` (assessment reports), ``DESIGN#`` (design docs)
    and ``TASK_TOKEN#`` (pending approvals).

Every entry is banded into an AIDLC lifecycle phase — Inception, Construction
or Operation — derived from the state / record that produced it.

Endpoints (mounted under /api/v1/governance by main.py):
  GET /summary        — estate-wide run + gate rollup
  GET /{app_id}       — one app's executions, gates, artifacts and audit rows
  GET /{app_id}/gates — just the gate list with outcomes

Degrade gracefully: if Step Functions is unreachable or the state machine is
missing, return whatever DynamoDB can supply plus a ``note`` in the payload
rather than 500ing the page.
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter

from backend.src.config import settings
from backend.src.core import aws
from backend.src.core.models import ApiResponse, error_response, internal_error
from backend.src.db.accessors import list_all_applications, list_app_children

logger = logging.getLogger(__name__)
router = APIRouter()


# ── AIDLC lifecycle banding ────────────────────────────────────────────
# Ordered keyword → band. First keyword found in a state / record name wins.
_BAND_KEYWORDS: list[tuple[str, str]] = [
    # Inception
    ("discovery", "Inception"),
    ("assessment", "Inception"),
    ("portfolio", "Inception"),
    # Construction
    ("design", "Construction"),
    ("migration_plan", "Construction"),
    ("migrationplan", "Construction"),
    ("plan", "Construction"),
    ("wave", "Construction"),
    ("transform", "Construction"),
    ("execution", "Construction"),
    ("build", "Construction"),
    ("validation", "Construction"),
    ("cutover", "Construction"),
    ("deploy", "Construction"),
    # Operation
    ("post_deploy", "Operation"),
    ("handover", "Operation"),
    ("decommission", "Operation"),
    ("operate", "Operation"),
    ("operation", "Operation"),
    ("ops", "Operation"),
    ("signal", "Operation"),
    ("monitor", "Operation"),
    ("health", "Operation"),
]


def _band_for(text: str) -> str:
    """Map a state name / record kind onto an AIDLC lifecycle band."""
    s = (text or "").lower()
    for keyword, band in _BAND_KEYWORDS:
        if keyword in s:
            return band
    # Unknown → Construction (the bulk of factory work lives there).
    return "Construction"


def _iso(value) -> str | None:
    """Best-effort ISO-8601 rendering for datetimes returned by boto3."""
    if value is None:
        return None
    try:
        return value.isoformat()
    except Exception:
        return str(value)


# ── Step Functions (systems of record for runs + gates) ────────────────


def _sfn_client():
    """The Step Functions client the router uses.

    Isolated in one place so tests can monkeypatch it with a stub (moto's
    stepfunctions support is too thin to drive real execution histories).
    """
    return aws.client("stepfunctions")


def _state_machine_arn() -> str:
    """Resolve the deployed state-machine ARN from account + settings."""
    return settings.get_sfn_state_machine_arn()


def _gates_from_history(events: list[dict]) -> list[dict]:
    """Extract approval gates from an execution history.

    A gate is an ``Approval_*`` state that was entered; its outcome is resolved
    by the next terminal task/execution event:
      * TaskSucceeded                              → approved
      * TaskFailed / ExecutionFailed / TimedOut    → rejected
      * ExecutionAborted                           → cancelled
      * (never resolved)                           → pending

    ``cancelled`` is deliberately distinct from ``rejected``. An aborted
    execution means an operator stopped the run — nobody reviewed the change and
    declined it. Reporting that as a rejection would credit a human decision
    that never happened, and inflate the rejected count with housekeeping.
    A timeout still reads as rejected: the gate was offered and expired
    unapproved, which is a real (if passive) refusal to let the change proceed.

    We never invent a gate that the history does not evidence.
    """
    gates: list[dict] = []
    pending_idx: int | None = None

    for e in events:
        etype = e.get("type", "")

        name = (e.get("stateEnteredEventDetails") or {}).get("name", "")
        if name.startswith("Approval_"):
            gates.append(
                {
                    "gate": name,
                    "band": _band_for(name),
                    "outcome": "pending",
                    "at": _iso(e.get("timestamp")),
                }
            )
            pending_idx = len(gates) - 1
            continue

        if pending_idx is None:
            continue
        if etype == "TaskSucceeded":
            gates[pending_idx]["outcome"] = "approved"
            pending_idx = None
        elif etype == "ExecutionAborted":
            gates[pending_idx]["outcome"] = "cancelled"
            pending_idx = None
        elif etype in ("TaskFailed", "ExecutionFailed", "ExecutionTimedOut"):
            gates[pending_idx]["outcome"] = "rejected"
            pending_idx = None

    return gates


_TERMINAL_EXECUTION_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})
_cache_lock = threading.Lock()
# executionArn → gates for executions that have finished: their
# history can no longer change, so it is fetched once per process.
_terminal_gates: dict[str, list[dict]] = {}
_TERMINAL_GATES_MAX = 5000
# app filter (or "") → (monotonic time, executions) — the page polls these views.
_collected: dict[str, tuple[float, list[dict]]] = {}


def clear_cache() -> None:
    """Drop the governance caches (tests; a forced refresh)."""
    with _cache_lock:
        _terminal_gates.clear()
        _collected.clear()


def _execution_belongs_to(name: str, app_id: str) -> bool:
    """Execution names are "<app_id>-<YYYYmmdd-HHMMSS>"; a bare prefix would also
    match APP-10 for APP-1."""
    return name.startswith(f"{app_id}-")


def _list_executions(sfn, arn: str) -> list[dict]:
    """Newest-first executions, paginated up to `governance_max_executions`."""
    cap = max(1, settings.governance_max_executions)
    out: list[dict] = []
    kwargs: dict = {"stateMachineArn": arn, "maxResults": min(100, cap)}
    while True:
        resp = sfn.list_executions(**kwargs)
        out.extend(resp.get("executions", []))
        token = resp.get("nextToken")
        if not token or len(out) >= cap:
            break
        kwargs["nextToken"] = token
    return out[:cap]


def _execution_gates(sfn, ex: dict) -> list[dict]:
    arn = ex.get("executionArn", "")
    terminal = ex.get("status") in _TERMINAL_EXECUTION_STATUSES
    if terminal:
        with _cache_lock:
            hit = _terminal_gates.get(arn)
        if hit is not None:
            return [dict(g) for g in hit]
    hist = sfn.get_execution_history(executionArn=arn, maxResults=settings.governance_history_page_size)
    gates = _gates_from_history(hist.get("events", []))
    if terminal and arn:
        with _cache_lock:
            if len(_terminal_gates) >= _TERMINAL_GATES_MAX:
                _terminal_gates.clear()
            _terminal_gates[arn] = [dict(g) for g in gates]
    return gates


def _collect_executions(app_id_filter: str | None = None) -> tuple[list[dict], str | None]:
    """Return (executions, note), cached for `governance_cache_seconds`."""
    key = app_id_filter or ""
    ttl = settings.governance_cache_seconds
    with _cache_lock:
        hit = _collected.get(key)
    if hit is not None and ttl > 0 and time.monotonic() - hit[0] < ttl:
        return [{**e, "gates": [dict(g) for g in e["gates"]]} for e in hit[1]], None
    executions, note = _collect_executions_uncached(app_id_filter)
    if note is None:
        with _cache_lock:
            _collected[key] = (time.monotonic(), executions)
        executions = [{**e, "gates": [dict(g) for g in e["gates"]]} for e in executions]
    return executions, note


def _collect_executions_uncached(app_id_filter: str | None = None) -> tuple[list[dict], str | None]:
    """Return (executions, note).

    ``executions`` is a list of dicts (name, status, timestamps,
    gates). ``note`` is set (and executions empty) when Step Functions
    is unreachable / the state machine is missing, so callers degrade to
    DynamoDB-only data instead of 500ing.

    Tolerates an execution with no retrievable history (gates stays empty).
    """
    try:
        sfn = _sfn_client()
        arn = _state_machine_arn()
        listed = _list_executions(sfn, arn)
    except Exception as exc:  # noqa: BLE001 — degrade, never 500 the page
        logger.warning("Step Functions unavailable (list_executions): %s", exc)
        return [], "Step Functions unavailable; showing DynamoDB-derived data only."

    executions: list[dict] = []
    for ex in listed:
        name = ex.get("name", "")
        if app_id_filter and not _execution_belongs_to(name, app_id_filter):
            continue
        entry = {
            "execution": name,
            "status": ex.get("status"),
            "started_at": _iso(ex.get("startDate")),
            "stopped_at": _iso(ex.get("stopDate")),
            "gates": [],
        }
        try:
            entry["gates"] = _execution_gates(sfn, ex)
        except Exception as exc:  # noqa: BLE001 — an app/execution may have no history
            logger.warning("get_execution_history failed for %s: %s", name, exc)
        executions.append(entry)

    return executions, None


# ── DynamoDB artifact / event assembly ─────────────────────────────────


def _partition_rows(rows: list[dict]) -> dict[str, list[dict]]:
    """Split an app's rows into analyses, designs and tokens."""
    analyses, designs, tokens = [], [], []
    for r in rows:
        sk = r.get("SK", "")
        if sk.startswith("ANALYSIS#"):
            analyses.append(r)
        elif sk.startswith("DESIGN#"):
            designs.append(r)
        elif sk.startswith("TASK_TOKEN#"):
            tokens.append(r)
    return {"analyses": analyses, "designs": designs, "tokens": tokens}


# ── Endpoints ──────────────────────────────────────────────────────────
# NOTE: /summary is declared BEFORE /{app_id} so the literal path is not
# captured as an app_id. This class of bug has bitten this codebase before.


@router.get("/summary")
def get_governance_summary():
    """Estate-wide governance rollup: runs, gates and apps under governance.

    Honest by construction — FAILED runs and rejected/pending gates are
    counted, never filtered out. Tolerant of executions with no history and of
    Step Functions being unreachable (returns DynamoDB-derived counts + note).
    """
    try:
        executions, note = _collect_executions()

        runs_by_status: dict[str, int] = {}
        gate_totals = {"total": 0, "approved": 0, "rejected": 0, "cancelled": 0, "pending": 0}
        for ex in executions:
            status = ex.get("status") or "UNKNOWN"
            runs_by_status[status] = runs_by_status.get(status, 0) + 1
            for g in ex.get("gates", []):
                gate_totals["total"] += 1
                outcome = g.get("outcome", "pending")
                if outcome in gate_totals:
                    gate_totals[outcome] += 1
                else:
                    gate_totals["pending"] += 1

        # Apps under governance = known apps whose id prefixes a real execution
        # name, unioned with apps that already carry governance artifacts/events.
        try:
            apps = list_all_applications()
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_all_applications failed: %s", exc)
            apps = []
        app_ids = [a.get("app_id") for a in apps if a.get("app_id")]

        governed: set[str] = set()
        for ex in executions:
            for aid in app_ids:
                if _execution_belongs_to(ex["execution"], aid):
                    governed.add(aid)
                    break

        data = {
            "total_runs": len(executions),
            "runs_by_status": runs_by_status,
            "gates": gate_totals,
            "apps_under_governance": len(governed),
            "governed_app_ids": sorted(governed),
            "bands": ["Inception", "Construction", "Operation"],
        }
        if note:
            data["note"] = note
        return ApiResponse(data=data)
    except Exception as exc:  # noqa: BLE001
        logger.error("get_governance_summary failed: %s", exc)
        return internal_error(exc, code="GOVERNANCE_READ_FAILED")


@router.get("/{app_id}")
def get_governance(app_id: str):
    """One application's audit trail: executions, gates and artifacts.

    Every entry is banded into Inception / Construction / Operation. Unknown
    app (no DynamoDB rows and no matching executions) → HTTP 404.
    """
    try:
        try:
            rows = list_app_children(app_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_app_children failed for %s: %s", app_id, exc)
            rows = []

        executions, note = _collect_executions(app_id_filter=app_id)

        # Unknown app: nothing in DynamoDB and nothing in Step Functions.
        if not rows and not executions:
            return error_response(f"No governance record for {app_id}", code="NOT_FOUND")

        meta = next((r for r in rows if r.get("SK") == "META"), {})
        parts = _partition_rows(rows)

        # Flattened gate list across all this app's executions.
        gates: list[dict] = []
        for ex in executions:
            for g in ex.get("gates", []):
                gates.append({**g, "execution": ex["execution"]})

        # Artifacts (traceable outputs).
        analyses = [
            {
                "artifact": r.get("SK", ""),
                "type": "assessment_report",
                "analysis_type": r.get("analysis_type", ""),
                "at": r.get("timestamp") or r.get("SK", "").split("#")[-1],
                "band": "Inception",
            }
            for r in parts["analyses"]
        ]
        designs = [
            {
                "artifact": r.get("SK", ""),
                "type": "design_document",
                "version": r.get("SK", "").replace("DESIGN#", ""),
                "at": r.get("generated_at") or r.get("timestamp") or "",
                "band": "Construction",
            }
            for r in parts["designs"]
        ]
        # Band EVERY entry into the three lifecycle phases.
        phases: dict[str, list[dict]] = {"Inception": [], "Construction": [], "Operation": []}
        for a in analyses:
            phases["Inception"].append({"kind": "analysis", "at": a["at"], "detail": a["artifact"]})
        for d in designs:
            phases["Construction"].append({"kind": "design", "at": d["at"], "detail": d["artifact"]})
        for g in gates:
            phases.setdefault(g["band"], []).append(
                {"kind": "gate", "at": g["at"], "detail": g["gate"], "outcome": g["outcome"]}
            )

        gate_summary = {
            "total": len(gates),
            "approved": sum(1 for g in gates if g["outcome"] == "approved"),
            "rejected": sum(1 for g in gates if g["outcome"] == "rejected"),
            "cancelled": sum(1 for g in gates if g["outcome"] == "cancelled"),
            "pending": sum(1 for g in gates if g["outcome"] == "pending"),
        }

        data = {
            "app_id": app_id,
            "lifecycle_status": meta.get("status", ""),
            "bands": ["Inception", "Construction", "Operation"],
            "executions": executions,
            "gates": gates,
            "gate_summary": gate_summary,
            "artifacts": {"analyses": analyses, "designs": designs},
            "phases": phases,
        }
        if note:
            data["note"] = note
        return ApiResponse(data=data)
    except Exception as exc:  # noqa: BLE001
        logger.error("get_governance failed for %s: %s", app_id, exc)
        return internal_error(exc, code="GOVERNANCE_READ_FAILED")


@router.get("/{app_id}/gates")
def get_governance_gates(app_id: str):
    """Just the gate list (with outcomes) for one application."""
    try:
        executions, note = _collect_executions(app_id_filter=app_id)

        # Distinguish "unknown app" from "known app, no gates yet".
        if not executions:
            try:
                exists = bool(list_app_children(app_id))
            except Exception:  # noqa: BLE001
                exists = False
            if not exists and note is None:
                return error_response(f"No governance record for {app_id}", code="NOT_FOUND")

        gates: list[dict] = []
        for ex in executions:
            for g in ex.get("gates", []):
                gates.append({**g, "execution": ex["execution"]})

        data = {
            "app_id": app_id,
            "gates": gates,
            "summary": {
                "total": len(gates),
                "approved": sum(1 for g in gates if g["outcome"] == "approved"),
                "rejected": sum(1 for g in gates if g["outcome"] == "rejected"),
                "pending": sum(1 for g in gates if g["outcome"] == "pending"),
            },
        }
        if note:
            data["note"] = note
        return ApiResponse(data=data)
    except Exception as exc:  # noqa: BLE001
        logger.error("get_governance_gates failed for %s: %s", app_id, exc)
        return internal_error(exc, code="GOVERNANCE_READ_FAILED")
