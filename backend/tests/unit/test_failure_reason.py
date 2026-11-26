"""explain_failure reconstructs the real reason behind the generic MarkFailed state."""
from __future__ import annotations

import json

from backend.src.core.failure_reason import explain_failure


def _ev(i, typ, **details):
    key = {
        "TaskStateEntered": "stateEnteredEventDetails",
        "ChoiceStateEntered": "stateEnteredEventDetails",
        "FailStateEntered": "stateEnteredEventDetails",
        "TaskStateExited": "stateExitedEventDetails",
        "ChoiceStateExited": "stateExitedEventDetails",
        "TaskSucceeded": "taskSucceededEventDetails",
        "TaskFailed": "taskFailedEventDetails",
        "ExecutionFailed": "executionFailedEventDetails",
        "ExecutionAborted": "executionAbortedEventDetails",
    }[typ]
    return {"id": i, "type": typ, "timestamp": f"t{i}", key: details}


def _tail(i):
    return [_ev(i, "FailStateEntered", name="MarkFailed"),
            _ev(i + 1, "ExecutionFailed", error="WorkflowFailed", cause="A phase failed or an approval was rejected.")]


def test_harness_502_is_a_transient_task_error():
    cause = "Service returned HTTP status code 502 (Service: BedrockAgentCore, Status Code: 502)"
    events = [
        _ev(1, "TaskStateEntered", name="Assessment"),
        _ev(2, "TaskFailed", error="BedrockAgentCore.BedrockAgentCoreException", cause=cause),
        _ev(3, "TaskStateExited", name="Assessment", output="{}"),
        *_tail(4),
    ]
    out = explain_failure(list(reversed(events)))  # API reads history newest-first
    assert out["kind"] == "task_error" and out["failed_state"] == "Assessment" and out["phase"] == "assessment"
    assert out["cause"] == cause
    assert any("transient" in r for r in out["reasons"])


def test_prehook_blockers_are_explained():
    payload = {"Payload": {"app_id": "A", "ready": False, "blockers": ["no_repo_linked", "repo_url_invalid:foo"]}}
    events = [
        _ev(1, "TaskStateEntered", name="PreHook_Assessment"),
        _ev(2, "TaskSucceeded", output=json.dumps(payload)),
        _ev(3, "TaskStateExited", name="PreHook_Assessment", output=json.dumps({"app_id": "A", "prehook": payload["Payload"]})),
        _ev(4, "ChoiceStateEntered", name="PreCheck_Assessment"),
        _ev(5, "ChoiceStateExited", name="PreCheck_Assessment"),
        *_tail(6),
    ]
    out = explain_failure(events)
    assert out["kind"] == "precondition" and out["phase"] == "assessment"
    assert out["reasons"][0].startswith("No source repository is linked")
    assert out["reasons"][1] == "Repository URL is not clonable: foo"


def test_post_check_missing_items():
    payload = {"Payload": {"valid": False, "missing": ["assessment_contradicts_confirmed_strategy"],
                           "blockers": ["compute for rehost is ecs"], "warnings": ["no cost"]}}
    events = [
        _ev(1, "TaskStateEntered", name="Validate_Assessment"),
        _ev(2, "TaskSucceeded", output=json.dumps(payload)),
        _ev(3, "TaskStateExited", name="Validate_Assessment", output="{}"),
        _ev(4, "ChoiceStateEntered", name="Check_Assessment"),
        _ev(5, "ChoiceStateExited", name="Check_Assessment"),
        *_tail(6),
    ]
    out = explain_failure(events)
    assert out["kind"] == "post_check"
    assert "different strategy" in out["reasons"][0] and out["reasons"][1] == "compute for rehost is ecs"
    assert out["warnings"] == ["no cost"]


def test_rejected_gate_carries_reviewer_comment():
    events = [
        _ev(1, "TaskStateEntered", name="Approval_Execution"),
        _ev(2, "TaskFailed", error="StepRejected", cause=json.dumps({"reviewer_comment": "wrong VPC"})),
        _ev(3, "TaskStateExited", name="Approval_Execution"),
        *_tail(4),
    ]
    out = explain_failure(events)
    assert out["kind"] == "rejected" and out["reasons"] == ["Reviewer comment: wrong VPC"]


def test_aborted_run():
    events = [_ev(1, "TaskStateEntered", name="Design"), _ev(2, "ExecutionAborted", cause="cancelled by user")]
    assert explain_failure(events)["kind"] == "aborted"
