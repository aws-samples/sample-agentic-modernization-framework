"""Run repo-controlled build/scan code as the unprivileged build user.

R5 already strips credentials from the environment (`ci_readiness.sandbox_env`). R9 adds an OS-user
boundary: the command runs as `settings.build_user` through the image's single sudo rule
(`atxuser ALL=(builduser) NOPASSWD:SETENV: ALL` — no root). As another uid it can't read the worker's
`/proc/<pid>/environ` (the task-role credential URI) or `atxuser`'s home (mode 700).

Shared tree: the build user's primary group (`buildshare` in the image; `atxuser` is a member) owns
every clone. `prepare_build_tree` makes the work tree group-writable with setgid dirs so builduser
builds in place, while `.git` stays atxuser-only (a build that could write `.git/config` or hooks
would run code as atxuser at the next `git commit`) and the repo root is sticky (it can't swap `.git`).

Only mvn/gradlew/pip/ruff/pytest go through here. git, ATX and terraform need credentials and
keep running as atxuser.

Without the user or sudo (a laptop, CI tests) or with `build_isolation_enabled=False`, commands run as
today (sandbox_env only) and a warning is logged once.
"""
from __future__ import annotations

import logging
import os
import pwd
import shutil
import stat
import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed
import sys
import tempfile
from pathlib import Path

from backend.src.config import settings

logger = logging.getLogger(__name__)

# umask 0002 so atxuser (ATX, git) can still modify/delete what the build creates; PATH is restored
# because sudo's secure_path replaces it; coreutils `timeout` kills the build as the build user
# (killing `sudo` from here would orphan it — atxuser can't signal builduser's processes).
_WRAPPER = 'umask 0002; PATH="$AMF_SANDBOX_PATH"; export PATH; unset AMF_SANDBOX_PATH; exec "$@"'
_TIMEOUT_EXIT = 124  # coreutils timeout

_state: dict = {}


def reset() -> None:
    """Forget the cached isolation probe (tests)."""
    _state.clear()


def _warn_once(key: str, level: int, msg: str, *args) -> None:
    if not _state.get(key):
        _state[key] = True
        logger.log(level, msg, *args)


def build_account() -> pwd.struct_passwd | None:
    """The build user's passwd entry when isolation is enabled and usable, else None (fallback)."""
    if "account" in _state:
        return _state["account"]
    account = None
    if not settings.build_isolation_enabled:
        _warn_once("warned", logging.WARNING, "build isolation disabled (BUILD_ISOLATION_ENABLED=false): "
                   "repo build code runs as the worker user with sandbox_env only")
    else:
        try:
            entry = pwd.getpwnam(settings.build_user)
        except KeyError:
            entry = None
        sudo = shutil.which("sudo")
        if entry is None or not sudo:
            _warn_once("warned", logging.WARNING, "build isolation unavailable (user %r %s, sudo %s): "
                       "repo build code runs as the worker user with sandbox_env only", settings.build_user,
                       "found" if entry else "missing", "found" if sudo else "missing")
        elif entry.pw_uid == os.geteuid():
            account = entry  # already the build user: nothing to switch
        else:
            # Fixed argv (resolved sudo path, configured user name, literal `true`); no shell.
            probe = subprocess.run([sudo, "-n", "-u", settings.build_user, "--", "true"],  # nosec B603
                                   capture_output=True, text=True, check=False,
                                   timeout=settings.sandbox_probe_timeout_seconds)
            if probe.returncode == 0:
                account = entry
            else:
                # The image is broken (sudoers missing, no setuid): say so loudly, keep the R5 sandbox.
                _warn_once("warned", logging.ERROR, "build isolation probe failed (sudo -n -u %s true: %s): "
                           "repo build code runs as the worker user with sandbox_env only",
                           settings.build_user, (probe.stderr or "").strip()[:200])
    _state["account"] = account
    return account


def isolated() -> bool:
    return build_account() is not None


def untrusted_python() -> str:
    """An interpreter the build user can execute (the worker's venv lives in atxuser's 700 home)."""
    if isolated():
        return getattr(sys, "_base_executable", "") or sys.executable
    return sys.executable


def _sandbox_path(path: str) -> str:
    """PATH without entries under the worker's home (not reachable for the build user)."""
    home = os.path.expanduser("~").rstrip("/")
    keep = [p for p in path.split(os.pathsep) if p and not (home and (p == home or p.startswith(home + "/")))]
    return os.pathsep.join(keep)


def untrusted_env(env: dict | None = None) -> dict:
    """sandbox_env() with the caller's (already sandboxed) additions; HOME = the build user's when isolated."""
    from backend.src.worker.ci_readiness import _sandbox_drop, sandbox_env

    merged = sandbox_env()
    merged.update({k: v for k, v in (env or {}).items() if not _sandbox_drop(k)})
    account = build_account()
    if account is not None:
        merged["HOME"] = account.pw_dir
        merged["PATH"] = _sandbox_path(merged.get("PATH", ""))
    return merged


def _switches_user() -> bool:
    account = build_account()
    return account is not None and account.pw_uid != os.geteuid()


def wrap(cmd: list[str], env: dict, timeout: int | float | None) -> tuple[list[str], dict]:
    """(argv, env for the sudo process) — the command as the build user, only `env` preserved."""
    if not _switches_user():
        return list(cmd), env
    account = build_account()
    env = {**env, "AMF_SANDBOX_PATH": env.get("PATH", "")}
    inner = list(cmd)
    if timeout:
        inner = [shutil.which("timeout") or "timeout", "-k", str(settings.sandbox_kill_after_seconds),
                 str(int(timeout)), *inner]
    argv = [shutil.which("sudo") or "sudo", "-n", "-u", account.pw_name, f"--preserve-env={','.join(sorted(env))}", "--",
            "/bin/sh", "-c", _WRAPPER, "amf-sandbox", *inner]
    return argv, env


def run_untrusted(cmd: list[str], cwd: str | Path | None = None, timeout: int | float | None = None,
                  env: dict | None = None) -> subprocess.CompletedProcess:
    """Run repo-controlled `cmd` sandboxed (no credentials; as the build user when available).

    Same contract as `subprocess.run(..., capture_output=True, text=True, check=False)`; a timeout
    raises `subprocess.TimeoutExpired` either way.
    """
    run_env = untrusted_env(env)
    argv, run_env = wrap(cmd, run_env, timeout)
    if _switches_user():
        outer = (timeout + settings.sandbox_kill_after_seconds + settings.sandbox_timeout_grace_seconds
                 if timeout else None)
        # List argv, no shell: `cmd` runs as the build user via the fixed sudo/sh wrapper with sandbox_env only;
        # callers pass literal tool names (mvn/gradlew/pip/pytest/ruff) and cwd inside the job workspace.
        res = subprocess.run(argv, cwd=None if cwd is None else str(cwd), env=run_env, timeout=outer,  # nosec B603
                             capture_output=True, text=True, check=False)
        if timeout and res.returncode in (_TIMEOUT_EXIT, 128 + 9):
            raise subprocess.TimeoutExpired(cmd, timeout, output=res.stdout, stderr=res.stderr)
        return subprocess.CompletedProcess(list(cmd), res.returncode, res.stdout, res.stderr)
    # Same contract without a user switch (no build user / sudo): list argv, no shell, sandbox_env only.
    return subprocess.run(argv, cwd=None if cwd is None else str(cwd), env=run_env, timeout=timeout,  # nosec B603
                          capture_output=True, text=True, check=False)


# ── shared work tree ─────────────────────────────────────────────────────────

def _group_writable(path: str, gid: int, mode: int, extra: int = 0) -> None:
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return  # never follow a link planted in the tree
        if st.st_gid != gid:
            os.lchown(path, -1, gid)
        want = stat.S_IMODE(st.st_mode) | mode | extra
        if want != stat.S_IMODE(st.st_mode):
            os.chmod(path, want)
    except OSError as exc:  # a file atxuser doesn't own (the build made it): already shared
        logger.debug("sandbox: %s not shared: %s", path, exc)


def _protect_git(git_dir: str) -> None:
    """`.git` stays writable by atxuser only (no group/other write) — hooks/config run as atxuser."""
    for root, dirs, files in os.walk(git_dir):
        for name in [*dirs, *files, None]:
            p = root if name is None else os.path.join(root, name)
            try:
                st = os.lstat(p)
                if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022:
                    os.chmod(p, stat.S_IMODE(st.st_mode) & ~0o022)
            except OSError:
                continue


def prepare_build_tree(root: str | Path) -> None:
    """Share `root` with the build user: group = its primary group, g+rwX, setgid dirs; `.git` excluded.

    One recursive pass; call it once per clone and before a build/scan step (ATX may have added files).
    No-op without isolation or when `root` doesn't exist.
    """
    account = build_account()
    root = str(root)
    if account is None or not os.path.isdir(root):
        return
    os.umask(0o002)  # what the worker creates from now on stays group-writable
    gid = account.pw_gid
    for cur, dirs, files in os.walk(root):
        if ".git" in dirs or ".git" in files:
            _protect_git(os.path.join(cur, ".git"))
            if ".git" in dirs:
                dirs.remove(".git")
            # sticky: the build can't rename/replace `.git` (or other atxuser-owned entries) here
            _group_writable(cur, gid, 0o070, stat.S_ISGID | stat.S_ISVTX)
        else:
            _group_writable(cur, gid, 0o070, stat.S_ISGID)
        for name in files:
            p = os.path.join(cur, name)
            try:
                is_exec = os.lstat(p).st_mode & 0o100
            except OSError:
                continue
            _group_writable(p, gid, 0o060 | (0o010 if is_exec else 0))


# rwx for atxuser + the build group, setgid so new entries keep the group; nothing for "other".
_SHARED_DIR_MODE = stat.S_ISGID | stat.S_IRWXU | stat.S_IRWXG


def shared_tempdir(prefix: str) -> Path:
    """mkdtemp the build user can work in (group-owned, 2770) — mkdtemp alone is atxuser-only 0700."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    account = build_account()
    if account is not None:
        os.umask(0o002)
        os.lchown(path, -1, account.pw_gid)
        os.chmod(path, _SHARED_DIR_MODE)
    return path

