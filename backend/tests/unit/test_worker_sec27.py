"""repo-controlled build/scan code never runs with the worker's credentials."""
import subprocess

import pytest

from backend.src.worker import ci_readiness

_SECRETS = {
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.2/v2/credentials/abc",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN": "tok",
    "AWS_ACCESS_KEY_ID": "ASIAREALKEY",
    "AWS_SECRET_ACCESS_KEY": "realsecret",  # pragma: allowlist secret — test fixture, not a credential
    "AWS_SESSION_TOKEN": "realsession",
    "AWS_PROFILE": "deploy",
    "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/token",
    "AWS_ROLE_ARN": "arn:aws:iam::1:role/uat-deploy-role",
    "ECS_CONTAINER_METADATA_URI": "http://169.254.170.2/v3/x",
    "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/x",
    "SFN_TASK_TOKEN": "task-token",
    "SFN_CALLBACK": "sfn-callback-on",
    "GITHUB_TOKEN": "ghp_real",
    "GRAPH_DB_PASSWORD": "pw",  # pragma: allowlist secret — test fixture, not a credential
}
_GONE = [k for k in _SECRETS if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")]


@pytest.fixture
def secrets_env(monkeypatch):
    for k, v in _SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", "/usr/bin")


def _assert_sandboxed(env):
    assert env is not None, "subprocess inherited the worker environment"
    leaked = [k for k in _GONE if k in env]
    assert not leaked, leaked
    # the only AWS_* left are inert fakes / switches, never a real value
    assert env["AWS_ACCESS_KEY_ID"] == "testing" and env["AWS_SECRET_ACCESS_KEY"] == "testing"  # noqa: S105
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert not any(k.startswith("SFN_") for k in env)
    assert set(_SECRETS.values()).isdisjoint(env.values())
    assert env["PATH"].endswith("/usr/bin")


def test_sandbox_env_strips_every_credential_source(secrets_env):
    _assert_sandboxed(ci_readiness.sandbox_env())


def test_ci_readiness_tool_runs_are_all_sandboxed(tmp_path, secrets_env):
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "app.py").write_text("print('x')\n")
    calls = []

    def runner(cmd, cwd=None, env=None, timeout=None):
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    tools = ci_readiness._Tools(tmp_path, runner)
    ci_readiness._lint(tools)
    ci_readiness._tests_and_coverage(tools, 80)
    assert len(calls) >= 5  # venv, pip tools, pip -r, ruff, coverage run, coverage report
    for _, env in calls:
        _assert_sandboxed(env)
