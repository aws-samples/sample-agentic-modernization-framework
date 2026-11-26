"""repo build code runs as an unprivileged build user; the git token never hits disk."""
import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest

from backend.src.worker import atx, ci_readiness, gitcmd, sandbox

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "ghp_r9_secret_value"  # noqa: S105 — test fixture
_SECRETS = {
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc",
    "AWS_SESSION_TOKEN": "realsession",
    "AWS_ACCESS_KEY_ID": "ASIAREALKEY",
    "SFN_TASK_TOKEN": "task-token",
    "GITHUB_TOKEN": TOKEN,
}


@pytest.fixture(autouse=True)
def _fresh_probe():
    sandbox.reset()
    yield
    sandbox.reset()


@pytest.fixture
def secrets_env(monkeypatch):
    for k, v in _SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", f"{os.path.expanduser('~')}/app/.venv/bin:/usr/local/bin:/usr/bin")


@pytest.fixture
def builduser(monkeypatch):
    """Pretend the image's build user exists (another uid, our gid so chgrp works in tmp)."""
    me = pwd.getpwuid(os.geteuid())
    fake = pwd.struct_passwd(("builduser", "x", me.pw_uid + 4242, os.getegid(), "", "/home/builduser",
                              "/usr/sbin/nologin"))
    monkeypatch.setitem(sandbox._state, "account", fake)
    return fake


def _assert_no_secrets(env: dict):
    assert set(_SECRETS.values()).isdisjoint(env.values())
    assert not any(k.startswith("SFN_") for k in env)
    assert "AMF_GIT_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert {k for k in env if k.startswith("AWS_")} <= {"AWS_EC2_METADATA_DISABLED", "AWS_DEFAULT_REGION",
                                                         "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    assert env["AWS_ACCESS_KEY_ID"] == "testing"


# ── R9.1 run_untrusted ──────────────────────────────────────────────────────

def test_run_untrusted_builds_the_sudo_argv_and_env(secrets_env, builduser, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    res = sandbox.run_untrusted(["mvn", "-B", "dependency:resolve"], cwd="/atx-workspace/r", timeout=60,
                                env={"VIRTUAL_ENV": "/opt/venv", "GITHUB_TOKEN": TOKEN})
    argv, env = seen["argv"], seen["env"]
    assert Path(argv[0]).name == "sudo" and argv[1:4] == ["-n", "-u", "builduser"]  # resolved sudo path
    preserve = argv[4]
    assert preserve.startswith("--preserve-env=") and set(preserve.split("=", 1)[1].split(",")) == set(env)
    assert argv[5] == "--" and argv[6:8] == ["/bin/sh", "-c"] and "umask 0002" in argv[8]
    assert argv[-3:] == ["mvn", "-B", "dependency:resolve"]
    assert argv[-7:-3][1:] == ["-k", "10", "60"]  # coreutils timeout kills it as the build user
    assert seen["cwd"] == "/atx-workspace/r" and seen["timeout"] > 60
    _assert_no_secrets(env)
    assert env["HOME"] == "/home/builduser"
    assert env["VIRTUAL_ENV"] == "/opt/venv"
    # atxuser's home isn't reachable for builduser; sudo's secure_path is undone via AMF_SANDBOX_PATH
    assert os.path.expanduser("~") not in env["PATH"]
    assert env["AMF_SANDBOX_PATH"] == env["PATH"]
    assert res.args == ["mvn", "-B", "dependency:resolve"] and res.stdout == "ok"


def test_run_untrusted_maps_the_sandbox_timeout_to_timeoutexpired(builduser, monkeypatch):
    monkeypatch.setattr(sandbox.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 124, "", ""))
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox.run_untrusted(["pytest"], timeout=5)


def test_fallback_without_build_user_keeps_r5_behaviour(secrets_env, monkeypatch, caplog):
    monkeypatch.setattr(sandbox.settings, "build_user", "amf-no-such-user-r9")
    calls = []
    monkeypatch.setattr(sandbox.subprocess, "run", lambda argv, **kw: calls.append((argv, kw))
                        or subprocess.CompletedProcess(argv, 0, "", ""))
    with caplog.at_level("WARNING"):
        sandbox.run_untrusted(["ruff", "check", "."], cwd="/x", timeout=5)
        sandbox.run_untrusted(["ruff", "check", "."], cwd="/x", timeout=5)
    assert [c[0] for c in calls] == [["ruff", "check", "."]] * 2
    _assert_no_secrets(calls[0][1]["env"])
    assert calls[0][1]["env"]["HOME"] == os.environ["HOME"]
    assert sum("build isolation unavailable" in r.message for r in caplog.records) == 1  # warned once


def test_disabled_isolation_never_calls_sudo(secrets_env, monkeypatch):
    monkeypatch.setattr(sandbox.settings, "build_isolation_enabled", False)
    monkeypatch.setattr(sandbox.settings, "build_user", pwd.getpwuid(os.geteuid()).pw_name)
    calls = []
    monkeypatch.setattr(sandbox.subprocess, "run", lambda argv, **kw: calls.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "", ""))
    sandbox.run_untrusted(["trivy", "fs", "."], timeout=5)
    assert calls == [["trivy", "fs", "."]]
    assert not sandbox.isolated()


def test_failed_sudo_probe_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(sandbox.pwd, "getpwnam", lambda n: pwd.struct_passwd(
        (n, "x", os.geteuid() + 1, os.getegid(), "", "/home/" + n, "/bin/false")))
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: "/usr/bin/sudo")
    monkeypatch.setattr(sandbox.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "a password is required"))
    with caplog.at_level("ERROR"):
        assert not sandbox.isolated()
    assert any("probe failed" in r.message for r in caplog.records)


def test_ci_readiness_and_ci_fix_tool_runs_go_through_run_untrusted(tmp_path, monkeypatch):
    from backend.src.worker import ci_fix

    seen = []
    monkeypatch.setattr(ci_readiness, "run_untrusted", lambda cmd, **kw: seen.append(cmd)
                        or subprocess.CompletedProcess(cmd, 0, "[]", ""))
    (tmp_path / "requirements.txt").write_text("flask\n")
    tools = ci_readiness._Tools(tmp_path, ci_readiness._default_runner)
    ci_readiness._lint(tools)
    ci_readiness._tests_and_coverage(tools, 80)
    ci_fix.ruff_fix(tmp_path, ci_readiness._default_runner)
    flat = [" ".join(c) for c in seen]
    assert any("-m venv" in c for c in flat) and any("pip install" in c and "-r" in c for c in flat)
    assert any("ruff check --output-format" in c for c in flat) and any("ruff check --fix" in c for c in flat)
    assert any("coverage run -m pytest" in c for c in flat)


def test_runner_default_is_sandboxed(monkeypatch):
    seen = []
    monkeypatch.setattr(ci_readiness, "run_untrusted", lambda cmd, **kw: seen.append((cmd, kw))
                        or subprocess.CompletedProcess(cmd, 0, "", ""))
    ci_readiness._default_runner(["pip", "install", "x"], cwd="/r", env={"A": "1"}, timeout=3)
    assert seen == [(["pip", "install", "x"], {"cwd": "/r", "timeout": 3, "env": {"A": "1"}})]


def test_ci_readiness_venv_uses_an_interpreter_outside_atxusers_home(tmp_path, builduser, monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox.os, "lchown", lambda *a: None)
    tools = ci_readiness._Tools(tmp_path, lambda cmd, **kw: calls.append(cmd)
                                or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert calls[0][1:3] == ["-m", "venv"]
    assert calls[0][0] == sandbox.untrusted_python()
    assert stat.S_IMODE(tools.dir.stat().st_mode) == 0o2770  # the build user can create the venv here


# ── R9.1 shared work tree ───────────────────────────────────────────────────

def test_prepare_build_tree_shares_the_work_tree_but_not_git(tmp_path, builduser):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "App.java").write_text("x")
    (repo / "gradlew").write_text("#!/bin/sh\n")
    (repo / "gradlew").chmod(0o755)
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / ".git" / "config").chmod(0o664)
    (repo / "link").symlink_to("/etc/passwd")
    old = os.umask(0o022)
    try:
        sandbox.prepare_build_tree(repo)
    finally:
        os.umask(old)

    def mode(p):
        return stat.S_IMODE(os.lstat(p).st_mode)

    assert mode(repo) & (stat.S_ISGID | stat.S_ISVTX | 0o070) == stat.S_ISGID | stat.S_ISVTX | 0o070
    assert mode(repo / "src") & (stat.S_ISGID | 0o070) == stat.S_ISGID | 0o070
    assert mode(repo / "src" / "App.java") & 0o060 == 0o060
    assert mode(repo / "gradlew") & 0o070 == 0o070
    assert mode(repo / ".git") & 0o022 == 0 and mode(repo / ".git" / "config") & 0o022 == 0
    assert os.lstat(repo / "src").st_gid == builduser.pw_gid


def test_prepare_build_tree_is_a_noop_without_isolation(tmp_path, monkeypatch):
    monkeypatch.setitem(sandbox._state, "account", None)
    f = tmp_path / "a"
    f.write_text("x")
    f.chmod(0o600)
    sandbox.prepare_build_tree(tmp_path)
    sandbox.prepare_build_tree(tmp_path / "missing")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


# ── R9.2 git: CodeCommit only, no token ────────────────────────────────────

@pytest.fixture
def git_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for var in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT", "GIT_ASKPASS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return home


def test_configure_git_sets_only_the_codecommit_helper(git_home):
    atx.configure_git()
    gitconfig = (git_home / ".gitconfig").read_text()
    assert "codecommit credential-helper" in gitconfig
    assert "github" not in gitconfig.lower() and "gitlab" not in gitconfig.lower()
    assert "insteadOf" not in gitconfig and "password" not in gitconfig


@pytest.mark.parametrize("url", [
    "https://github.com/org/app.git", "https://gitlab.example.com/team/app.git",
    "git@github.com:org/app.git", "ssh://git-codecommit.us-east-1.amazonaws.com/v1/repos/app",
    "https://git-codecommit.us-east-1.amazonaws.com.evil.com/v1/repos/app",
])
def test_clone_refuses_non_codecommit_urls(git_home, monkeypatch, url):
    calls = []
    monkeypatch.setattr(gitcmd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert atx.clone_repo(url, "main", git_home / "app") is False
    assert calls == []


def test_clone_accepts_codecommit_urls(git_home, monkeypatch):
    calls = []
    monkeypatch.setattr(gitcmd.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    url = "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/app"
    assert atx.clone_repo(url, "main", git_home / "app")
    assert any(c[:2] == ["git", "clone"] and url in c for c in calls)


def test_git_env_carries_no_token(secrets_env):
    env = gitcmd.git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0" and "AMF_GIT_TOKEN" not in env
    _assert_no_secrets(sandbox.untrusted_env())


def test_run_git_uses_list_argv(monkeypatch):
    seen = {}
    monkeypatch.setattr(gitcmd.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, **kw))
    gitcmd.run_git(["push", "origin", "b"], cwd=Path("/r"), capture_output=True)
    assert seen["cmd"] == ["git", "push", "origin", "b"] and seen["cwd"] == "/r"
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_no_worker_module_shells_out_to_git_directly():
    worker = REPO_ROOT / "backend/src/worker"
    offenders = [p.name for p in worker.glob("*.py") if p.name != "gitcmd.py" and '["git"' in p.read_text()]
    assert not offenders, offenders


# ── image ───────────────────────────────────────────────────────────────────

def test_worker_image_has_the_build_user_and_one_narrow_sudo_rule():
    df = (REPO_ROOT / "docker/Dockerfile.worker").read_text()
    assert "printf 'atxuser ALL=(builduser) NOPASSWD:SETENV: ALL\\n' > /etc/sudoers.d/builduser" in df
    assert "chmod 0440 /etc/sudoers.d/builduser" in df and "visudo -cf /etc/sudoers.d/builduser" in df
    assert "--gid buildshare builduser" in df and "usermod -aG buildshare atxuser" in df
    assert "chmod 700 /home/atxuser" in df
    assert "/home/builduser/.m2" in df
    assert df.count("NOPASSWD") == 1
