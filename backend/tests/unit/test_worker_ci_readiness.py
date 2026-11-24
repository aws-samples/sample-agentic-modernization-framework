"""Wiring detection + CI readiness + ci-fix.

Fixtures replicate the two 2026-10-21 apps: APP-MUSIC (Flask on a root Dockerfile,
app/app.py, DynamoDB musicTable keyed on artist) and APP-MOVIES (a CDK project in
the aws-cdk-examples monorepo whose Lambda asset is lambda/apigw-handler).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.src.worker import ci_fix, ci_readiness, code_analysis
from backend.src.worker.ci_readiness import check, dockerfile_findings
from backend.src.worker.repo_scan import scan_wiring

CDK_APP = "python/apigw-http-api-lambda-dynamodb-python-cdk"
HANDLER_DIR = f"{CDK_APP}/lambda/apigw-handler"

FLASK_DOCKERFILE = """FROM public.ecr.aws/docker/library/python:3.8-slim as builder
WORKDIR app
COPY ./app .
RUN pip install --user fastapi uvicorn boto3 flask pytest moto
RUN python -m pytest ./tests/test.py

FROM public.ecr.aws/docker/library/python:3.8-slim as app
WORKDIR app
COPY --from=builder /root/.local /root/.local
COPY --from=builder ./app .
ENV PATH=/root/.local:$PATH
EXPOSE 5000
CMD ["python", "./app.py"]
"""
FLASK_APP = """import os

import boto3
from flask import Flask, jsonify, request

app = Flask(__name__)

client = boto3.client('dynamodb', region_name='us-east-1')
dynamoTableName = os.environ.get('DYNAMODB_TABLE', 'musicTable')


@app.route("/")
def hello():
    return 'Hello World!'


@app.route("/v1/bestmusic/90s/<string:artist>")
def get_artist(artist):
    resp = client.get_item(TableName=dynamoTableName, Key={'artist': {'S': artist}})
    return jsonify(resp.get('Item') or {})


@app.route("/v1/bestmusic/90s", methods=["POST"])
def create_artist():
    resp = client.put_item(TableName=dynamoTableName, Item={'artist': {'S': request.json['artist']}})
    return jsonify({})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
"""
CDK_STACK = """from aws_cdk import aws_dynamodb as dynamodb_, aws_lambda as lambda_
TABLE_NAME = "demo_table"


class Stack:
    def __init__(self):
        demo_table = dynamodb_.Table(
            self, TABLE_NAME,
            partition_key=dynamodb_.Attribute(name="id", type=dynamodb_.AttributeType.STRING),
        )
        fn = lambda_.Function(
            self, "ApiHandler",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("lambda/apigw-handler"),
            handler="index.handler",
        )
        fn.add_environment("TABLE_NAME", demo_table.table_name)
"""
HANDLER = """import json
import os

import boto3

dynamodb_client = boto3.client("dynamodb")


def handler(event, context):
    table = os.environ.get("TABLE_NAME")
    if event["body"]:
        item = json.loads(event["body"])
        dynamodb_client.put_item(TableName=table, Item={"id": {"S": str(item["id"])}})
    return {"statusCode": 200, "body": json.dumps({"message": "ok"})}
"""
SAM_TEMPLATE = """AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Globals:
  Function:
    CodeUri: target/app-aws.jar
    Handler: org.springframework.cloud.function.adapter.aws.FunctionInvoker::handleRequest
    Runtime: java17
    MemorySize: 1024
    Environment:
      Variables:
        PRODUCT_TABLE_NAME: !Ref ProductsTable
Resources:
  GetProductsFunction:
    Type: AWS::Serverless::Function
    Properties:
      Environment:
        Variables:
          SPRING_CLOUD_FUNCTION_DEFINITION: getAllProductsFunction
      Events:
        GetRequest:
          Type: Api
          Properties:
            Path: /products
            Method: get
  PutProductFunction:
    Type: AWS::Serverless::Function
    Properties:
      Events:
        PutRequest:
          Type: Api
          Properties:
            Path: /products/{id}
            Method: put
  ProductsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      KeySchema:
        - AttributeName: PK
          KeyType: HASH
"""


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def flask_repo(tmp_path):
    return _write(tmp_path / "flask", {
        "Dockerfile": FLASK_DOCKERFILE, "app/app.py": FLASK_APP,
        "app/tests/test.py": "from app import app\n\n\ndef test_base_route():\n"
                             "    assert app.test_client().get('/').status_code == 200\n",
    })


@pytest.fixture
def cdk_repo(tmp_path):
    return _write(tmp_path / "cdk", {
        f"{CDK_APP}/cdk.json": '{"app": "python3 app.py"}', f"{CDK_APP}/requirements.txt": "aws-cdk-lib\n",
        f"{CDK_APP}/app.py": "import aws_cdk\n", f"{CDK_APP}/stacks/__init__.py": "",
        f"{CDK_APP}/stacks/stack.py": CDK_STACK, f"{HANDLER_DIR}/index.py": HANDLER,
        "python/other-example/cdk.json": "{}",
    })


# ── scan_wiring ───────────────────────────────────────────────────────────────

def test_flask_wiring(flask_repo):
    w = scan_wiring(flask_repo, "")
    assert w["language"] == "python" and w["runtime_version"] == "3.8"
    assert w["container_port"] == 5000 and w["health_check_path"] == "/"
    assert w["dynamodb_tables"] == [{"name": "musicTable", "env_var": "DYNAMODB_TABLE", "hash_key": "artist"}]
    assert w["web_app"] == {"module": "app/app.py", "var": "app"} and "lambda_handler" not in w
    assert w["sources"]["container_port"] == "Dockerfile:12"


@pytest.mark.parametrize("given", [CDK_APP, HANDLER_DIR, ""])
def test_cdk_lambda_wiring_from_project_handler_dir_or_root(cdk_repo, given):
    w = scan_wiring(cdk_repo, given)
    assert w["build_dir"] == HANDLER_DIR
    assert w["lambda_handler"] == "index.handler"
    assert w["dynamodb_tables"] == [{"hash_key": "id", "env_var": "TABLE_NAME"}]
    assert w["runtime_version"] == "3.9"


def test_sam_template_functions_routes_env_and_key(tmp_path):
    repo = _write(tmp_path / "sam", {"springboot/template.yaml": SAM_TEMPLATE, "springboot/pom.xml": "<project/>"})
    w = scan_wiring(repo, "springboot")
    fns = {f["name"]: f for f in w["lambda_functions"]}
    assert fns["GetProductsFunction"]["routes"] == ["GET /products"]
    assert fns["PutProductFunction"]["routes"] == ["PUT /products/{id}"]
    assert fns["GetProductsFunction"]["handler"].endswith("FunctionInvoker::handleRequest")
    assert fns["GetProductsFunction"]["env"]["SPRING_CLOUD_FUNCTION_DEFINITION"] == "getAllProductsFunction"
    assert fns["PutProductFunction"]["env"]["PRODUCT_TABLE_NAME"] == "${Ref:ProductsTable}"
    assert fns["GetProductsFunction"]["memory"] == 1024
    assert w["dynamodb_tables"] == [{"hash_key": "PK", "env_var": "PRODUCT_TABLE_NAME"}]
    assert w["language"] == "java"


# ── check() (static) ──────────────────────────────────────────────────────────

def _statuses(report):
    return {i["check"]: i["status"] for i in report["items"]}


def test_flask_readiness_static_findings(flask_repo):
    rep = check(flask_repo, "", scan_wiring(flask_repo, ""), run_tools=False)
    st = _statuses(rep)
    assert st["dependency_manifest"] == "fixable" and st["test_runner_config"] == "fixable"
    assert st["dockerfile"] == "fixable" and rep["status"] == "fixable" and rep["tests_found"] == 1
    cfg = next(i for i in rep["items"] if i["check"] == "test_runner_config")["evidence"]
    assert "app/tests/test.py" in cfg and "pythonpath: app" in cfg


def test_dockerfile_findings_follow_copy_from_site_packages(flask_repo):
    found = " | ".join(dockerfile_findings(flask_repo / "Dockerfile"))
    assert "EOL base image" in found and "moto" in found and "pytest" in found and "root" in found


def test_lambda_readiness_needs_tests_not_boto3(cdk_repo):
    w = scan_wiring(cdk_repo, HANDLER_DIR)
    st = _statuses(check(cdk_repo, HANDLER_DIR, w, run_tools=False))
    assert st["dependency_manifest"] == "ok"  # boto3 comes with the Lambda runtime
    assert st["tests_found"] == "fixable" and st["lambda_package"] == "ok"


def _runner(results: dict):
    """Stub runner: maps a command marker to (returncode, stdout)."""
    calls = []

    def run(cmd, cwd=None, env=None, timeout=None):
        calls.append({"cmd": cmd, "env": env})
        joined = " ".join(map(str, cmd))
        for marker, (rc, out) in results.items():
            if marker in joined:
                return subprocess.CompletedProcess(cmd, rc, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    run.calls = calls
    return run


def test_tool_checks_with_stubbed_tools(flask_repo):
    lint = json.dumps([{"code": "I001", "filename": "app.py", "location": {"row": 1}, "fix": {"applicability": "safe"}}])
    run = _runner({"ruff check": (1, lint), "coverage run": (0, "1 passed"),
                   "coverage report": (0, "Name  Stmts\nTOTAL   20   8   61%\n")})
    rep = check(flask_repo, "", scan_wiring(flask_repo, ""), runner=run)
    st = _statuses(rep)
    assert st["lint"] == "fixable" and rep["lint_findings"] == 1
    assert st["tests_pass"] == "ok" and st["coverage"] == "fixable" and rep["coverage_pct"] == 61.0
    # customer tests never see real AWS credentials
    test_env = next(c["env"] for c in run.calls if "coverage" in " ".join(map(str, c["cmd"])))
    assert test_env["AWS_EC2_METADATA_DISABLED"] == "true" and test_env["AWS_ACCESS_KEY_ID"] == "testing"
    assert not any(k in test_env for k in ("AWS_SESSION_TOKEN", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"))


def test_failing_tests_and_broken_toolchain_are_items_not_crashes(flask_repo):
    rep = check(flask_repo, "", {}, runner=_runner({"coverage run": (1, "FAILED tests/x.py::t - boom")}))
    assert _statuses(rep)["tests_pass"] == "blocker" and rep["status"] == "blocked"
    rep = check(flask_repo, "", {}, runner=_runner({"-m venv": (1, "no venv")}))
    assert _statuses(rep)["toolchain"] == "blocker"


def test_sandbox_env_strips_credentials(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "real")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/creds")
    env = ci_readiness.sandbox_env()
    assert env["AWS_SECRET_ACCESS_KEY"] == "testing" and "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in env  # pragma: allowlist secret — test fixture, not a credential


# ── fixer pieces ──────────────────────────────────────────────────────────────

def test_requirements_from_imports(flask_repo, cdk_repo):
    w = scan_wiring(flask_repo, "")
    text = ci_fix.generate_requirements(flask_repo, flask_repo, w)
    assert [ln for ln in text.splitlines() if not ln.startswith("#")] == ["boto3", "flask"]
    lam = cdk_repo / HANDLER_DIR
    assert ci_fix.generate_requirements(lam, cdk_repo, scan_wiring(cdk_repo, HANDLER_DIR)) is None  # boto3 only


def test_pytest_ini_collects_test_py_with_pythonpath(flask_repo):
    note = ci_fix.write_pytest_ini(flask_repo)
    ini = (flask_repo / "pytest.ini").read_text()
    assert "pythonpath = app" in ini and "test.py" in ini and note.startswith("pytest.ini:")


def test_dockerfile_rewrite_keeps_semantics_and_drops_test_deps(flask_repo):
    (flask_repo / "requirements.txt").write_text("flask\nboto3\n")
    text, note = ci_fix.rewrite_dockerfile(flask_repo)
    runtime = text.split("AS app", 1)[1]
    assert "python:3.12-slim AS test" in text and "python -m pytest -q" in text
    assert "WORKDIR /app" in runtime and "COPY ./app ." in runtime and 'CMD ["python", "./app.py"]' in runtime
    assert "EXPOSE 5000" in runtime and "USER appuser" in runtime
    assert "moto" not in runtime and ".local" not in runtime
    assert "COPY --from=test /tmp/.amf-tests-passed" in runtime  # forces the test stage to build
    assert dockerfile_findings_text(text) == []


def dockerfile_findings_text(text: str) -> list[str]:
    import tempfile

    p = Path(tempfile.mkdtemp()) / "Dockerfile"
    p.write_text(text)
    return dockerfile_findings(p)


def test_dockerfile_rewrite_skips_unrecognised(tmp_path):
    _write(tmp_path, {"Dockerfile": "FROM node:20\nRUN npm ci \\\n  && npm test\nCMD node x\n", "requirements.txt": "x\n"})
    text, note = ci_fix.rewrite_dockerfile(tmp_path)
    assert text is None and "left as is" in note


def test_env_default_quoting(flask_repo):
    assert ci_fix.fix_env_defaults(flask_repo / "app") == 1
    assert "os.environ.get('PORT', '5000')" in (flask_repo / "app/app.py").read_text()


def test_scaffold_and_prune_lambda_smoke(cdk_repo):
    w = scan_wiring(cdk_repo, HANDLER_DIR)
    smoke = ci_fix.scaffold_tests(cdk_repo, cdk_repo / HANDLER_DIR, w)
    text = smoke.read_text()
    assert "amf_smoke_handler" in text and "'index.py'" in text and "def test_empty_event" in text
    compile(text, "smoke", "exec")
    assert ci_fix.prune_failing_smoke_tests(smoke, ["tests/test_amf_smoke.py::test_empty_event"]) == ["test_empty_event"]
    assert "test_empty_event" not in smoke.read_text() and "def test_http_api_get_event" in smoke.read_text()
    ci_fix.prune_failing_smoke_tests(smoke, ["tests/test_amf_smoke.py::test_http_api_get_event"])
    assert not smoke.exists()


def test_scaffold_web_smoke_imports_by_file(flask_repo):
    smoke = ci_fix.scaffold_tests(flask_repo, flask_repo, scan_wiring(flask_repo, ""))
    text = smoke.read_text()
    assert "'../app/app.py'" in text and "HEALTH = '/'" in text and "APP = getattr(_module, 'app')" in text
    compile(text, "smoke", "exec")


def test_apply_fixes_flask_end_to_end_with_stubbed_tools(flask_repo):
    w = scan_wiring(flask_repo, "")
    run = _runner({"coverage report": (0, "TOTAL  30  5  83%\n")})
    before = check(flask_repo, "", w, run_tools=False)
    changes, warnings, after = ci_fix.apply_fixes(flask_repo, "", w, before, threshold=70, runner=run)
    joined = " | ".join(changes)
    assert "requirements.txt generated" in joined and "pytest.ini" in joined and "Dockerfile: multi-stage" in joined
    assert (flask_repo / "tests" / "test_amf_smoke.py").exists() is False  # coverage 83% ≥ 70: no scaffold
    assert after["status"] == "ready", after


# ── git + records ─────────────────────────────────────────────────────────────

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_commit_and_push(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "migration/app-x")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", str(remote))
    assert ci_fix.commit_and_push(repo, "migration/app-x", []) == "no_change"
    (repo / "requirements.txt").write_text("flask\n")
    assert ci_fix.commit_and_push(repo, "migration/app-x", ["requirements.txt generated"]) == "committed"
    log = subprocess.run(["git", "log", "-1", "--format=%B", "migration/app-x"], cwd=remote,
                         capture_output=True, text=True).stdout
    assert log.startswith("chore(ci): AMF CI-readiness fixes") and "- requirements.txt generated" in log


def test_record_ci_readiness_persists_detected_fields(flask_repo, monkeypatch):
    from backend.src.worker import dynamo

    meta_calls, records = [], []
    monkeypatch.setattr(dynamo, "update_meta_fields", lambda app_id, s, remove_fields=(), set_if_absent=None:
                        meta_calls.append((s, list(remove_fields))))
    monkeypatch.setattr(dynamo, "put_result_record", lambda app_id, t, r, status="completed": records.append((t, r)))
    monkeypatch.setattr(ci_readiness, "_Tools", lambda *a, **k: SimpleNamespace(error="offline", deps_error=""))
    app = {"app_id": "APP-MUSIC", "repos": [{"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/flask"}]}
    report = code_analysis.record_ci_readiness(app, workspace=flask_repo.parent)
    set_fields, removed = meta_calls[0]
    assert set_fields["detected_container_port"] == 5000 and set_fields["detected_health_check_path"] == "/"
    assert set_fields["detected_dynamodb_tables"][0]["hash_key"] == "artist"
    assert set_fields["ci_readiness_status"] == report["status"] and "detected_lambda_handler" in removed
    assert "container_port" not in set_fields  # human fields untouched
    assert records[0][0] == "ci-readiness" and set(records[0][1]) == {"wiring", "readiness"}


def test_record_ci_readiness_never_raises(tmp_path, monkeypatch):
    from backend.src.worker import dynamo

    monkeypatch.setattr(dynamo, "update_meta_fields", lambda *a, **k: None)
    monkeypatch.setattr(dynamo, "put_result_record", lambda *a, **k: None)
    assert code_analysis.record_ci_readiness({"app_id": "X", "repos": []}, workspace=tmp_path)["status"] == "error"


def test_run_ci_fix_without_repo_calls_back_success_with_error(monkeypatch):
    from backend.src.worker import dynamo, sfn_callback

    sent = {}
    monkeypatch.setattr(ci_fix, "read_full_app", lambda app_id: {"app_id": app_id, "repos": []})
    monkeypatch.setattr(sfn_callback, "resolve_token", lambda job: "tok")
    monkeypatch.setattr(sfn_callback, "finalize_job_once", lambda *a, **k: True)
    monkeypatch.setattr(sfn_callback, "send_success", lambda tok, payload: sent.update(payload))
    monkeypatch.setattr(sfn_callback, "send_failure", lambda *a: sent.update({"failed": True}))
    monkeypatch.setattr(dynamo, "update_meta_fields", lambda *a, **k: None)
    monkeypatch.setattr(dynamo, "put_result_record", lambda *a, **k: None)
    ci_fix.run_ci_fix()
    assert sent["status"] == "error" and "no linked repository" in sent["message"] and "failed" not in sent


@pytest.mark.skipif(not os.environ.get("AMF_NET_TESTS"), reason="set AMF_NET_TESTS=1 (pip install from PyPI)")
def test_real_tools_bring_flask_to_ready(flask_repo):
    w = scan_wiring(flask_repo, "")
    before = check(flask_repo, "", w)
    changes, warnings, after = ci_fix.apply_fixes(flask_repo, "", w, before, threshold=70)
    assert before["status"] == "blocked" and after["status"] == "ready", (changes, warnings, after)


def test_commit_excludes_check_artifacts(tmp_path, monkeypatch):
    """APP-MUSIC-R3 (2026-10-22): the fixer committed .coverage and __pycache__/*.pyc left by its own checks."""
    import subprocess

    from backend.src.worker import ci_fix

    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    run("add", ".")
    run("commit", "-qm", "base")
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / ".coverage").write_text("x")
    (tmp_path / "coverage.xml").write_text("x")
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)
    (tmp_path / "app" / "__pycache__" / "m.cpython-312.pyc").write_text("x")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "v").write_text("x")

    real_git = ci_fix._git
    monkeypatch.setattr(ci_fix, "_git", lambda repo, *args: subprocess.CompletedProcess(args, 0, "", "")
                        if args[:1] == ("push",) else real_git(repo, *args))
    assert ci_fix.commit_and_push(tmp_path, "main", ["requirements"]) == "committed"
    committed = subprocess.run(["git", "show", "--name-only", "--format="], cwd=tmp_path,
                               capture_output=True, text=True).stdout.split()
    assert committed == ["requirements.txt"]


# ── Java (2026-10-23: a Java Lambda handler was checked as a Python module path) ──
def _java_app(root: Path, tests: bool, handler_src: str = "") -> Path:
    app = root / "svc"
    (app / "src/main/java/com/x").mkdir(parents=True)
    (app / "pom.xml").write_text("<project><artifactId>spring-cloud-function-adapter-aws</artifactId></project>")
    if tests:
        (app / "src/test/java/com/x").mkdir(parents=True)
        (app / "src/test/java/com/x/AppTest.java").write_text("class AppTest {}")
    if handler_src:
        (app / f"src/main/java/{handler_src}.java").write_text("class H {}")
    return root


def test_java_without_tests_blocks_the_coverage_gate(tmp_path):
    r = check(_java_app(tmp_path, tests=False), "svc", {"language": "java"}, run_tools=False)
    assert r["status"] == "blocked"
    assert {i["check"]: i["status"] for i in r["items"]} == {"build_file": "ok", "tests_found": "blocker"}


def test_java_coverage_opt_out_downgrades_missing_tests(tmp_path):
    r = check(_java_app(tmp_path, tests=False), "svc", {"language": "java"}, threshold=0, run_tools=False)
    assert r["status"] == "fixable"


def test_java_library_handler_is_not_a_python_module(tmp_path):
    w = {"language": "java", "lambda_handler": "org.springframework.cloud.function.adapter.aws.FunctionInvoker::handleRequest"}
    r = check(_java_app(tmp_path, tests=True), "svc", w, run_tools=False)
    assert r["status"] == "ready" and r["tests_found"] == 1
    assert next(i for i in r["items"] if i["check"] == "lambda_package")["status"] == "ok"


def test_java_app_handler_class_must_exist(tmp_path):
    repo = _java_app(tmp_path, tests=True, handler_src="com/x/Handler")
    ok = check(repo, "svc", {"language": "java", "lambda_handler": "com.x.Handler::handleRequest"}, run_tools=False)
    bad = check(repo, "svc", {"language": "java", "lambda_handler": "com.x.Missing::handleRequest"}, run_tools=False)
    assert ok["status"] == "ready" and bad["status"] == "blocked"


def test_unknown_language_is_detected_not_assumed_python(tmp_path):
    r = check(_java_app(tmp_path, tests=True), "svc", {}, run_tools=False)
    assert all(i["check"] != "dependency_manifest" for i in r["items"]) and r["status"] == "ready"


def test_java_dockerfile_with_a_newer_jdk_flag_blocks(tmp_path):
    """APP-HE: -XX:+UseCompactObjectHeaders on a Corretto 21 image — the container exits at start."""
    repo = _java_app(tmp_path, tests=True)
    (repo / "svc/Dockerfile").write_text(
        "FROM public.ecr.aws/docker/library/maven:3-amazoncorretto-21-al2023 AS builder\n"
        'ENTRYPOINT ["java", "-XX:+UseCompactObjectHeaders", "-jar", "/app.jar"]\n')
    r = check(repo, "svc", {"language": "java"}, run_tools=False)
    item = next(i for i in r["items"] if i["check"] == "dockerfile")
    assert r["status"] == "blocked" and "JDK 25+" in item["evidence"] and "generate_dockerfile" in item["evidence"]
    (repo / "svc/Dockerfile").write_text('FROM amazoncorretto:25\nENTRYPOINT ["java", "-XX:+UseCompactObjectHeaders"]\n')
    assert check(repo, "svc", {"language": "java"}, run_tools=False)["status"] == "ready"


def test_generate_dockerfile_resolves_a_broken_repo_dockerfile(tmp_path):
    """APP-HE 2026-10-27: CI readiness said 'blocked' although Execution replaces the Dockerfile."""
    repo = _java_app(tmp_path, tests=True)
    (repo / "svc/Dockerfile").write_text('FROM amazoncorretto:21\nENTRYPOINT ["java", "-XX:+UseCompactObjectHeaders"]\n')
    assert check(repo, "svc", {"language": "java"}, run_tools=False)["status"] == "blocked"
    r = check(repo, "svc", {"language": "java"}, run_tools=False, generate_dockerfile=True)
    item = next(i for i in r["items"] if i["check"] == "dockerfile")
    assert r["status"] == "ready" and item["status"] == "ok" and "replaced by AMF" in item["evidence"]


@pytest.mark.parametrize("parent,cfg_name,cfg,want", [
    ("1.5.2.RELEASE", "application.properties", "management.contextPath=/manage\n", "/manage/health"),
    ("1.5.2.RELEASE", "application.properties", "server.port=8080\n", "/health"),
    ("3.2.0", "application.properties", "server.servlet.context-path=/api\n", "/api/actuator/health"),
    ("4.1.0", "application.yaml", "management:\n  endpoints:\n    web:\n      base-path: /ops\n", "/ops/health"),
    ("4.1.0", "application.yaml", "spring:\n  application:\n    name: x\n", "/actuator/health"),
])
def test_spring_actuator_health_path(tmp_path, parent, cfg_name, cfg, want):
    """APP-TE 2026-10-27: PetClinic's actuator lives under management.contextPath=/manage."""
    app = tmp_path / "svc"
    (app / "src/main/resources").mkdir(parents=True)
    (app / "pom.xml").write_text(f"<project><parent><artifactId>spring-boot-starter-parent</artifactId>"
                                 f"<version>{parent}</version></parent><dependencies><dependency>"
                                 "<artifactId>spring-boot-starter-actuator</artifactId></dependency></dependencies></project>")
    (app / "src/main/resources" / cfg_name).write_text(cfg)
    assert scan_wiring(tmp_path, "svc")["health_check_path"] == want


def test_no_actuator_no_health_guess(tmp_path):
    app = tmp_path / "svc"
    app.mkdir()
    (app / "pom.xml").write_text("<project><artifactId>spring-boot-starter-web</artifactId></project>")
    assert "health_check_path" not in scan_wiring(tmp_path, "svc")
