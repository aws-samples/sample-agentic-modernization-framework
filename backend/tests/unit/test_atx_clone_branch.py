"""Branch-isolation tests for the ATX workbench clone path.

The clone URL carries no token: git gets it from env via the credential helper.
"""
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

from backend.src.worker.atx import clone_repo_full

URL = "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/app"


def _result(code=0, stderr=""):
    return Mock(returncode=code, stderr=stderr)


def test_existing_remote_branch_uses_normal_clone(tmp_path: Path):
    target = tmp_path / "repo"
    with patch(
        "backend.src.worker.atx.subprocess.run", return_value=_result()
    ) as run:
        assert clone_repo_full(URL, "feature", target, "legacy")
    run.assert_called_once_with(
        ["git", "clone", "--branch", "feature", "--", URL, str(target)],
        capture_output=True,
        text=True,
        env=ANY, timeout=ANY,
    )


def test_missing_branch_requires_explicit_source_ref(tmp_path: Path):
    target = tmp_path / "repo"
    with patch(
        "backend.src.worker.atx.subprocess.run", return_value=_result(1, "not found")
    ) as run:
        assert not clone_repo_full(URL, "feature", target)
    assert run.call_count == 1


def test_missing_branch_is_created_from_exact_source_ref(tmp_path: Path):
    target = tmp_path / "repo"
    results = [_result(1, "remote branch absent"), _result(), _result()]
    with patch(
        "backend.src.worker.atx.subprocess.run", side_effect=results
    ) as run:
        assert clone_repo_full(URL, "demo/amf-modernization", target, "347ec91")
    assert run.call_args_list == [
        call(
            ["git", "clone", "--branch", "demo/amf-modernization", "--", URL, str(target)],
            capture_output=True,
            text=True,
            env=ANY, timeout=ANY,
        ),
        call(["git", "clone", "--", URL, str(target)], capture_output=True, text=True, env=ANY, timeout=ANY),
        call(
            ["git", "checkout", "-b", "demo/amf-modernization", "347ec91"],
            cwd=str(target),
            capture_output=True,
            text=True,
            env=ANY, timeout=ANY,
        ),
    ]


def test_source_ref_checkout_failure_stops_before_atx(tmp_path: Path):
    target = tmp_path / "repo"
    results = [_result(1), _result(), _result(1, "bad ref")]
    with patch(
        "backend.src.worker.atx.subprocess.run", side_effect=results
    ):
        assert not clone_repo_full(URL, "feature", target, "missing-commit")


def test_review_base_is_published_before_modernization_checkout(tmp_path: Path):
    target = tmp_path / "repo"
    results = [_result(1), _result(), _result(), _result()]
    with patch(
        "backend.src.worker.atx.subprocess.run", side_effect=results
    ) as run:
        assert clone_repo_full(
            URL, "demo/amf-modernization", target, "347ec91", "demo/legacy-baseline"
        )
    assert run.call_args_list[2] == call(
        ["git", "push", "origin", "347ec91:refs/heads/demo/legacy-baseline"],
        cwd=str(target), capture_output=True, text=True,
        env=ANY, timeout=ANY,
    )
    assert run.call_args_list[3] == call(
        ["git", "checkout", "-b", "demo/amf-modernization", "347ec91"],
        cwd=str(target), capture_output=True, text=True,
        env=ANY, timeout=ANY,
    )


def test_stale_recorded_branch_falls_back_to_default_branch(tmp_path: Path):
    """APP-TE 2026-10-13: L2 recorded `master`; the repo only has `main`."""
    target = tmp_path / "repo"
    results = [_result(1, "remote branch absent"), _result(), _result(1), _result()]
    with patch(
        "backend.src.worker.atx.subprocess.run", side_effect=results
    ) as run:
        assert clone_repo_full(URL, "migration/app-te", target, "origin/master")
    assert run.call_args_list[3] == call(
        ["git", "checkout", "-b", "migration/app-te", "HEAD"],
        cwd=str(target), capture_output=True, text=True,
        env=ANY, timeout=ANY,
    )


def test_option_like_or_bad_inputs_never_reach_git(tmp_path: Path):
    """A clone URL / branch / ref that git would parse as an option (or a non-https transport) is refused."""
    target = tmp_path / "repo"
    with patch("backend.src.worker.atx.subprocess.run", return_value=_result()) as run:
        assert not clone_repo_full("--upload-pack=touch /tmp/pwned", "feature", target)
        assert not clone_repo_full("ext::sh -c touch% /tmp/pwned", "feature", target)
        assert not clone_repo_full("file:///etc", "feature", target)
        assert not clone_repo_full(URL, "-f", target)
        assert not clone_repo_full(URL, "feature", target, "--orphan")
        assert not clone_repo_full(URL, "feature", target, "legacy", "a..b")
    run.assert_not_called()
