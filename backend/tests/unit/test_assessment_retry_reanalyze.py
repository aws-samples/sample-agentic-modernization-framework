"""A rejected Assessment re-runs the code analysis when its inputs changed (2026-10-23).

APP-HE's build_dir was corrected at the gate, but Retry looped to the Assessment agent only:
the repo scan, detected wiring and CI readiness stayed on the wrong subproject, and the
re-run also lost app_facts (the authoritative facts the Assessment prompt embeds).
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.src.api.v1.workflow import _build_dir_changed, _truthy_input

ASL = json.loads((Path(__file__).resolve().parents[3] / "infra/step-functions/migration-workflow.v2.asl.json").read_text().replace("${ApprovalTimeoutSeconds}", "604800"))
S = ASL["States"]


def test_build_dir_change_triggers_reanalysis():
    assert _build_dir_changed({"build_dir": "apps/unicorn-store-spring", "detected_build_dir": "samples/demo"})
    assert _build_dir_changed({"build_dir": "springboot"})
    assert not _build_dir_changed({"build_dir": "/springboot/", "detected_build_dir": "springboot"})
    assert not _build_dir_changed({"detected_build_dir": "springboot"})
    assert _truthy_input(True) and _truthy_input("true") and not _truthy_input(None)


def test_rejected_assessment_routes_through_reanalysis_choice():
    catch = next(c for c in S["Approval_Assessment"]["Catch"] if c["ErrorEquals"] == ["StepRejected"])
    assert catch["Next"] == "Route_AssessmentRetry"
    assert {"app_facts", "reviewer_comment", "reanalyze"} <= set(catch["Output"])
    route = S["Route_AssessmentRetry"]
    assert route["Choices"][0]["Next"] == "PreHook_Assessment" and route["Default"] == "Assessment"


def test_reanalysis_path_keeps_the_reviewer_comment():
    # PreHook_Assessment merges its input; LaunchATX must pass the comment on to the agent.
    assert "$merge([$states.input" in S["PreHook_Assessment"]["Output"]
    assert "reviewer_comment" in S["LaunchATX"]["Output"] and S["LaunchATX"]["Next"] == "Assessment"


def test_failed_validation_returns_to_the_execution_gate_within_the_deploy_budget():
    """APP-CLI 2026-10-26: a fixable app bug (500 from API Gateway) ended the run at Failed_Validation."""
    ch = S["Check_Validation"]["Choices"]
    assert ch[0]["Next"] == "Approval_Validation"
    assert ch[1]["Next"] == "Approval_Execution" and "deploy_attempt < 3" in ch[1]["Condition"]
    assert S["Check_Validation"]["Default"] == "Failed_Validation"
    assert "deploy_attempt" in S["Validation"]["Output"]


def test_rerun_validation_invokes_validate_deployment_async_for_deployed_apps():
    from unittest.mock import MagicMock, patch

    from backend.src.api.v1 import workflow

    class T:
        def __init__(self, status):
            self.status = status

        def get_item(self, Key):  # noqa: N803
            return {"Item": {"PK": Key["PK"], "SK": "META", "exec_deploy_status": self.status}}

    lam = MagicMock()
    with patch.object(workflow, "get_table", return_value=T("pipeline_failed")), \
            patch("backend.src.api.v1.workflow.boto3.client", return_value=lam):
        refused = workflow.rerun_validation("APP-X", user=None)
    assert b"NOT_DEPLOYED" in refused.body and lam.invoke.call_count == 0
    with patch.object(workflow, "get_table", return_value=T("deployed")), \
            patch("backend.src.api.v1.workflow.boto3.client", return_value=lam):
        ok = workflow.rerun_validation("APP-X", user=None)
    kw = lam.invoke.call_args.kwargs
    assert kw["InvocationType"] == "Event" and json.loads(kw["Payload"]) == {
        "tool_name": "validate_deployment", "arguments": {"app_id": "APP-X", "_rerun": True}}
    assert ok.data["status"] == "started"


def test_report_aliases_reach_execution_sub_artifacts():
    from backend.src.api.v1.workflow import _REPORT_PHASE_ALIASES

    assert _REPORT_PHASE_ALIASES["deploy"] == "execution/deploy"
