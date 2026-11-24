"""Validators for repo-/record-controlled strings before they reach a subprocess argv or cwd.

Every git / ATX call in the worker is list-form (no shell), so the remaining risks are a value that
git or ATX parses as an *option* (a clone URL `--upload-pack=...`, a branch `-f`, a TD name
`x -p /`) and a path that escapes the job workspace (`..`, an absolute path, a symlink). These
helpers reject both; callers turn a ValueError into a failed step instead of running anything.
"""
from __future__ import annotations

import re
from pathlib import Path

# Same accepted forms as the API's repo-link validation (api/v1/portfolio._CLONE_URL_RE): AWS CodeCommit
# only (HTTPS or git-remote-codecommit); never `ext::`, `file://`, another host or a leading '-'.
_CLONE_URL_RE = re.compile(r"^https://git-codecommit\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/v1/repos/[A-Za-z0-9._-]{1,100}$|^codecommit::[a-z0-9-]+://[A-Za-z0-9._@-]{1,200}$")

# git check-ref-format, conservatively: no leading '-' or '/', no '..', '@{', '//', control chars,
# spaces, '~^:?*[\\'; no trailing '/', '.' or '.lock'.
_REF_RE = re.compile(r"^(?!-)(?!/)(?!.*\.\.)(?!.*@\{)(?!.*//)[A-Za-z0-9._/+-]{1,255}$")

# ATX transformation-definition names as `atx custom def list` returns them (e.g. AWS/java-version-upgrade).
_TD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

# One directory name under the workspace (a repo name derived from its URL).
_DIR_NAME_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,128}$")


def safe_clone_url(url: str) -> str:
    u = str(url or "").strip()
    if not _CLONE_URL_RE.match(u):
        raise ValueError(f"refusing clone URL {u[:120]!r}: not an AWS CodeCommit URL")
    return u


def safe_ref(ref: str, what: str = "ref") -> str:
    r = str(ref or "").strip()
    if not _REF_RE.match(r) or r.endswith(("/", ".", ".lock")):
        raise ValueError(f"refusing {what} {r[:120]!r}: not a plain git ref name")
    return r


def safe_td_name(name: str) -> str:
    n = str(name or "").strip()
    if not _TD_NAME_RE.match(n) or ".." in n:
        raise ValueError(f"refusing ATX transformation name {n[:120]!r}")
    return n


def safe_dir_name(name: str) -> str:
    n = str(name or "").strip()
    if not _DIR_NAME_RE.match(n):
        raise ValueError(f"refusing directory name {n[:120]!r}")
    return n


def confined(base: Path, rel: str | Path) -> Path:
    """`base / rel` resolved (symlinks followed); ValueError unless it stays inside `base`."""
    root = Path(base).resolve()
    path = (root / rel).resolve()
    if path != root and not path.is_relative_to(root):
        raise ValueError(f"refusing path {str(rel)[:120]!r}: outside {root}")
    return path
