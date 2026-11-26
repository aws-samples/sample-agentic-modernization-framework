"""Deploy is off by default (Terraform enable_deploy=false): AMF stops at a reviewed migration branch.

The Deploy task resumes at once with status "skipped", Check_Deploy routes that to Validation,
Validation runs its static checks only, teardown is refused, and approving execution_review needs
no deploy commit (nor the Admin role).
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.tests.unit.test_journey_exec_infra import te  # noqa: F401 — fixture

REPO = Path(__file__).resolve().parents[3]
TF = REPO / "infra" / "environments" / "agentcore"


def test_deploy_defaults_off_everywhere():
    from backend.src.config import Settings

    assert Settings().deploy_enabled is False
    deploy_tf = (TF / "deploy.tf").read_text()
    assert 'variable "enable_deploy"' in deploy_tf and "default     = false" in deploy_tf
    # The deploy role and everything that can use it exist only when enabled.
    for res in ('resource "aws_iam_role" "deploy"', 'resource "aws_iam_role_policy_attachment" "deploy_power_user"',
                'resource "aws_iam_role_policy" "deploy_iam"', 'resource "aws_iam_role_policy" "worker_assume_deploy"'):
        block = deploy_tf[deploy_tf.index(res):]
        assert "count" in block[:block.index("\n}\n")] and "local.deploy" in block[:block.index("\n}\n")], res


def test_deploy_task_is_skipped_and_resumes_the_workflow(te, monkeypatch):  # noqa: F811
    mod, env = te
    monkeypatch.setattr(mod, "DEPLOY_ENABLED", False)
    sent = []
    monkeypatch.setattr(mod, "_succeed_task", lambda token, payload: sent.append((token, payload)))
    env.seed("APP-HE")
    out = mod.deploy_infrastructure({"app_id": "APP-HE", "_task_token": "tok"})
    assert out["status"] == "skipped" and "enable_deploy" in out["message"]
    assert sent == [("tok", out)]
    assert env.table.items[("APP#APP-HE", "META")]["exec_deploy_status"] == "skipped"


def test_teardown_and_live_checks_are_off(te, monkeypatch):  # noqa: F811
    mod, env = te
    monkeypatch.setattr(mod, "DEPLOY_ENABLED", False)
    env.seed("APP-HE")
    assert mod.destroy_infrastructure({"app_id": "APP-HE", "confirm": "APP-HE"})["status"] == "error"
    checks: list = []
    mod._live_checks("APP-HE", {}, checks)
    assert [(c["check"], c["result"]) for c in checks] == [("live_deployment", "skip")]


def test_check_deploy_routes_skipped_to_validation():
    asl = json.loads((REPO / "infra/step-functions/migration-workflow.v2.asl.json").read_text()
                     .replace("${ApprovalTimeoutSeconds}", "1"))
    cond = asl["States"]["Check_Deploy"]["Choices"][0]
    assert "'skipped'" in cond["Condition"] and cond["Next"] == "Validation"


def test_api_teardown_refused_when_disabled(monkeypatch):
    from backend.src.api.v1.workflow import TeardownRequest, teardown_app
    from backend.src.config import settings

    monkeypatch.setattr(settings, "deploy_enabled", False)
    resp = teardown_app(TeardownRequest(app_id="APP-X", confirm="APP-X"), user=None)
    assert b"DEPLOY_DISABLED" in resp.body


def test_capabilities_expose_deploy_enabled():
    from backend.src.api.v1.system import get_capabilities

    assert get_capabilities()["deploy_enabled"] is False
