"""Code transformation: TD/context precedence, language guard, migration branch,
and honest per-step / per-plan outcomes in the workbench worker.

Audit (APP-HE, Java): AWS/python-version-upgrade got "upgrade to Java 21" because
the app language beat the TD name; a no-op SDK step and PARTIAL validations were
reported as "completed — Pushed to main"; a failed synthesis (Bedrock
AccessDenied) still produced plan status "completed".
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend.src.worker import branches
from backend.src.worker.workbench import (
    parse_validation_status,
    plan_final_status,
    step_outcome,
)
from backend.tests.unit.test_journey_exec_infra import te  # noqa: F401 — fixture

# Local audit dump (optional); resolved under the platform temp dir rather than a
# hard-coded /tmp path (bandit B108).
_HE_ARTIFACTS = (Path(tempfile.gettempdir()) / "review" / "APP-HE" / "s3" / "execution" / "transformation"
                 / "transform-APP-HE-bd15f3a4" / "artifacts")


@pytest.fixture
def transform(te, monkeypatch):  # noqa: F811
    """start_code_transformation with the ECS launch stubbed; returns (run, env)."""
    mod, env = te
    captured = {}

    def fake_launch(job_id, app_id, mode, td_name, target_version="", task_token="", extra_env=None):
        import json as _json
        captured["steps"] = _json.loads((extra_env or {}).get("WORKBENCH_STEPS", "[]"))
        return {"task_arn": "arn:aws:ecs:::task/stub", "status": "RUNNING"}

    failures = []
    monkeypatch.setattr(mod, "_launch_ecs_task", fake_launch)
    monkeypatch.setattr(mod, "_fail_task", lambda token, err, cause: failures.append((err, cause)))

    def run(app_id, **meta_overrides):
        captured.clear()
        env.seed(app_id, **meta_overrides)
        out = mod.start_code_transformation({"app_id": app_id, "_task_token": "tok"})
        return out, captured.get("steps", []), failures

    return run, env


def test_python_td_on_java_app_is_skipped_with_a_note(transform):
    run, _ = transform
    out, steps, _ = run("APP-HE")
    assert [s["td_name"] for s in steps] == ["AWS/java-version-upgrade", "AWS/java-aws-sdk-v1-to-v2"]
    assert out["skipped_steps"][0]["td_name"] == "AWS/python-version-upgrade"
    assert "app language is java" in out["skipped_steps"][0]["reason"]


def test_td_name_drives_the_derived_context(transform):
    run, _ = transform
    _, steps, _ = run("APP-HE")
    ctx = {s["td_name"]: s["additional_context"] for s in steps}
    assert ctx["AWS/java-version-upgrade"] == "The target Java version to upgrade to is Java 21."
    assert "AWS SDK for Java v1" in ctx["AWS/java-aws-sdk-v1-to-v2"]
    assert "upgrade to Java" not in ctx["AWS/java-aws-sdk-v1-to-v2"]


def test_python_td_on_python_app_gets_a_python_context(transform):
    run, _ = transform
    _, steps, _ = run("APP-HE", language="Python", transformation_steps=["AWS/python-version-upgrade"])
    assert steps[0]["additional_context"] == "The target Python version to upgrade to is Python 3.12."


def test_language_neutral_td_falls_back_to_app_language(transform):
    run, _ = transform
    _, steps, _ = run("APP-HE", transformation_steps=["AWS/early-access-comprehensive-upgrade"])
    assert steps[0]["additional_context"].startswith("The target Java version")


def test_all_mismatched_tds_fail_fast(transform):
    run, _ = transform
    out, steps, failures = run("APP-HE", transformation_steps=["AWS/python-version-upgrade"])
    assert out["status"] == "no_transforms" and steps == []
    assert failures and failures[0][0] == "NoTransforms"


def test_steps_target_the_migration_branch_from_the_default_branch(transform):
    run, _ = transform
    _, steps, _ = run("APP-HE")
    assert {s["repo_branch"] for s in steps} == {"migration/app-he"}
    assert {s["repo_source_ref"] for s in steps} == {"origin/main"}


def test_plan_record_carries_branch_and_skips(transform):
    run, env = transform
    run("APP-HE")
    plan = next(it for (pk, sk), it in env.table.items.items() if sk.startswith("PLAN#"))
    assert plan["branch"] == "migration/app-he"
    assert plan["skipped_steps"][0]["td_name"] == "AWS/python-version-upgrade"


# ── one branch convention for Lambda + worker ─────────────────────────────────

@pytest.mark.parametrize("app_id", ["APP-HE", "APP-TE", "app_cli", "Weird App/ID!"])
def test_lambda_and_worker_agree_on_the_migration_branch(te, app_id):  # noqa: F811
    mod, _ = te
    assert mod.migration_branch(app_id) == branches.migration_branch(app_id)
    assert mod.MIGRATION_BRANCH_PREFIX == branches.MIGRATION_BRANCH_PREFIX
    assert mod._REVIEW_BRANCH_RE.fullmatch(branches.migration_branch(app_id))


def test_worker_never_pushes_to_a_default_branch():
    assert branches.resolve_push_branch("APP-HE", "main") == ("migration/app-he", "origin/main")
    assert branches.resolve_push_branch("APP-TE", "master", "") == ("migration/app-te", "origin/master")
    # An explicit review branch (demo flows) is kept with its own source ref.
    assert branches.resolve_push_branch("APP-X", "demo/amf-modernization", "347ec91") == ("demo/amf-modernization", "347ec91")
    assert branches.resolve_push_branch("APP-HE", "migration/app-he", "origin/main") == ("migration/app-he", "origin/main")


# ── honest outcomes ───────────────────────────────────────────────────────────

def test_parse_validation_status():
    assert parse_validation_status("# x\n\n## OVERALL STATUS: PARTIAL\n") == "PARTIAL"
    assert parse_validation_status("## OVERALL STATUS: **COMPLETE**") == "COMPLETE"
    assert parse_validation_status("no header") == ""


@pytest.mark.skipif(not _HE_ARTIFACTS.exists(), reason="audit dump not present")
def test_parse_validation_status_on_the_audit_artifacts():
    got = {p.parent.name: parse_validation_status(p.read_text()) for p in _HE_ARTIFACTS.glob("*/validation_summary.md")}
    assert got["20261005_140517_01f9ee9b"] == "PARTIAL"  # SDK v1->v2 no-op
    assert got["20261005_135107_b56a6004"] == "COMPLETE"


def test_step_outcome_states():
    b = "migration/app-he"
    assert step_outcome(False, "PARTIAL", "mvn -B -q compile", b)[0] == "no_change"
    status, msg = step_outcome(True, "PARTIAL", "mvn -B -q compile", b)
    assert status == "partial" and "validation PARTIAL" in msg and b in msg
    status, msg = step_outcome(True, "COMPLETE", "", b)
    assert status == "partial" and "build not verified" in msg
    status, msg = step_outcome(True, "COMPLETE", "mvn -B -q compile", b)
    assert status == "completed" and "build verified" in msg
    assert "Pushed to main" not in msg


def test_failed_synthesis_marks_plan_completed_with_errors():
    assert plan_final_status(0, {"status": "needs_review", "error": "AccessDeniedException"}) == "completed_with_errors"
    assert plan_final_status(0, {"status": "success"}) == "completed"
    assert plan_final_status(2, {"error": "x"}) == "failed"


def test_synthesis_failure_is_flagged(monkeypatch):
    from backend.src.worker import synthesis

    class Boom:
        def converse(self, **_):
            raise RuntimeError("AccessDeniedException: not authorized to perform bedrock:InvokeModel")

    monkeypatch.setattr(synthesis, "bedrock_client", Boom())
    out = synthesis.call_workbench_synthesis([{"step_number": 1, "td_name": "t", "status": "completed"}], {})
    assert out["error"].startswith("AccessDeniedException")
    assert plan_final_status(0, out) == "completed_with_errors"


def test_assessment_recommended_td_for_a_second_language_is_kept(transform):
    """APP-CLI (UAT 2026-10-12): Java monolith + Node.js 14 Lambda in the same repo. The
    assessment recommended AWS/nodejs-version-upgrade from ATX evidence, then the language
    guard failed LaunchTransformation with NoTransforms."""
    run, env = transform
    env.table.put_item(Item={
        "PK": "APP#APP-HE", "SK": "ANALYSIS#assessment-summary#2026-10-12T08:09:21Z",
        "result": {"transformations": {"recommended": [{"td_name": "AWS/nodejs-version-upgrade"}]}},
    })
    out, steps, failures = run("APP-HE", transformation_steps=["AWS/nodejs-version-upgrade", "AWS/python-version-upgrade"])
    assert [s["td_name"] for s in steps] == ["AWS/nodejs-version-upgrade"]
    assert [s["td_name"] for s in out["skipped_steps"]] == ["AWS/python-version-upgrade"]
    assert not failures


def test_non_upgrade_java_tds_are_not_told_to_upgrade_java(transform):
    run, _ = transform
    _, steps, _ = run("APP-HE", transformation_steps=["AWS/early-access-java-x86-to-graviton",
                                                       "AWS/java-performance-optimization"])
    ctx = {s["td_name"]: s["additional_context"] for s in steps}
    assert "Graviton (ARM64)" in ctx["AWS/early-access-java-x86-to-graviton"]
    assert "Optimize" in ctx["AWS/java-performance-optimization"]
    assert not any("upgrade to is Java" in c for c in ctx.values())


def test_agent_cannot_start_a_second_transformation_while_one_runs(te, monkeypatch):  # noqa: F811
    """APP-TE 2026-10-14: chat launched each step as its own parallel job on one branch.
    The first job is launched through the real tool so the PLAN fields are the real ones."""
    mod, env = te
    env.seed("APP-HE")
    monkeypatch.setattr(mod, "ECS_CLUSTER", "c")
    launched = []
    monkeypatch.setattr(mod, "_launch_ecs_task", lambda *a, **k: launched.append(a[0]) or
                        {"job_id": a[0], "status": "RUNNING", "task_arn": "arn:aws:ecs:::task/live"})
    status = {"value": "RUNNING"}
    monkeypatch.setattr(mod.ecs, "describe_tasks", lambda **kw: {"tasks": [
        {"taskArn": arn, "lastStatus": status["value"]} for arn in kw["tasks"]]})
    first = mod.handler({"tool_name": "start_code_transformation", "arguments": {"app_id": "APP-HE"}})
    assert first["status"] == "RUNNING" and len(launched) == 1
    second = mod.handler({"tool_name": "start_code_transformation", "arguments": {"app_id": "APP-HE"}})
    assert second["status"] == "rejected" and second["running_job_id"] == first["job_id"] and len(launched) == 1
    status["value"] = "STOPPED"  # a stopped task no longer blocks
    assert mod._running_transformation("APP-HE") == ""


def test_registry_dispatches_start_code_transformation_to_the_tool(te, monkeypatch):  # noqa: F811
    """A helper inserted under the decorator once got registered as this tool and returned ''."""
    mod, env = te
    assert mod.registry._tools["start_code_transformation"]["fn"] is mod.start_code_transformation
    env.seed("APP-HE")
    monkeypatch.setattr(mod, "_launch_ecs_task", lambda *a, **k: {"job_id": a[0], "status": "RUNNING", "task_arn": "x"})
    out = mod.handler({"tool_name": "start_code_transformation", "arguments": {"app_id": "APP-HE"}})
    assert isinstance(out, dict) and out.get("status") == "RUNNING"


def test_no_steps_approved_at_the_gate_skips_the_transformation(transform, monkeypatch):
    """APP-CLI 2026-10-19: the reviewer approved the assessment with no code
    transformation (ATX already ran elsewhere). An empty list used to fall back to a
    derived AWS/java-version-upgrade, i.e. a long ATX run nobody asked for."""
    import tool_execution as mod

    run, _ = transform
    succeeded = []
    monkeypatch.setattr(mod, "_succeed_task", lambda token, payload: succeeded.append((token, payload)))
    out, steps, failures = run("APP-HE", transformation_steps=[])
    assert out["status"] == "skipped" and steps == [] and not failures
    assert succeeded == [("tok", out)]


def test_missing_steps_still_derive_a_default(transform):
    """No list at all (not a gate decision) keeps the fallback — it exists so
    LaunchTransformation never waits 4h on a no-op."""
    import tool_execution as mod

    run, env = transform
    meta = env.seed("APP-HE")
    meta.pop("transformation_steps", None)
    env.table.put_item({"PK": "APP#APP-HE", "SK": "META", **meta})
    out = mod.start_code_transformation({"app_id": "APP-HE", "_task_token": "tok"})
    assert out.get("status") != "skipped"


def test_steps_run_in_the_assessments_recommended_order(transform):
    """The gate is a checkbox list (no order): Execution follows ATX's recommended order (APP-CLI 2026-10-27)."""
    run, env = transform
    env.seed("APP-HE")
    env.table.put_item({"PK": "APP#APP-HE", "SK": "ANALYSIS#assessment-summary#2099-01-01T00:00:00Z", "result": {
        "transformations": {"recommended": [{"td_name": "AWS/spring-boot-version-upgrade"},
                                            {"td_name": "AWS/java-version-upgrade"}]}}})
    _, steps, _ = run("APP-HE", transformation_steps=["AWS/java-version-upgrade", "AWS/early-access-comprehensive-upgrade",
                                                      "AWS/spring-boot-version-upgrade"])
    assert [s["td_name"] for s in steps] == ["AWS/spring-boot-version-upgrade", "AWS/java-version-upgrade",
                                             "AWS/early-access-comprehensive-upgrade"]


def test_security_fixer_runs_last_with_its_own_context(transform):
    """Ticked first at the gate, the fixer still runs after the upgrades it remediates."""
    run, _ = transform
    _, steps, _ = run("APP-HE", transformation_steps=["AWS/security-issue-fixer", "AWS/java-version-upgrade"])
    assert [s["td_name"] for s in steps] == ["AWS/java-version-upgrade", "AWS/security-issue-fixer"]
    ctx = steps[-1]["additional_context"]
    assert ctx.startswith("Remediate ONLY the security findings")
    assert "upgrade to Java" not in ctx
