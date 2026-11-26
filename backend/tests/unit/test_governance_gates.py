"""
A cancelled run must not be reported as a human rejection.

Stopping an execution (operator housekeeping) and a reviewer declining a change
are different facts. Conflating them credits a decision nobody made and inflates
the rejected count with cleanup — which is exactly what happened when nine
abandoned June executions were stopped and the governance summary jumped from
0 rejected to 8.
"""

from __future__ import annotations

from backend.src.api.v1.governance import _gates_from_history


def _entered(name: str, ts: str = "2026-08-21T02:00:00+00:00") -> dict:
    return {"type": "TaskStateEntered", "timestamp": ts, "stateEnteredEventDetails": {"name": name}}


class TestGateOutcomes:
    def test_task_succeeded_is_approved(self):
        gates = _gates_from_history([_entered("Approval_Discovery"), {"type": "TaskSucceeded"}])
        assert [g["outcome"] for g in gates] == ["approved"]

    def test_execution_aborted_is_cancelled_not_rejected(self):
        gates = _gates_from_history([_entered("Approval_Design"), {"type": "ExecutionAborted"}])
        assert gates[0]["outcome"] == "cancelled", (
            "an operator stopping a run must not be recorded as a human rejection"
        )

    def test_task_failed_is_still_rejected(self):
        gates = _gates_from_history([_entered("Approval_Design"), {"type": "TaskFailed"}])
        assert gates[0]["outcome"] == "rejected"

    def test_timeout_is_still_rejected(self):
        """A gate that expired unapproved is a real, if passive, refusal."""
        gates = _gates_from_history([_entered("Approval_Validation"), {"type": "ExecutionTimedOut"}])
        assert gates[0]["outcome"] == "rejected"

    def test_unresolved_gate_stays_pending(self):
        gates = _gates_from_history([_entered("Approval_Cutover")])
        assert gates[0]["outcome"] == "pending"

    def test_no_gate_is_invented_without_evidence(self):
        gates = _gates_from_history([{"type": "TaskSucceeded"}, {"type": "ExecutionAborted"}])
        assert gates == []

    def test_multiple_gates_resolve_independently(self):
        gates = _gates_from_history([
            _entered("Approval_Discovery"), {"type": "TaskSucceeded"},
            _entered("Approval_Design"), {"type": "ExecutionAborted"},
        ])
        assert [g["outcome"] for g in gates] == ["approved", "cancelled"]
