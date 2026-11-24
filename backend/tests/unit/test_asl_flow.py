"""
Unit tests for the canonical AgentCore workflow ASL (migration-workflow.v2.asl.json).

Validates the state machine definition WITHOUT deploying to AWS:
1. Structure — valid JSON, StartAt exists, all Next/Choice/Catch targets valid, all states reachable
2. Agent phases — use the bedrockagentcore:invokeHarness resource
3. Approval gates — waitForTaskToken, with a split Catch (StepRejected vs States.ALL) + Retry
4. Terminal states — a Succeed and a Fail state exist
"""

import json
import re
from pathlib import Path

import pytest

ASL_PATH = (
    Path(__file__).parent.parent.parent.parent / "infra" / "step-functions" / "migration-workflow.v2.asl.json"
)

APPROVAL_PREFIX = "Approval_"


@pytest.fixture(scope="module")
def asl():
    """Load the ASL, tolerating ${...} templatefile placeholders (Terraform-owned)."""
    content = ASL_PATH.read_text().replace("${ApprovalTimeoutSeconds}", "604800")
    # Terraform injects ARNs via templatefile ${HarnessArn} etc.; stub them for parsing.
    content = re.sub(r"\$\{(\w+)\}", r"placeholder-\1", content)
    return json.loads(content)


@pytest.fixture(scope="module")
def states(asl):
    return asl["States"]


class TestStructure:
    def test_file_exists(self):
        assert ASL_PATH.exists(), f"ASL file not found at {ASL_PATH}"

    def test_valid_json(self, asl):
        assert "States" in asl
        assert asl.get("QueryLanguage") == "JSONata"

    def test_start_state_exists(self, asl, states):
        assert asl["StartAt"] in states

    def test_all_next_targets_valid(self, states):
        for name, state in states.items():
            if "Next" in state:
                assert state["Next"] in states, f"{name}.Next -> missing '{state['Next']}'"

    def test_all_choice_targets_valid(self, states):
        for name, state in states.items():
            if state.get("Type") == "Choice":
                for choice in state.get("Choices", []):
                    assert choice["Next"] in states, f"{name} choice -> missing '{choice['Next']}'"
                if "Default" in state:
                    assert state["Default"] in states

    def test_all_catch_targets_valid(self, states):
        for name, state in states.items():
            for catch in state.get("Catch", []):
                assert catch["Next"] in states, f"{name} Catch -> missing '{catch['Next']}'"

    def test_all_states_reachable(self, asl, states):
        reachable = set()

        def walk(name):
            if name in reachable or name not in states:
                return
            reachable.add(name)
            state = states[name]
            if "Next" in state:
                walk(state["Next"])
            if state.get("Type") == "Choice":
                for c in state.get("Choices", []):
                    walk(c["Next"])
                if "Default" in state:
                    walk(state["Default"])
            for catch in state.get("Catch", []):
                walk(catch["Next"])

        walk(asl["StartAt"])
        unreachable = set(states) - reachable
        assert not unreachable, f"Unreachable states: {unreachable}"

    def test_terminal_states(self, states):
        types = {s.get("Type") for s in states.values()}
        assert "Succeed" in types, "No Succeed terminal state"
        assert "Fail" in types, "No Fail terminal state"


class TestAgentPhases:
    def test_agent_phases_use_invoke_harness(self, states):
        harness_states = [
            n for n, s in states.items() if "bedrockagentcore:invokeHarness" in s.get("Resource", "")
        ]
        assert harness_states, "No invokeHarness agent states found"
        assert "Discovery" in harness_states


class TestApprovalGates:
    def test_gates_exist(self, states):
        gates = [n for n in states if n.startswith(APPROVAL_PREFIX)]
        assert gates, "No approval gates found"

    def test_gates_use_wait_for_token(self, states):
        for name, state in states.items():
            if name.startswith(APPROVAL_PREFIX):
                assert "waitForTaskToken" in state.get("Resource", ""), f"{name} not waitForTaskToken"

    def test_gates_split_catch_reject_vs_error(self, states):
        """Each gate distinguishes user rejection (StepRejected) from system error (States.ALL->MarkFailed)."""
        for name, state in states.items():
            if not name.startswith(APPROVAL_PREFIX):
                continue
            catches = state.get("Catch", [])
            errs = {tuple(c.get("ErrorEquals", [])): c.get("Next") for c in catches}
            flat = [e for k in errs for e in k]
            assert "StepRejected" in flat, f"{name} missing StepRejected Catch"
            assert errs.get(("States.ALL",)) == "MarkFailed", f"{name} States.ALL must route to MarkFailed"

    def test_gates_have_retry(self, states):
        for name, state in states.items():
            if name.startswith(APPROVAL_PREFIX):
                assert state.get("Retry"), f"{name} missing transient-error Retry"
