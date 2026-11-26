"""Worker deploy / tear-down: MODE=infra INFRA_ACTION=apply|destroy."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from backend.src.worker import deploy_runner as dr

COMMIT = "a" * 40
APP = {"app_id": "APP-MUSIC", "repos": [{"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/music"}]}
PLAN_OK = {"planned_values": {"root_module": {"resources": []}}, "resource_changes": []}
PUBLIC_LB = {"resource_changes": [{"address": "module.compute.aws_lb.main", "type": "aws_lb", "change": {
    "actions": ["create"], "after": {"internal": False, "load_balancer_type": "application"}}}]}


class FakeCC:
    def __init__(self, head=COMMIT):
        self.head = head

    def get_branch(self, repositoryName, branchName):  # noqa: N803
        assert branchName == "migration/app-music"
        return {"branch": {"commitId": self.head}}

    def get_folder(self, repositoryName, folderPath, commitSpecifier):  # noqa: N803
        assert commitSpecifier in (COMMIT, self.head)
        if folderPath == "infra":
            return {"files": [{"absolutePath": "infra/main.tf"}], "subFolders": [{"absolutePath": "infra/modules/vpc"}]}
        return {"files": [{"absolutePath": "infra/modules/vpc/main.tf"}], "subFolders": []}

    def get_file(self, repositoryName, filePath, commitSpecifier):  # noqa: N803
        return {"fileContent": f"# {filePath}@{commitSpecifier[:8]}\n".encode()}


class FakeSTS:
    def assume_role(self, RoleArn, RoleSessionName, DurationSeconds):  # noqa: N803
        # Role chaining (worker role → deploy role) caps the session at 1 hour.
        assert RoleSessionName == "amf-deploy-APP-MUSIC" and DurationSeconds == 3600
        return {"Credentials": {"AccessKeyId": "AKIADEPLOY", "SecretAccessKey": "sek", "SessionToken": "tok"}}  # pragma: allowlist secret — test fixture, not a credential


def tf_runner(results: dict | None = None, show=PLAN_OK):
    """Stub `terraform …` subprocess.run; records every call's env."""
    results = results or {}
    calls = []

    def run(cmd, cwd=None, env=None, **_):
        calls.append({"cmd": cmd, "env": env})
        verb = cmd[1]
        if verb == "show":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(show), "")
        if verb == "output":
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"alb_dns": {"value": "internal-x"},
                                                                   "db": {"value": "s", "sensitive": True}}), "")
        rc, out = results.get(verb, (0, f"{verb} ok"))
        return subprocess.CompletedProcess(cmd, rc, out, "")

    run.calls = calls
    return run


class FakeSession:
    def __init__(self, cp):
        self.cp = cp

    def client(self, name):
        assert name == "codepipeline"
        return self.cp


@pytest.fixture(autouse=True)
def no_uploads(monkeypatch):
    monkeypatch.setattr(dr, "_upload", lambda name, body: None)


def _apply(tmp_path, cc=None, runner=None, waived=False, pipeline=None):
    return dr.apply(APP, "APP-MUSIC", COMMIT, "arn:aws:iam::111122223333:role/amf-deploy", cc=cc or FakeCC(),
                    sts=FakeSTS(), workdir=tmp_path, runner=runner or tf_runner(), waived=waived,
                    session_factory=lambda creds, region: FakeSession(None),
                    pipeline_wait=pipeline or (lambda cp, name, commit: {"status": "deployed",
                                                                          "pipeline_execution_id": "pe-1"}))


def test_branch_moved_refuses_before_touching_terraform(tmp_path):
    runner = tf_runner()
    with pytest.raises(dr.DeployError) as e:
        _apply(tmp_path, cc=FakeCC(head="b" * 40), runner=runner)
    assert e.value.status == "branch_moved"
    assert e.value.message == "migration branch moved from aaaaaaaa to bbbbbbbb; review the new head"
    assert runner.calls == []


def test_apply_uses_the_reviewed_commit_and_deploy_creds_only_in_subprocess_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "WORKER")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/creds")
    before = dict(os.environ)
    runner = tf_runner()
    out = _apply(tmp_path, runner=runner)
    assert out == {"status": "deployed", "pipeline_execution_id": "pe-1", "commit": COMMIT,
                   "outputs": {"alb_dns": "internal-x", "db": "(sensitive)"}}
    assert (tmp_path / "main.tf").read_text() == f"# infra/main.tf@{COMMIT[:8]}\n"
    assert (tmp_path / "modules/vpc/main.tf").exists()
    verbs = [c["cmd"][1] for c in runner.calls]
    assert verbs == ["init", "plan", "show", "apply", "output"]
    assert runner.calls[3]["cmd"][-1] == "tfplan"  # applies exactly the reviewed plan
    for c in runner.calls:
        assert c["env"]["AWS_ACCESS_KEY_ID"] == "AKIADEPLOY" and c["env"]["AWS_SESSION_TOKEN"] == "tok"
        assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in c["env"]
    assert dict(os.environ) == before  # the worker's own environment is untouched


def test_exposure_policy_refuses_apply_unless_waived(tmp_path):
    runner = tf_runner(show=PUBLIC_LB)
    with pytest.raises(dr.DeployError) as e:
        _apply(tmp_path, runner=runner)
    assert e.value.status == "error" and e.value.message.startswith("exposure policy:")
    assert "apply" not in [c["cmd"][1] for c in runner.calls]
    assert _apply(tmp_path / "w", runner=tf_runner(show=PUBLIC_LB), waived=True)["status"] == "deployed"


def test_apply_failure_reports_the_tail(tmp_path):
    with pytest.raises(dr.DeployError) as e:
        _apply(tmp_path, runner=tf_runner({"apply": (1, "Error: creating ECS Service: AccessDenied")}))
    assert e.value.status == "apply_failed" and "AccessDenied" in e.value.message


class FakeCP:
    def __init__(self, sequence, failed_action=None):
        self.sequence, self.started, self.failed_action = list(sequence), 0, failed_action

    def list_pipeline_executions(self, pipelineName, maxResults):  # noqa: N803
        return {"pipelineExecutionSummaries": self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]}

    def start_pipeline_execution(self, name):
        self.started += 1

    def list_action_executions(self, pipelineName, filter):  # noqa: A002, N803
        return {"actionExecutionDetails": [self.failed_action] if self.failed_action else []}

    def get_pipeline_execution(self, pipelineName, pipelineExecutionId):  # noqa: N803
        return {"pipelineExecution": {"status": "Failed"}}


def _run(status, rev=COMMIT, pid="pe-9"):
    return [{"pipelineExecutionId": pid, "status": status, "sourceRevisions": [{"revisionId": rev}]}]


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_pipeline_success_and_failure_mapping():
    clock = Clock()
    cp = FakeCP([_run("InProgress"), _run("Succeeded")])
    assert dr.wait_for_pipeline(cp, "APP-MUSIC-pipeline", COMMIT, sleep=clock.sleep, clock=clock) == {
        "status": "deployed", "pipeline_execution_id": "pe-9"}
    failed = {"stageName": "Build", "actionName": "Build", "status": "Failed",
              "output": {"executionResult": {"externalExecutionSummary": "Build failed: trivy image"}}}
    out = dr.wait_for_pipeline(FakeCP([_run("InProgress"), _run("Failed")], failed), "P", COMMIT,
                               sleep=clock.sleep, clock=clock)
    assert out["status"] == "pipeline_failed" and out["message"] == "Build/Build failed: Build failed: trivy image"


def test_pipeline_started_once_when_no_run_of_the_commit_appears():
    clock = Clock()
    cp = FakeCP([_run("Succeeded", rev="c" * 40)])  # only an older commit's run
    with pytest.raises(dr.DeployError) as e:
        dr.wait_for_pipeline(cp, "P", COMMIT, sleep=clock.sleep, clock=clock, timeout=600, start_grace=180)
    assert cp.started == 1 and e.value.status == "pipeline_failed"


def test_destroy_path(tmp_path):
    runner = tf_runner()
    assert dr.destroy(APP, "APP-MUSIC", "arn:role", cc=FakeCC(), sts=FakeSTS(), workdir=tmp_path,
                      runner=runner) == "destroyed"
    assert [c["cmd"][1] for c in runner.calls] == ["init", "destroy"]
    assert "-auto-approve" in runner.calls[1]["cmd"]
    with pytest.raises(dr.DeployError) as e:
        dr.destroy(APP, "APP-MUSIC", "arn:role", cc=FakeCC(), sts=FakeSTS(), workdir=tmp_path / "x",
                   runner=tf_runner({"destroy": (1, "Error: DependencyViolation")}))
    assert e.value.status == "destroy_failed"


def test_run_deploy_always_calls_back_success_with_status(monkeypatch, tmp_path):
    from backend.src.worker import dynamo, sfn_callback, terraform_runner

    sent, meta = {}, {}
    monkeypatch.setattr(dr, "read_full_app", lambda app_id: APP)
    monkeypatch.setattr(dr, "APP_ID", "APP-MUSIC")
    monkeypatch.setattr(dr, "DEPLOY_COMMIT", COMMIT)
    monkeypatch.setattr(dr, "DEPLOY_ROLE_ARN", "arn:role")
    monkeypatch.setattr(dr.boto3, "client", lambda name, **k: FakeCC(head="b" * 40) if name == "codecommit" else FakeSTS())
    monkeypatch.setattr(sfn_callback, "resolve_token", lambda job: "tok")
    monkeypatch.setattr(sfn_callback, "finalize_job_once", lambda *a, **k: True)
    monkeypatch.setattr(sfn_callback, "send_success", lambda tok, p: sent.update(p))
    monkeypatch.setattr(sfn_callback, "send_failure", lambda *a: sent.update({"failure": True}))
    monkeypatch.setattr(dynamo, "update_meta_fields", lambda app_id, f, **k: meta.update(f))
    monkeypatch.setattr(dynamo, "put_result_record", lambda *a, **k: None)
    monkeypatch.setattr(terraform_runner, "_exposure_waived", lambda: False)
    dr.run_deploy("apply")
    assert sent["status"] == "branch_moved" and "failure" not in sent
    assert meta["exec_deploy_status"] == "branch_moved" and "review the new head" in meta["exec_deploy_error"]


def test_infra_action_unset_keeps_the_dry_run(monkeypatch):
    from backend.src.worker import config, terraform_runner

    called = []
    monkeypatch.setattr(config, "INFRA_ACTION", "")
    monkeypatch.setattr(dr, "run_deploy", lambda a: called.append(a))
    monkeypatch.setattr(terraform_runner, "resolve_token", lambda job: (_ for _ in ()).throw(RuntimeError("dry-run")))
    with pytest.raises(RuntimeError, match="dry-run"):
        terraform_runner.run_terraform()
    assert called == []
    monkeypatch.setattr(config, "INFRA_ACTION", "destroy")
    terraform_runner.run_terraform()
    assert called == ["destroy"]


def test_sam_payload_format(tmp_path):
    from backend.src.worker.repo_scan import scan_wiring

    tpl = """Resources:
  Rest:
    Type: AWS::Serverless::Function
    Properties:
      Handler: a.h
      Events:
        E: {Type: Api, Properties: {Path: /a, Method: get}}
  Http:
    Type: AWS::Serverless::Function
    Properties:
      Handler: b.h
      Events:
        E: {Type: HttpApi, Properties: {Path: /b, Method: get}}
  Plain:
    Type: AWS::Serverless::Function
    Properties:
      Handler: c.h
"""
    (tmp_path / "template.yaml").write_text(tpl)
    fns = {f["name"]: f["payload_format"] for f in scan_wiring(Path(tmp_path), "")["lambda_functions"]}
    assert fns == {"Rest": "1.0", "Http": "2.0", "Plain": "1.0"}


STALE_STATE = {"resource_changes": [
    {"address": "module.vpc.aws_vpc.main", "type": "aws_vpc", "change": {"actions": ["delete", "create"]}},
    {"address": "module.compute.aws_ecs_cluster.main", "type": "aws_ecs_cluster", "change": {"actions": ["delete"]}},
    {"address": "module.compute.aws_lambda_function.app", "type": "aws_lambda_function", "change": {"actions": ["create"]}},
]}


def test_a_deploy_plan_that_destroys_refuses_before_apply(tmp_path, monkeypatch):
    """APP-CLI 2026-10-26: the reviewed dry-run plan was 62 add / 0 destroy, the real-state plan
    replaced a stale stack (31 deletes) and was applied without review."""
    uploads = {}
    monkeypatch.setattr(dr, "_upload", lambda name, body: uploads.__setitem__(name, body))
    runner = tf_runner(show=STALE_STATE)
    with pytest.raises(dr.DeployError) as e:
        _apply(tmp_path, runner=runner)
    assert e.value.status == "plan_destroys" and "2 resource(s)" in e.value.message
    assert "module.vpc.aws_vpc.main" in e.value.message and "deploy-plan.txt" in uploads
    assert "apply" not in [c["cmd"][1] for c in runner.calls]


def test_destroy_is_applied_only_with_explicit_opt_in(tmp_path):
    app = {**APP, "deploy_allow_destroy": True}
    out = dr.apply(app, "APP-MUSIC", COMMIT, "arn:aws:iam::111122223333:role/amf-deploy", cc=FakeCC(), sts=FakeSTS(),
                   workdir=tmp_path, runner=tf_runner(show=STALE_STATE), session_factory=lambda c, r: FakeSession(None),
                   pipeline_wait=lambda cp, name, commit: {"status": "deployed", "pipeline_execution_id": "pe-1"})
    assert out["status"] == "deployed"


def test_destroy_without_the_migration_branch_uses_the_state_alone(tmp_path, monkeypatch):
    """APP-CLI 2026-10-27: the migration branch was archived, so teardown could not fetch infra/."""
    class GoneCC(FakeCC):
        def get_branch(self, repositoryName, branchName):  # noqa: N803
            raise RuntimeError("BranchDoesNotExistException")

    monkeypatch.setattr(dr.settings, "project_prefix", "uat")
    runner = tf_runner()
    app = {**APP, "target_account": "111122223333"}
    assert dr.destroy(app, "APP-MUSIC", "arn:role", cc=GoneCC(), sts=FakeSTS(), workdir=tmp_path,
                      runner=runner) == "destroyed"
    assert [c["cmd"][1] for c in runner.calls] == ["init", "destroy"]
    main = (tmp_path / "main.tf").read_text()
    assert 'bucket         = "uat-tfstate-111122223333-' in main and 'key            = "APP-MUSIC/terraform.tfstate"' in main
    assert 'dynamodb_table = "uat-tfstate-lock"' in main and 'provider "aws"' in main


def test_redeploying_a_commit_whose_run_failed_starts_a_fresh_run():
    """APP-HE 2026-10-27: attempt 2 reported attempt 1's failed run instantly instead of re-running."""
    clock = Clock()
    old = _run("Failed", pid="pe-1")[0]
    cp = FakeCP([[old], [old, _run("InProgress", pid="pe-2")[0]], [old, _run("Succeeded", pid="pe-2")[0]]])
    assert dr.wait_for_pipeline(cp, "P", COMMIT, sleep=clock.sleep, clock=clock) == {
        "status": "deployed", "pipeline_execution_id": "pe-2"}
    assert cp.started == 1


def test_fetch_infra_refuses_a_path_outside_the_workdir(tmp_path):
    class TraversalCC:
        def get_folder(self, repositoryName, folderPath, commitSpecifier):  # noqa: N803
            return {"files": [{"absolutePath": "infra/../../escape.tf"}], "subFolders": []}

        def get_file(self, repositoryName, filePath, commitSpecifier):  # noqa: N803
            return {"fileContent": b"x"}

    dest = tmp_path / "work"
    dest.mkdir()
    with pytest.raises(dr.DeployError):
        dr.fetch_infra(TraversalCC(), "repo", "a" * 40, dest)
    assert not (tmp_path / "escape.tf").exists()
