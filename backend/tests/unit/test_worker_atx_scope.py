"""Spec 12: ATX runs on the app's build_dir, and a transform that writes outside it is flagged."""
import subprocess
from pathlib import Path

from backend.src.worker import atx
from backend.src.worker.scope import (
    app_build_dir,
    atx_path,
    changed_paths,
    normalize_build_dir,
    out_of_scope,
    primary_repo_name,
    scope_outcome,
)

BD = "python/apigw-http-api-lambda-dynamodb-python-cdk/lambda/apigw-handler"
CC = "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "aws-cdk-examples"
    (repo / BD).mkdir(parents=True)
    (repo / BD / "index.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    return repo


def test_build_dir_resolution():
    assert normalize_build_dir("/a/b/") == "a/b"
    assert normalize_build_dir(".") == "" and normalize_build_dir(None) == ""
    assert app_build_dir({"build_dir": "", "detected_build_dir": "svc/"}) == "svc"
    assert app_build_dir({"build_dir": "app", "detected_build_dir": "svc"}) == "app"
    assert primary_repo_name({"repos": [{"url": "https://github.com/a/other"},
                                        {"url": CC + "aws-cdk-examples"}]}) == "aws-cdk-examples"


def test_atx_path_scopes_to_existing_build_dir(tmp_path):
    repo = _repo(tmp_path)
    assert atx_path(repo, BD) == BD
    assert atx_path(repo, "") == "."
    assert atx_path(repo, "does/not/exist") == "."  # falls back, never fails the run
    assert atx_path(repo, "dir with space") == "."
    (repo / "dir with space").mkdir()
    assert atx_path(repo, "dir with space") == "dir with space"  # one argv element: no shell quoting


def test_atx_path_never_escapes_the_repo(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "outside").mkdir()
    (repo / "link").symlink_to(tmp_path / "outside")
    (repo / "-x").mkdir()
    assert atx_path(repo, "../outside") == "."
    assert atx_path(repo, str(tmp_path / "outside")) == "."  # absolute → normalized, then confined
    assert atx_path(repo, "link") == "."                     # symlink out of the tree
    assert atx_path(repo, "-x") == "."                       # would parse as an ATX option


def test_out_of_scope_allows_build_dir_root_build_files_and_amf_dirs():
    paths = [f"{BD}/index.py", "Dockerfile", "requirements-dev.txt", "pom.xml", "cicd/buildspec.yml",
             "infra/main.tf", "notes/spec/units.md", "python/api-cors-lambda/app.py",
             "python/url-shortener/setup.py", "sub/Dockerfile"]
    # Only cicd/ and infra/ are AMF-owned directories in an app repo.
    assert out_of_scope(paths, BD) == ["notes/spec/units.md", "python/api-cors-lambda/app.py",
                                       "python/url-shortener/setup.py", "sub/Dockerfile"]
    assert out_of_scope(paths, "") == []  # unscoped: today's behaviour


def test_transform_outside_build_dir_downgrades_to_partial(tmp_path):
    repo = _repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    (repo / BD / "index.py").write_text("x = 2\n")
    for i in range(12):
        (repo / f"other{i}.py").write_text("y = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "atx"], cwd=repo, check=True)
    paths = changed_paths(repo, base)
    outcome, msg = scope_outcome("completed", "Pushed to migration/app-movies", paths, BD)
    assert outcome == "partial"
    assert "12 path(s) outside build_dir" in msg and "(+2 more)" in msg
    assert scope_outcome("completed", "ok", [f"{BD}/index.py"], BD) == ("completed", "ok")
    assert scope_outcome("no_change", "none", paths, BD) == ("no_change", "none")
    assert scope_outcome("completed", "ok", paths, "") == ("completed", "ok")


def _run_analysis(tmp_path, monkeypatch, repo_names, build_dir, primary=""):
    """run_atx_analysis with clone/ATX stubbed; returns the ATX command it ran."""
    ws = tmp_path / "ws"
    monkeypatch.setattr(atx, "Path", lambda p: ws if p == "/atx-workspace" else Path(p))
    monkeypatch.setattr(atx, "configure_git", lambda: None)

    def fake_clone(url, branch, target):
        (target / build_dir).mkdir(parents=True, exist_ok=True) if build_dir else target.mkdir(parents=True)
        return True

    monkeypatch.setattr(atx, "clone_repo", fake_clone)
    seen = {}
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:1] == ["atx"]:
            seen["cmd"] = cmd
            raise RuntimeError("stop")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(atx.subprocess, "run", fake_run)
    monkeypatch.setattr(atx.shutil, "which", lambda name: f"/usr/bin/{name}")
    try:
        atx.run_atx_analysis("APP-X", [{"url": CC + n, "branch": "main"} for n in repo_names],
                             build_dir=build_dir, primary_repo=primary)
    except RuntimeError:
        pass
    return seen["cmd"]


def test_code_analysis_command_is_scoped_for_a_monorepo(tmp_path, monkeypatch):
    cmd = _run_analysis(tmp_path, monkeypatch, ["aws-cdk-examples"], BD, "aws-cdk-examples")
    assert cmd[cmd.index("-p") + 1] == f"aws-cdk-examples/{BD}"


def test_code_analysis_command_is_unscoped_without_build_dir(tmp_path, monkeypatch):
    cmd = _run_analysis(tmp_path, monkeypatch, ["flask-app"], "")
    assert cmd[cmd.index("-p") + 1] == "."


def test_code_analysis_stays_unscoped_for_several_repos(tmp_path, monkeypatch):
    cmd = _run_analysis(tmp_path, monkeypatch, ["a", "b"], BD, "a")
    assert cmd[cmd.index("-p") + 1] == "."
