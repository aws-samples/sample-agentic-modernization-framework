"""CI readiness of an app's repo against the generated CodeBuild gates (spec 14).

The CodePipeline buildspec runs, in BUILD_DIR: `pip install -r requirements.txt`,
`ruff check .`, `coverage run -m pytest` + `coverage report --fail-under`. On 2026-10-21 both apps needed six manual fixes before that
passed (no requirements.txt, uncollected test.py, no tests, 61% coverage, ruff
I001/F841, test deps in the runtime image). `check` finds those up front, in the
worker that already holds the clone, by running the same tools.

Each check is independent: a tool that can't run yields a `fixable`/`blocker`
item with evidence, never an exception. App tests run with AWS credentials
stripped from the environment, so customer code never sees the worker's role.

Python gets the full tool-backed check; Java gets static checks (`_java_items`: build file,
unit tests for the JaCoCo gate, Lambda handler class) — CodeBuild runs the Maven/Gradle build.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess  # nosec B404 — subprocess runs fixed tools with list argv; inputs validated (worker/argsafe.py), untrusted builds sandboxed
from collections.abc import Callable
from pathlib import Path

from backend.src.config import settings
from backend.src.worker.repo_scan import _files, _is_test_path, _language, _norm, _read, third_party_imports
from backend.src.worker.sandbox import prepare_build_tree, run_untrusted, shared_tempdir, untrusted_python

DEFAULT_COVERAGE_THRESHOLD = settings.coverage_threshold_default
_TOOL_TIMEOUT = settings.ci_tool_timeout_seconds
_TEST_DEPS = ("pytest", "moto", "coverage", "pytest-cov", "pytest_cov", "mock", "responses", "faker", "hypothesis")
_EOL_PYTHON = re.compile(r"python:(2\.\d+|3\.[0-8])(?:[.-]|$)")
_DEFAULT_TEST_GLOBS = ("test_*.py", "*_test.py")

# Every subprocess goes through this, so tests can stub tool runs.
Runner = Callable[..., subprocess.CompletedProcess]


def _default_runner(cmd, cwd=None, env=None, timeout=_TOOL_TIMEOUT):
    """Repo code (pip/pytest/ruff) runs as the unprivileged build user."""
    return run_untrusted(cmd, cwd=cwd, timeout=timeout, env=env)


# Never handed to customer code: AWS_* covers the task-role endpoint
# (AWS_CONTAINER_CREDENTIALS_*), keys/session token, AWS_PROFILE and AWS_WEB_IDENTITY_TOKEN_FILE;
# ECS_CONTAINER_METADATA_URI* is the task metadata endpoint; SFN_* carries the callback task token.
_SANDBOX_DROP_PREFIXES = ("AWS_", "SFN_", "ECS_CONTAINER_METADATA_URI", "ECS_AGENT_URI")
_SANDBOX_DROP_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")  # any credential-shaped env var


def _sandbox_drop(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(_SANDBOX_DROP_PREFIXES) or any(m in upper for m in _SANDBOX_DROP_MARKERS)


def sandbox_env(venv: Path | None = None) -> dict:
    """Environment for running customer code: no AWS credentials, no metadata access.

    Every subprocess that executes repo-controlled code (pip/pytest/ruff here and in ci_fix,
    mvn/gradlew in the workbench) must pass `env=sandbox_env()` — via
    `sandbox.run_untrusted`, which also runs it as the unprivileged build user (R9). The AWS keys set below
    are inert fakes (moto-style), so an SDK in the app's tests resolves them instead of
    looking further down the credential chain.
    """
    env = {k: v for k, v in os.environ.items() if not _sandbox_drop(k)}
    env.update({"AWS_EC2_METADATA_DISABLED": "true", "AWS_DEFAULT_REGION": "us-east-1",
                "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",  # noqa: S105  # nosec B105 — fake moto creds for the sandboxed repo tests
                "PYTHONDONTWRITEBYTECODE": "1"})
    if venv:
        env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(venv)
        # User-level tool config (~/.config/ruff) must not change results: CodeBuild has none.
        env["XDG_CONFIG_HOME"] = str(venv.parent / "xdg")
    return env


def _item(check: str, status: str, evidence: str) -> dict:
    return {"check": check, "status": status, "evidence": evidence[:1500]}


def _tail(text: str, n: int = 25) -> str:
    return "\n".join((text or "").strip().splitlines()[-n:])


# ── static checks ───────────────────────────────────────────────────────────

def pytest_config(app_root: Path) -> dict | None:
    """The pytest ini section ({key: value}) and where it lives, else None."""
    for name, section in (("pytest.ini", "pytest"), ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        p = app_root / name
        if p.is_file():
            cp = configparser.ConfigParser()
            try:
                cp.read_string(_read(p))
            except configparser.Error:
                continue
            if cp.has_section(section):
                return {"file": name, **dict(cp.items(section))}
    pp = app_root / "pyproject.toml"
    if pp.is_file() and "[tool.pytest.ini_options]" in _read(pp):
        return {"file": "pyproject.toml"}
    return None


def test_files(app_root: Path) -> list[Path]:
    """Files that look like tests (whatever pytest's config would collect)."""
    out = []
    for f in _files(app_root, (".py",)):
        rel = f.relative_to(app_root).as_posix()
        if _is_test_path(rel) and f.name not in ("conftest.py", "__init__.py"):
            out.append(f)
    return out


def _collected(f: Path, cfg: dict | None) -> bool:
    import fnmatch

    globs = (cfg or {}).get("python_files", "").split() or list(_DEFAULT_TEST_GLOBS)
    return any(fnmatch.fnmatch(f.name, g) for g in globs)


def missing_pythonpath(app_root: Path, tests: list[Path], cfg: dict | None) -> list[str]:
    """Folders (relative to app_root) that test imports need on sys.path.

    `coverage run -m pytest` puts BUILD_DIR (cwd) and each test file's rootdir-less
    folder on sys.path. `from app import app` with app/app.py and an `app/` folder
    at the root resolves to the namespace package (APP-MUSIC) — so the module's
    folder must be on `pythonpath`.
    """
    import ast

    configured = {p.strip().strip("/") for p in (cfg or {}).get("pythonpath", "").split()}
    modules: dict[str, Path] = {}
    for f in _files(app_root, (".py",)):
        rel = f.relative_to(app_root)
        if _is_test_path(rel.as_posix()):
            continue
        modules.setdefault(f.stem, f.parent)
    need: list[str] = []
    for t in tests:
        try:
            tree = ast.parse(_read(t))
        except SyntaxError:
            continue
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
        for n in names:
            home = modules.get(n)
            if home is None or home == app_root or home == t.parent:
                continue
            rel = home.relative_to(app_root).as_posix()
            if rel not in configured and rel not in need:
                need.append(rel)
    return need


def dockerfile_findings(path: Path) -> list[str]:
    """Hygiene problems in a Dockerfile (empty list = fine)."""
    text = _read(path)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    stages: list[dict] = []
    for ln in lines:
        m = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", ln, re.I)
        if m:
            stages.append({"image": m.group(1), "name": (m.group(2) or "").lower(), "lines": []})
        elif stages:
            stages[-1]["lines"].append(ln)
    if not stages:
        return ["no FROM instruction"]
    out = []
    final = stages[-1]
    if _EOL_PYTHON.search(final["image"]):
        out.append(f"EOL base image {final['image']}")

    def installs_test_deps(stage: dict) -> list[str]:
        found = []
        for ln in stage["lines"]:
            if re.match(r"RUN\b.*\bpip\d?\s+install\b", ln, re.I):
                found += [d for d in _TEST_DEPS if re.search(rf"(?<![\w-]){re.escape(d)}(?![\w-])", ln)]
        return found

    leaked = installs_test_deps(final)
    by_name = {s["name"]: s for s in stages if s["name"]}
    for ln in final["lines"]:
        m = re.match(r"COPY\s+--from=(\S+)\s+(\S+)", ln, re.I)
        if m and re.search(r"site-packages|\.local|/usr/local/lib", m.group(2)):
            src = by_name.get(m.group(1).lower())
            if src:
                leaked += installs_test_deps(src)
    if leaked:
        out.append("runtime image carries test-only deps: " + ", ".join(sorted(set(leaked))))
    if not any(re.match(r"USER\s+(?!root\b|0\b)\S+", ln, re.I) for ln in final["lines"]):
        out.append("runs as root (no non-root USER in the final stage)")
    return out


# ── tool-backed checks ──────────────────────────────────────────────────────

class _Tools:
    """An isolated venv with the app's deps + the gate tools (created once per check)."""

    def __init__(self, app_root: Path, runner: Runner):
        self.app_root, self.run = app_root, runner
        prepare_build_tree(app_root)  # the build user writes .coverage/caches in place (R9)
        self.dir = shared_tempdir("amf-ci-")
        self.venv = self.dir / "venv"
        self.error = ""
        self.deps_error = ""
        r = runner([untrusted_python(), "-m", "venv", str(self.venv)], env=sandbox_env())
        if r.returncode != 0:
            self.error = f"venv creation failed: {_tail(r.stderr or r.stdout, 5)}"
            return
        pip = [str(self.venv / "bin" / "python"), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
        # boto3 mirrors CodeBuild's Python image and the Lambda runtime, which both
        # ship it (APP-MOVIES's handler imported boto3 with no requirements.txt).
        r = runner(pip + ["pytest", "coverage", "ruff", "boto3"], env=sandbox_env())
        if r.returncode != 0:
            self.error = f"installing pytest/coverage/ruff failed: {_tail(r.stderr or r.stdout, 8)}"
            return
        req = app_root / "requirements.txt"
        if req.is_file():
            r = runner(pip + ["-r", str(req)], cwd=str(app_root), env=sandbox_env())
            if r.returncode != 0:
                self.deps_error = f"pip install -r requirements.txt failed: {_tail(r.stderr or r.stdout, 8)}"

    def bin(self, name: str) -> str:
        return str(self.venv / "bin" / name)


def _lint(tools: _Tools) -> tuple[dict, int]:
    r = tools.run([tools.bin("ruff"), "check", "--output-format", "json", "."], cwd=str(tools.app_root),
                  env=sandbox_env(tools.venv))
    try:
        findings = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return _item("lint", "fixable", f"ruff did not run: {_tail(r.stderr or r.stdout, 5)}"), 0
    if not findings:
        return _item("lint", "ok", "ruff check: no findings"), 0
    safe = [f for f in findings if (f.get("fix") or {}).get("applicability") == "safe"]
    codes = sorted({f.get("code") or "?" for f in findings})
    sample = "; ".join(f"{f.get('code')} {Path(f.get('filename', '')).name}:{(f.get('location') or {}).get('row')}"
                       for f in findings[:8])
    status = "fixable" if len(safe) == len(findings) else "blocker"
    return _item("lint", status, f"{len(findings)} finding(s) ({', '.join(codes)}), {len(safe)} safe-fixable: "
                                 f"{sample}"), len(findings)


def _tests_and_coverage(tools: _Tools, threshold: int) -> tuple[list[dict], float | None]:
    env = sandbox_env(tools.venv)
    r = tools.run([tools.bin("coverage"), "run", "-m", "pytest", "-q"], cwd=str(tools.app_root), env=env)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 5:
        return [_item("tests_pass", "fixable", "pytest collected no tests (exit 5)")], None
    if r.returncode != 0:
        return [_item("tests_pass", "blocker", f"pytest exit {r.returncode}:\n{_tail(out)}")], None
    items = [_item("tests_pass", "ok", _tail(out, 3))]
    rep = tools.run([tools.bin("coverage"), "report"], cwd=str(tools.app_root), env=env)
    m = re.search(r"^TOTAL\s+.*?(\d+(?:\.\d+)?)%\s*$", rep.stdout or "", re.M)
    if not m:
        items.append(_item("coverage", "fixable", f"coverage report unreadable: {_tail(rep.stdout or rep.stderr, 3)}"))
        return items, None
    pct = float(m.group(1))
    items.append(_item("coverage", "ok" if pct >= threshold else "fixable",
                       f"line coverage {pct:.0f}% (threshold {threshold}%)"))
    return items, pct


# ── public ──────────────────────────────────────────────────────────────────

# Handler classes that come from a dependency (not the app's sources).
_JAVA_LIBRARY_HANDLERS = ("org.springframework.cloud.function.adapter.aws.", "com.amazonaws.serverless.proxy.",
                          "io.micronaut.function.aws.", "io.quarkus.amazon.lambda.")


def _java_items(repo: Path, app_root: Path, wiring: dict, threshold: int,
                generate_dockerfile: bool = False) -> tuple[list[dict], int]:
    """Static Java checks against the CodeBuild gates (the build itself runs in CodeBuild)."""
    items = []
    build = next((n for n in ("pom.xml", "build.gradle", "build.gradle.kts") if (app_root / n).is_file()), "")
    items.append(_item("build_file", "ok" if build else "blocker",
                       f"{build} in {_rel_or(app_root, repo)}" if build else f"no pom.xml / build.gradle in {_rel_or(app_root, repo)}"))
    tests = [p for p in _files(app_root, (".java", ".kt")) if "/src/test/" in p.as_posix()]
    if tests:
        items.append(_item("tests_found", "ok", f"{len(tests)} test source file(s) under src/test"))
    elif threshold > 0:
        items.append(_item("tests_found", "blocker",
                           f"no unit tests under src/test: the JaCoCo gate ({threshold}%) fails — add tests, or set "
                           "coverage_threshold=0 with coverage_threshold_reason on the app"))
    else:
        items.append(_item("tests_found", "fixable", "no unit tests (coverage gate disabled for this app)"))
    docker = app_root / "Dockerfile"
    if docker.is_file():
        found = _java_dockerfile_findings(_read(docker), str(wiring.get("runtime_version") or ""))
        if found and generate_dockerfile:
            # Execution replaces the repo Dockerfile with AMF's (tool_execution.generate_cicd).
            items.append(_item("dockerfile", "ok", "repo Dockerfile is replaced by AMF's (generate_dockerfile=true): "
                               + "; ".join(found)))
        else:
            items.append(_item("dockerfile", "blocker" if found else "ok",
                               "; ".join(found) + " — fix it, or set generate_dockerfile=true on the app" if found
                               else "no JVM flags beyond the runtime's JDK"))
    handler = str(wiring.get("lambda_handler") or "")
    if handler:
        cls = handler.split("::", 1)[0]
        if cls.startswith(_JAVA_LIBRARY_HANDLERS):
            dep = cls.rsplit(".", 1)[0]
            pom = _read(app_root / build) if build else ""
            known = any(a in pom for a in ("spring-cloud-function-adapter-aws", "aws-serverless-java-container",
                                           "micronaut-function-aws", "quarkus-amazon-lambda"))
            items.append(_item("lambda_package", "ok" if known else "fixable",
                               f"handler {cls} comes from a dependency ({dep})"
                               + ("" if known else " not declared in the build file")))
        else:
            src = cls.replace(".", "/")
            found = any(p.as_posix().endswith(f"/{src}.java") or p.as_posix().endswith(f"/{src}.kt")
                        for p in _files(app_root, (".java", ".kt")))
            items.append(_item("lambda_package", "ok" if found else "blocker",
                               f"handler class {cls} {'found' if found else 'not found'} in {_rel_or(app_root, repo)}"))
    return items, len(tests)


# JVM options that exist only from a JDK version on (without an unlock flag the JVM won't start).
_JVM_FLAG_MIN_JDK = {"UseCompactObjectHeaders": 25}  # JEP 519 (experimental in 24)


def _java_dockerfile_findings(text: str, runtime_version: str) -> list[str]:
    """JVM flags in the Dockerfile the image's JDK doesn't know (the container exits at start)."""
    m = re.findall(r"(?:corretto|openjdk|temurin|jdk|jre)[-:]?(\d{1,2})\b", text, re.I)
    jdk = int(m[-1]) if m else (int(re.match(r"\d+", runtime_version).group()) if re.match(r"\d+", runtime_version) else 0)
    out = []
    for flag, since in _JVM_FLAG_MIN_JDK.items():
        if re.search(rf"-XX:\+{flag}\b", text) and jdk and jdk < since and "UnlockExperimentalVMOptions" not in text:
            out.append(f"-XX:+{flag} needs JDK {since}+ but the image runs JDK {jdk}: the JVM won't start")
    return out


def _rel_or(p: Path, repo: Path) -> str:
    return (p.relative_to(repo).as_posix() or ".") if p.is_relative_to(repo) else str(p)


def check(repo_dir, build_dir, wiring: dict, *, threshold: int | None = None, run_tools: bool = True,
          runner: Runner | None = None, generate_dockerfile: bool = False) -> dict:
    """CI-readiness report for the app in repo_dir/build_dir (see module docstring)."""
    repo = Path(repo_dir)
    app_root = repo / _norm(build_dir) if _norm(build_dir) else repo
    threshold = DEFAULT_COVERAGE_THRESHOLD if threshold is None else int(threshold)
    runner = runner or _default_runner
    # Unknown language → detect it; never assume Python (a Java Lambda's handler
    # was checked as a .py module path — APP-CLI 2026-10-23).
    lang = (wiring or {}).get("language") or _language(app_root) or "unknown"
    items: list[dict] = []
    tests_found, coverage_pct, lint_findings = 0, None, 0

    if lang == "java":
        items, tests_found = _java_items(repo, app_root, wiring or {}, threshold, generate_dockerfile)
        statuses = {i["status"] for i in items}
        status = "blocked" if "blocker" in statuses else ("fixable" if "fixable" in statuses else "ready")
        return {"status": status, "items": items, "tests_found": tests_found, "coverage_pct": None,
                "lint_findings": 0, "build_dir": _norm(build_dir) or "."}
    if lang != "python":
        items.append(_item("language", "fixable", f"CI-readiness checks are not implemented for {lang} yet"))
        return {"status": "fixable", "items": items, "tests_found": 0, "coverage_pct": None, "lint_findings": 0,
                "build_dir": _norm(build_dir) or "."}

    # dependency manifest
    third = set(third_party_imports(app_root)) if app_root.is_dir() else set()
    if (wiring or {}).get("lambda_handler") and not (wiring or {}).get("framework"):
        third -= {"boto3", "botocore"}  # provided by the Lambda runtime
    third = sorted(third)
    if (app_root / "requirements.txt").is_file():
        items.append(_item("dependency_manifest", "ok", "requirements.txt present"))
    elif third:
        has_pyproject = (app_root / "pyproject.toml").is_file()
        items.append(_item("dependency_manifest", "fixable",
                           ("pyproject.toml only — the buildspec installs requirements.txt; " if has_pyproject else
                            "no requirements.txt; ") + "imports: " + ", ".join(third)))
    else:
        items.append(_item("dependency_manifest", "ok", "no third-party imports"))

    # tests + runner config
    tests = test_files(app_root) if app_root.is_dir() else []
    tests_found = len(tests)
    cfg = pytest_config(app_root) if app_root.is_dir() else None
    if not tests:
        items.append(_item("tests_found", "fixable", "no test files under " + (_norm(build_dir) or ".")))
    else:
        items.append(_item("tests_found", "ok", f"{len(tests)} test file(s)"))
        uncollected = [t.relative_to(app_root).as_posix() for t in tests if not _collected(t, cfg)]
        paths = missing_pythonpath(app_root, tests, cfg)
        problems = []
        if uncollected:
            problems.append("not collected by pytest's python_files: " + ", ".join(uncollected))
        if paths:
            problems.append("imports need pythonpath: " + ", ".join(paths))
        items.append(_item("test_runner_config", "fixable" if problems else "ok",
                           "; ".join(problems) or f"pytest config: {(cfg or {}).get('file', 'defaults')}"))

    # Dockerfile hygiene (container apps)
    docker = next((d / "Dockerfile" for d in (app_root, repo) if (d / "Dockerfile").is_file()), None)
    if docker:
        found = dockerfile_findings(docker)
        items.append(_item("dockerfile", "fixable" if found else "ok",
                           "; ".join(found) or "multi-stage-safe, non-root, supported base"))
    elif (wiring or {}).get("container_port") or (wiring or {}).get("framework"):
        items.append(_item("dockerfile", "fixable", "web app without a Dockerfile"))

    # Lambda package: the handler module must exist in the package root.
    handler = (wiring or {}).get("lambda_handler") or ""
    if handler and "." in handler:
        module, func = handler.rsplit(".", 1)
        mod_path = app_root / (module.replace(".", "/") + ".py")
        ok = mod_path.is_file() and re.search(rf"^def\s+{re.escape(func)}\s*\(", _read(mod_path), re.M)
        items.append(_item("lambda_package", "ok" if ok else "blocker",
                           f"handler {handler} {'found in' if ok else 'not found in'} "
                           f"{mod_path.relative_to(repo).as_posix() if mod_path.is_relative_to(repo) else mod_path}"))

    # tool-backed checks
    if run_tools and app_root.is_dir():
        try:
            tools = _Tools(app_root, runner)
            if tools.error:
                items.append(_item("toolchain", "blocker", tools.error))
            else:
                if tools.deps_error:
                    items.append(_item("dependency_install", "blocker", tools.deps_error))
                lint_item, lint_findings = _lint(tools)
                items.append(lint_item)
                if tests and not tools.deps_error:
                    test_items, coverage_pct = _tests_and_coverage(tools, threshold)
                    items += test_items
        except subprocess.TimeoutExpired as exc:
            items.append(_item("toolchain", "blocker", f"tool run timed out: {exc.cmd}"))
        except Exception as exc:  # noqa: BLE001 — a broken check must not abort the report
            items.append(_item("toolchain", "fixable", f"tool checks could not run: {exc}"))

    statuses = {i["status"] for i in items}
    status = "blocked" if "blocker" in statuses else ("fixable" if "fixable" in statuses else "ready")
    return {"status": status, "items": items, "tests_found": tests_found, "coverage_pct": coverage_pct,
            "lint_findings": lint_findings, "build_dir": _norm(build_dir) or ".", "threshold": threshold}
