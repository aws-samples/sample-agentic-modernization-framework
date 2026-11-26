"""Every git subprocess the worker runs.

AMF sources AWS CodeCommit only. Git authenticates through the AWS CodeCommit credential helper
(the ECS task role), so no token is ever handed to git, written to ~/.gitconfig or put in a clone URL.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed
from pathlib import Path

from backend.src.config import settings


def git_env() -> dict:
    """The worker's env for git (never sandbox/ATX/terraform subprocesses)."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # a missing credential fails fast instead of waiting on a tty
    return env


def run_git(args: list[str], cwd: str | Path | None = None, **kwargs) -> subprocess.CompletedProcess:
    """`git <args>`. kwargs pass through to subprocess.run.

    List argv, never a shell. Callers validate repo-/record-controlled values first (worker/argsafe.py:
    clone URLs, ref names) and put `--` before positional URLs/paths. Bounded by git_cmd_timeout_seconds.
    """
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    kwargs.setdefault("timeout", settings.git_cmd_timeout_seconds)
    return subprocess.run(["git", *args], env=git_env(), **kwargs)  # nosec B603 B607 — list argv and validated inputs
