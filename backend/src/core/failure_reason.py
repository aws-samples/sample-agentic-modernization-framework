"""Explain why a journey execution failed, from its Step Functions history.

Every failure path in migration-workflow.v2.asl.json ends in one generic Fail
state (``MarkFailed``: "A phase failed or an approval was rejected.") or a
``Failed_<step>`` state, so the execution's own error/cause says nothing useful.
The real reason is in the events just before it:

* a Task that errored (Harness 5xx, Lambda error, ECS callback failure, timeout),
* a pre-hook (``PreCheck_<phase>``) whose payload has ``blockers``,
* a post-check (``Check_<phase>``) whose payload has ``missing`` / ``blockers``,
* an approval gate rejected by a reviewer (``Approval_<phase>`` task failure).

Pure: takes the event list (any order), returns a dict for the UI.
"""

from __future__ import annotations

import json
import re
from typing import Any

_PHASE_RE = re.compile(r"^(?:PreHook|PreCheck|Validate|Check|Approval|Failed|Score|Launch)_?")

# Human-readable text for the deterministic blocker / missing codes.
_CODE_TEXT = {
    "app_not_found": "The application record does not exist.",
    "app_name": "The application has no name.",
    "no_repo_linked": "No source repository is linked (add it via L2 enrichment).",
    "migration_strategy": "No migration strategy has been confirmed.",
    "strategy_confidence": "The strategy recommendation has no confidence score.",
    "assessment_summary": "No assessment summary was produced in this run.",
    "assessment_summary_stale": "The only assessment summary is from an earlier run.",
    "assessment_contradicts_confirmed_strategy": "The assessment was written for a different strategy than the confirmed one.",
    "target_compute": "Target compute is missing or not valid for the strategy.",
    "target_database": "Target database is missing or not valid for the strategy.",
    "cicd_target": "CI/CD target is missing or not valid for the strategy.",
    "target_account": "Target AWS account is missing or not a 12-digit id.",
    "vpc_cidr": "VPC CIDR is missing.",
    "vpc_cidr_invalid": "VPC CIDR must be an RFC1918 /16–/28 network address (e.g. 10.0.0.0/24, not 10.0.0.2/24).",
    "target_account_invalid": "Target AWS account must be 12 digits.",
    "ami_id": "An AMI id is required for EC2 targets.",
    "repo_url_invalid": "Repository URL is not clonable.",
    "design_contradicts_approved_selections": "The design contradicts the approved selections (details follow).",
}


_FAILURE_EVENTS = (
    "TaskFailed", "TaskTimedOut", "TaskSubmitFailed", "TaskStartFailed",
    "LambdaFunctionFailed", "LambdaFunctionTimedOut",
)


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _details(event: dict) -> dict:
    for key, val in event.items():
        if key.endswith("EventDetails") and isinstance(val, dict):
            return val
    return {}


def _find_list(payload: Any, key: str) -> list:
    """First list under ``key`` anywhere in a (possibly Lambda-wrapped) payload."""
    if isinstance(payload, dict):
        if isinstance(payload.get(key), list):
            return payload[key]
        for v in payload.values():
            found = _find_list(v, key)
            if found:
                return found
    return []


def _phase_of(state: str) -> str:
    base = _PHASE_RE.sub("", state or "")
    return base.lower() if base else ""


def _describe(code: str) -> str:
    head, _, tail = str(code).partition(":")
    text = _CODE_TEXT.get(head)
    if head == "repo_url_invalid":
        return f"Repository URL is not clonable: {tail or code}"
    return f"{text} ({code})" if text else str(code)


def explain_failure(events: list[dict]) -> dict:
    """Return {failed_state, phase, kind, summary, error, cause, reasons[], warnings[], at}."""
    ordered = sorted(events, key=lambda e: e.get("id", 0))
    names: dict[int, str] = {}
    current = ""
    for ev in ordered:
        d = _details(ev)
        if ev.get("type", "").endswith("StateEntered"):
            current = d.get("name", "")
        names[ev.get("id", 0)] = current

    fail = next((e for e in reversed(ordered) if e.get("type") == "FailStateEntered"), None)
    end = next((e for e in reversed(ordered) if e.get("type") in (
        "ExecutionFailed", "ExecutionAborted", "ExecutionTimedOut")), None)
    if end and end.get("type") == "ExecutionAborted":
        return {"kind": "aborted", "failed_state": names.get(end.get("id", 0), ""),
                "phase": _phase_of(names.get(end.get("id", 0), "")),
                "summary": "The run was stopped (cancelled) before it finished.",
                "error": "", "cause": _details(end).get("cause", ""), "reasons": [], "warnings": [],
                "at": str(end.get("timestamp", ""))}
    if end and end.get("type") == "ExecutionTimedOut":
        return {"kind": "timeout", "failed_state": names.get(end.get("id", 0), ""),
                "phase": _phase_of(names.get(end.get("id", 0), "")),
                "summary": "The run exceeded its overall time limit.", "error": "", "cause": "",
                "reasons": [], "warnings": [], "at": str(end.get("timestamp", ""))}

    stop_id = (fail or end or {}).get("id", 10**9)
    before = [e for e in ordered if e.get("id", 0) < stop_id]
    at = str((fail or end or {}).get("timestamp", ""))

    # The state that routed to the Fail state: a Task (caught error) or a Choice (gate).
    last_exit = next((e for e in reversed(before)
                      if e.get("type") in ("TaskStateExited", "ChoiceStateExited")), None)
    exited_state = _details(last_exit).get("name", "") if last_exit else ""

    # 1. A task that errored and was caught (Catch → MarkFailed / Failed_*).
    if last_exit and last_exit.get("type") == "TaskStateExited":
        entered = next((e for e in reversed(before) if e.get("type") == "TaskStateEntered"
                        and _details(e).get("name") == exited_state), None)
        lo = entered.get("id", 0) if entered else 0
        failure = next((e for e in reversed(before) if lo < e.get("id", 0) < last_exit.get("id", 0)
                        and e.get("type") in _FAILURE_EVENTS), None)
        if failure:
            d = _details(failure)
            state = exited_state
            error, cause = d.get("error", failure.get("type", "")), d.get("cause", "")
            if state.startswith("Approval_"):
                parsed = _json(cause) or {}
                comment = parsed.get("reviewer_comment") or parsed.get("comment") or ""
                return {"kind": "rejected", "failed_state": state, "phase": _phase_of(state),
                        "summary": f"Rejected by the reviewer at {state.replace('Approval_', '')} review.",
                        "error": error, "cause": cause,
                        "reasons": [f"Reviewer comment: {comment}"] if comment else [],
                        "warnings": [], "at": at}
            transient = bool(re.search(r"status code 5\d\d|ServiceUnavailable|InternalServer|Throttl",
                                       f"{cause} {error}"))
            reasons = [cause] if cause else []
            if transient:
                reasons.append("This looks like a transient AWS service error. "
                               "Starting the workflow again usually succeeds.")
            return {"kind": "task_error", "failed_state": state, "phase": _phase_of(state),
                    "summary": f"{state or 'A step'} failed: {error}", "error": error, "cause": cause,
                    "reasons": reasons, "warnings": [], "at": at}

    # 2. A deterministic gate (pre-hook / post-check) routed to the Fail state.
    choice = next((e for e in reversed(before) if e.get("type") == "ChoiceStateEntered"
                   and _details(e).get("name") == exited_state), None)
    choice_state = _details(choice).get("name", "") if choice else ""
    # Only the task that fed this Choice — older payloads (e.g. a pre-hook's
    # warnings three phases back) are not the reason.
    choice_id = choice.get("id", 0) if choice else stop_id
    feeder = next((e for e in reversed(before) if e.get("type") == "TaskStateEntered"
                   and e.get("id", 0) < choice_id), None)
    lo = feeder.get("id", 0) if feeder else 0
    payload = None
    for ev in reversed(before):
        if not (lo < ev.get("id", 0) < choice_id):
            continue
        if ev.get("type") in ("TaskSucceeded", "TaskStateExited", "LambdaFunctionSucceeded"):
            candidate = _json(_details(ev).get("output"))
            if any(_find_list(candidate, k) for k in ("blockers", "missing", "warnings")):
                payload = candidate
                break
    blockers = _find_list(payload, "blockers")
    missing = _find_list(payload, "missing")
    warnings = _find_list(payload, "warnings")
    codes = list(dict.fromkeys([*missing, *blockers]))
    # Failed_<step> states carry a specific Cause (MarkFailed's is generic).
    end_cause = _details(end).get("cause", "") if end else ""
    specific = end_cause if fail and _details(fail).get("name", "").startswith("Failed_") else ""
    if choice_state.startswith("PreCheck_"):
        kind, summary = "precondition", f"{choice_state.replace('PreCheck_', '')} could not start: its inputs are not ready."
    elif choice_state.startswith("Check_"):
        kind, summary = "post_check", f"{choice_state.replace('Check_', '')} output failed validation."
    else:
        kind, summary = "unknown", "The run failed."
    reasons = [_describe(c) for c in codes]
    if specific:
        reasons.insert(0, specific)
    return {"kind": kind, "failed_state": choice_state or (fail and _details(fail).get("name", "")) or "",
            "phase": _phase_of(choice_state), "summary": summary, "error": "", "cause": specific,
            "reasons": reasons, "warnings": [str(w) for w in warnings], "at": at}
