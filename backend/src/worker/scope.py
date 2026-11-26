"""Scope ATX to the app's build_dir.

A repo can hold many projects (APP-MOVIES lives in one folder of the
aws-cdk-examples monorepo). Running ATX at the repo root analysed the whole
repo and let a Python upgrade rewrite ~45 unrelated examples, so analysis and
transforms run on the app's folder, and a transform that still writes outside
it is reported instead of passing silently.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from backend.src.worker.argsafe import confined
from backend.src.worker.config import logger
from backend.src.worker.gitcmd import run_git

# Root-level files a transform may legitimately change for an app in a sub-folder
# (container + dependency + build files), and AMF's own committed directories.
_ALLOWED_ROOT_FILES = (
    "Dockerfile", "requirements*.txt", "pytest.ini", "pyproject.toml", "setup.cfg",
    "pom.xml", "build.gradle*", "settings.gradle*", ".dockerignore",
)
_ALLOWED_DIRS = ("cicd/", "infra/")


def normalize_build_dir(value) -> str:
    """'' for the repo root ('', '.', '/', None), else the folder without surrounding slashes."""
    v = str(value or "").strip().strip("/")
    return "" if v in ("", ".") else v


def app_build_dir(app: dict | None) -> str:
    """The app's build_dir: META build_dir, else the Assessment-detected one."""
    app = app or {}
    return normalize_build_dir(app.get("build_dir") or app.get("detected_build_dir"))


def primary_repo_name(app: dict | None) -> str:
    """Name of the repo build_dir refers to: the first CodeCommit repo, else the first repo."""
    repos = [r for r in (app or {}).get("repos") or [] if str(r.get("url") or "").strip()]
    if not repos:
        return ""
    repos.sort(key=lambda r: 0 if "codecommit" in str(r.get("url")) or r.get("provider") == "codecommit" else 1)
    return str(repos[0]["url"]).strip().rstrip("/").split("/")[-1].replace(".git", "")


def atx_path(base: Path, rel: str) -> str:
    """The `-p` argument for ATX run from `base`: `rel` when it exists there, else '.'.

    One argv element (callers pass a list, never a shell string). `rel` must stay inside `base`
    (no `..`, absolute path or symlink escape) and can't start with '-' (ATX would parse an option).
    """
    rel = normalize_build_dir(rel)
    if not rel:
        return "."
    try:
        inside = confined(base, rel)
    except ValueError:
        logger.warning("build_dir %r escapes %s — running ATX on the whole tree", rel, base)
        return "."
    if rel.startswith("-") or not inside.is_dir():
        logger.warning("build_dir %r not found under %s — running ATX on the whole tree", rel, base)
        return "."
    return rel


def out_of_scope(paths: list[str], build_dir: str) -> list[str]:
    """Changed paths outside build_dir that aren't allow-listed root/AMF files."""
    bd = normalize_build_dir(build_dir)
    if not bd:
        return []
    prefix = bd + "/"
    bad = []
    for p in paths:
        if not p or p.startswith(prefix) or p.startswith(_ALLOWED_DIRS):
            continue
        if "/" not in p and any(fnmatch.fnmatch(p, pat) for pat in _ALLOWED_ROOT_FILES):
            continue
        bad.append(p)
    return bad


def changed_paths(repo_dir: Path, since: str) -> list[str]:
    """Files changed between `since` and HEAD ([] when either is unknown)."""
    if not since:
        return []
    r = run_git(["diff", "--name-only", f"{since}..HEAD"], cwd=str(repo_dir),
                       capture_output=True, text=True, check=False)
    return [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def scope_outcome(outcome: str, message: str, paths: list[str], build_dir: str) -> tuple[str, str]:
    """Downgrade a step to `partial` when it wrote outside build_dir (changes are kept)."""
    bad = out_of_scope(paths, build_dir)
    if not bad or outcome == "no_change":
        return outcome, message
    shown = ", ".join(bad[:10]) + (f" (+{len(bad) - 10} more)" if len(bad) > 10 else "")
    return "partial", (f"{message}; changed {len(bad)} path(s) outside build_dir "
                       f"'{normalize_build_dir(build_dir)}': {shown}")
