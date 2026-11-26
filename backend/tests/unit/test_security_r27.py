"""Negative tests for the API/auth hardening."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jose import JWTError
from moto import mock_aws

from backend.src.auth.middleware import AuthUser
from backend.src.config import Settings, settings
from backend.src.main import app

REPO = Path(__file__).resolve().parents[3]


# ── R2: skill upload ──────────────────────────────────────────────────────


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    from backend.src.api.v1 import skills

    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(settings, "s3_bucket", "")
    return tmp_path


def _upload(names: list[str], body: bytes = b"# skill\n"):
    files = [("files", (n, io.BytesIO(body), "text/markdown")) for n in names]
    return TestClient(app).post("/api/v1/skills/upload", files=files).json()["data"]


@pytest.mark.parametrize("name", [
    "../../src/main.md", "..\\..\\evil.md", "/etc/passwd.md", "sub/dir.md", "Design.md", "x.md.txt",
    "a b.md", ".md", "..md", "evil.MD",
])
def test_skill_upload_refuses_traversal_and_bad_names(skills_dir, name):
    data = _upload([name])
    assert data["uploaded"] == [] and data["skipped"][0]["name"] == name
    assert list(skills_dir.iterdir()) == []


@pytest.mark.parametrize("core", ["discovery", "design", "execution", "risk_scoring", "handover"])
def test_skill_upload_refuses_core_playbooks(skills_dir, core):
    data = _upload([f"{core}.md"])
    assert data["uploaded"] == [] and "Core Harness playbooks" in data["skipped"][0]["reason"]
    assert not (skills_dir / f"{core}.md").exists()


def test_skill_upload_accepts_a_new_skill(skills_dir):
    data = _upload(["my-team_notes.md"])
    assert [u["file"] for u in data["uploaded"]] == ["my-team_notes.md"]
    assert (skills_dir / "my-team_notes.md").read_bytes() == b"# skill\n"


def test_skill_upload_size_cap(skills_dir, monkeypatch):
    monkeypatch.setattr(settings, "skill_upload_max_bytes", 10)
    data = _upload(["big.md"], b"x" * 11)
    assert data["uploaded"] == [] and "Larger than" in data["skipped"][0]["reason"]


def test_core_skill_list_matches_harness_tf_and_the_playbooks():
    from backend.src.api.v1.skills import CORE_SKILLS

    tf = (REPO / "infra/environments/agentcore/harness.tf").read_text()
    listed = re.search(r"harness_skill_files\s*=\s*\[([^\]]*)\]", tf).group(1)
    assert set(re.findall(r'"([^"]+)"', listed)) == CORE_SKILLS
    assert {p.stem for p in (REPO / "backend/resources/skills").glob("*.md")} == CORE_SKILLS


@pytest.mark.parametrize("name", ["Design", "__init__.py", "a.b", "..x"])
def test_get_skill_rejects_odd_names(name):
    resp = TestClient(app).get(f"/api/v1/skills/{name}")
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == f"Skill '{name}' not found"  # no filesystem path


def test_get_skill_not_found_hides_the_path():
    resp = TestClient(app).get("/api/v1/skills/nonexistent")
    assert resp.status_code == 404 and "resources" not in resp.text


# ── R6: XSS ───────────────────────────────────────────────────────────────

PAYLOADS = ["<script>alert(1)</script>", '<img src=x onerror="alert(1)">', "<iframe src=javascript:alert(1)>"]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_md_to_html_escapes_every_construct(payload):
    from backend.src.api.v1.analysis import _md_to_html

    md = "\n".join([f"# {payload}", f"## {payload}", f"- {payload}", f"**{payload}** `{payload}`",
                    "```", payload, "```", payload])
    out = _md_to_html(md)
    assert "<script" not in out and "<img" not in out and "<iframe" not in out
    assert "&lt;" in out
    assert "<h1>" in out and "<strong>" in out and "<code" in out  # markdown still renders


@pytest.mark.parametrize(("line", "want"), [
    ("**a** and **b**", "<strong>a</strong> and <strong>b</strong>"),
    ("**unclosed", "**unclosed"),
    ("****", "****"),
    ("**a*b**", "**a*b**"),
    ("x `c` y `d`", 'x <code s>c</code> y <code s>d</code>'),
    ("`` `x`", "`<code s> </code>x`"),
])
def test_wrap_spans_matches_the_markdown_rules(line, want):
    from backend.src.api.v1.analysis import _wrap_spans

    tag = "<code s>" if "`" in line else "<strong>"
    close = "</code>" if "`" in line else "</strong>"
    marker = "`" if "`" in line else "**"
    assert _wrap_spans(line, marker, tag, close) == want


def test_wrap_spans_is_linear_on_adversarial_input():
    import time

    from backend.src.api.v1.analysis import _md_to_html

    start = time.monotonic()
    _md_to_html("*" * 200_000 + "`" * 200_000)
    assert time.monotonic() - start < 2


@pytest.mark.parametrize("path", ["/api/v1/analysis/A%20B/x/html", "/api/v1/analysis/A/x..y/html",
                                  "/api/v1/analysis/A/-x/html"])
def test_html_report_rejects_odd_ids(path):
    table = MagicMock()
    with patch("backend.src.api.v1.analysis.get_table", return_value=table):
        resp = TestClient(app).get(path)
    assert resp.status_code == 400
    table.query.assert_not_called()


def _report_table(item):
    t = MagicMock()
    t.query.return_value = {"Items": [item]}
    return t


def test_html_report_is_escaped_and_served_with_a_restrictive_csp():
    from backend.src.api.v1 import analysis

    item = {"PK": "APP#A", "SK": "ANALYSIS#x#1", "data": {"report_md": "# hi <script>alert(1)</script>"}}
    with patch.object(analysis, "get_table", return_value=_report_table(item)):
        resp = TestClient(app).get("/api/v1/analysis/A/x/html")
    assert resp.status_code == 200 and "<script>" not in resp.text
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_raw_s3_html_report_is_served_inert():
    from backend.src.api.v1 import analysis

    item = {"PK": "APP#A", "SK": "ANALYSIS#x#1", "run_id": "r1"}
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": [{"Key": "applications/A/analysis/x/r1/report.html"}]}
    s3.get_object.return_value = {"Body": io.BytesIO(b"<html><script>steal()</script></html>")}
    with patch.object(analysis, "get_table", return_value=_report_table(item)), patch.object(analysis, "s3", s3):
        resp = TestClient(app).get("/api/v1/analysis/A/x/html")
    assert resp.headers["content-security-policy"] == analysis.REPORT_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("tag", [
    '<meta http-equiv="refresh" content="0;url=https://evil.example.com">',
    "<META HTTP-EQUIV=Refresh CONTENT='0; URL=https://evil.example.com'/>",
    '<base href="https://evil.example.com/">',
])
def test_raw_s3_html_report_cannot_redirect(tag):
    from backend.src.api.v1 import analysis

    item = {"PK": "APP#A", "SK": "ANALYSIS#x#1", "run_id": "r1"}
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": [{"Key": "applications/A/analysis/x/r1/report.html"}]}
    body = f"<html><head>{tag}</head>\n<body><p>report</p></body></html>"
    s3.get_object.return_value = {"Body": io.BytesIO(body.encode())}
    with patch.object(analysis, "get_table", return_value=_report_table(item)), patch.object(analysis, "s3", s3):
        resp = TestClient(app).get("/api/v1/analysis/A/x/html")
    assert resp.status_code == 200
    assert "evil.example.com" not in resp.text and "<p>report</p>" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "sandbox" in csp and "base-uri 'none'" in csp and "form-action 'none'" in csp


def test_report_error_does_not_leak_exception_text():
    from backend.src.api.v1 import analysis

    t = MagicMock()
    t.query.side_effect = RuntimeError("arn:aws:dynamodb:us-east-1:123456789012:table/secret")
    with patch.object(analysis, "get_table", return_value=t):
        resp = TestClient(app).get("/api/v1/analysis/A/x/html")
    assert resp.status_code == 500 and "123456789012" not in resp.text and "error id" in resp.text


def test_global_security_headers():
    resp = TestClient(app).get("/api/v1/system/capabilities")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"


# ── L7: generic errors ────────────────────────────────────────────────────


def test_json_errors_are_generic_with_code_kept():
    from backend.src.api.v1 import portfolio_program

    boom = RuntimeError("botocore: arn:aws:iam::123456789012:role/x is not authorized")
    with patch.object(portfolio_program, "_load_program", side_effect=boom):
        resp = TestClient(app).get("/api/v1/portfolio/dashboard")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "DASHBOARD_FAILED"
    assert "123456789012" not in resp.text and "error id" in body["error"]["message"]


def test_no_api_route_returns_raw_exception_text():
    """Static guard: no `error_response(str(exc)…)` / `{exc}` in a client message."""
    offenders = []
    for p in (REPO / "backend/src/api/v1").glob("*.py"):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if "logger." in line:
                continue
            if re.search(r"error_response\(str\(e(xc)?\)|\"message\": str\(e(xc)?\)\}|\{exc\}", line):
                if "Curated _atx_list" in p.read_text().splitlines()[n - 2]:
                    continue
                offenders.append(f"{p.name}:{n}: {line.strip()}")
    assert not offenders, offenders


# ── R7: privileged fields on PUT /portfolio/apps/{id} ─────────────────────


@pytest.fixture
def app_table():
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="amf_portfolio", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={"PK": "APP#APP-P", "SK": "META", "app_id": "APP-P", "status": "discovered",
                         "migration_strategy": "replatform", "exec_head_commit": "a" * 40,
                         "risk_score": 40})
        from backend.src.api.v1 import portfolio

        with patch.object(portfolio, "get_table", return_value=t):
            yield t


def _req(groups):
    r = MagicMock()
    r.state.user = AuthUser(sub="s", username="u", email="u@example.com", groups=groups)
    return r


@pytest.mark.parametrize("field,value", [
    ("deploy_approved_commit", "b" * 40), ("deploy_approved_by", "me"), ("deploy_approved_at", "now"),
    ("exec_head_commit", "c" * 40), ("status", "completed"), ("current_step", "deploy_running"),
    ("exec_deploy_status", "deployed"), ("validation_status", "pass"), ("risk_score", 1),
    ("PK", "APP#OTHER"), ("GSI1PK", "TYPE#APP"),
])
def test_put_refuses_server_managed_fields(app_table, field, value):
    from backend.src.api.v1 import portfolio

    with pytest.raises(HTTPException) as e:
        portfolio.update_app("APP-P", {field: value, "description": "x"}, _req(["Admin"]))
    assert e.value.status_code == 400 and f"'{field}'" in e.value.detail
    meta = app_table.get_item(Key={"PK": "APP#APP-P", "SK": "META"})["Item"]
    assert "description" not in meta  # nothing written


def test_put_drops_unchanged_echoes_of_privileged_fields(app_table):
    """The edit modal PUTs the whole GET record back: unchanged values are a no-op."""
    from backend.src.api.v1 import portfolio

    body = {"PK": "APP#APP-P", "SK": "META", "status": "discovered", "exec_head_commit": "a" * 40,
            "risk_score": 40, "description": "edited"}
    resp = portfolio.update_app("APP-P", body, _req(["Engineer"]))
    assert resp.status == "success"
    meta = app_table.get_item(Key={"PK": "APP#APP-P", "SK": "META"})["Item"]
    assert meta["description"] == "edited" and meta["status"] == "discovered"


@pytest.mark.parametrize("field", ["expose_publicly", "validation_write_tests"])
def test_admin_only_fields(app_table, field):
    from backend.src.api.v1 import portfolio

    with pytest.raises(HTTPException) as e:
        portfolio.update_app("APP-P", {field: True}, _req(["Engineer"]))
    assert e.value.status_code == 403 and e.value.detail == "Requires ADMIN role. Your role: ENGINEER"
    assert portfolio.update_app("APP-P", {field: True}, _req(["Admin"])).status == "success"
    assert app_table.get_item(Key={"PK": "APP#APP-P", "SK": "META"})["Item"][field] is True


def test_engineer_may_set_assessment_gate_inputs(app_table):
    from backend.src.api.v1 import portfolio

    resp = portfolio.update_app("APP-P", {"target_compute": "ecs_fargate", "target_account": "123456789012",
                                          "vpc_cidr": "10.1.0.0/16"}, _req(["Engineer"]))
    assert resp.status == "success"


def test_put_never_creates_an_app(app_table):
    from backend.src.api.v1 import portfolio

    resp = portfolio.update_app("APP-NOPE", {"description": "x"}, _req(["Admin"]))
    assert resp.status_code == 404
    assert "Item" not in app_table.get_item(Key={"PK": "APP#APP-NOPE", "SK": "META"})


def test_put_condition_blocks_a_create_race(app_table):
    """META deleted between the read and the write → 404, not an upsert."""
    from backend.src.api.v1 import portfolio

    real_update = app_table.update_item

    def delete_then_update(**kw):
        app_table.delete_item(Key={"PK": "APP#APP-P", "SK": "META"})
        return real_update(**kw)

    with patch.object(app_table, "update_item", side_effect=delete_then_update):
        resp = portfolio.update_app("APP-P", {"description": "x"}, _req(["Admin"]))
    assert resp.status_code == 404
    assert "Item" not in app_table.get_item(Key={"PK": "APP#APP-P", "SK": "META"})


def test_http_put_privileged_field_is_400_with_detail(app_table):
    resp = TestClient(app).put("/api/v1/portfolio/apps/APP-P", json={"deploy_approved_commit": "f" * 40})
    assert resp.status_code == 400 and "deploy_approved_commit" in resp.json()["detail"]


def test_enrich_refuses_server_managed_fields(app_table):
    from backend.src.api.v1 import portfolio

    items = [portfolio.EnrichAppItem(app_id="APP-P", deploy_approved_commit="d" * 40, tier="tier_1")]
    data = portfolio.bulk_enrich(items, _req(["Engineer"])).data
    assert data["updated"] == 0 and "deploy_approved_commit" in data["errors"][0]["error"]
    items = [portfolio.EnrichAppItem(app_id="APP-P", expose_publicly=True)]
    data = portfolio.bulk_enrich(items, _req(["Engineer"])).data
    assert "Requires ADMIN" in data["errors"][0]["error"]


def test_create_never_overwrites_an_existing_app(app_table):
    from backend.src.api.v1 import portfolio

    resp = portfolio.create_app(portfolio.ApplicationModel(app_id="APP-P", app_name="clobber"))
    assert resp.status_code == 409
    meta = app_table.get_item(Key={"PK": "APP#APP-P", "SK": "META"})["Item"]
    assert meta["exec_head_commit"] == "a" * 40


# ── R8: chat sessions ─────────────────────────────────────────────────────


def test_chat_session_is_per_user_and_ignores_the_client_id():
    from backend.src.api.v1 import chat

    seen = []

    def fake_invoke(app_id, prompt, session_id):
        seen.append(session_id)
        return {"response": "ok"}

    with patch.object(chat, "_invoke_harness", side_effect=fake_invoke), \
         patch.object(chat, "_load_session", return_value=[]), patch.object(chat, "_save_session"):
        r = TestClient(app).post("/api/v1/chat/message",
                                 json={"app_id": "APP-X", "message": "hi", "session_id": "chat-someone-else"})
    assert r.json()["data"]["session_id"] == seen[0] != "chat-someone-else"
    alice = chat.chat_session_id(AuthUser(sub="alice"), "APP-X")
    bob = chat.chat_session_id(AuthUser(sub="bob"), "APP-X")
    assert alice != bob and alice.endswith("-APP-X") and "alice" not in alice
    assert chat.chat_session_id(AuthUser(sub="alice"), "") == chat.chat_session_id(AuthUser(sub="alice"), "portfolio")
    with pytest.raises(HTTPException):
        chat.chat_session_id(AuthUser(), "APP-X")


@pytest.mark.parametrize("bad", ["../x", "APP/1", "a" * 200, "APP X"])
def test_chat_rejects_odd_app_ids(bad):
    r = TestClient(app).post("/api/v1/chat/message", json={"app_id": bad, "message": "hi"})
    assert r.status_code == 400


# ── R8: Cognito token checks ──────────────────────────────────────────────

CLIENT = "client-123"


@pytest.mark.parametrize("claims", [
    {"token_use": "access", "client_id": CLIENT, "sub": "u"},
    {"token_use": "id", "aud": CLIENT, "sub": "u"},
    {"token_use": "id", "aud": [CLIENT], "sub": "u"},
])
def test_token_audience_accepts_this_client(claims):
    from backend.src.auth.cognito import check_token_audience

    check_token_audience(claims, CLIENT)


@pytest.mark.parametrize("claims", [
    {"token_use": "access", "client_id": "other-client", "sub": "u"},
    {"token_use": "access", "sub": "u"},  # no client_id
    {"token_use": "access", "aud": CLIENT, "sub": "u"},  # access token must use client_id
    {"token_use": "id", "aud": "other-client", "sub": "u"},
    {"token_use": "id", "aud": [CLIENT, "other"], "sub": "u"},
    {"token_use": "refresh", "client_id": CLIENT, "sub": "u"},
    {"client_id": CLIENT, "sub": "u"},  # no token_use
    {"token_use": "access", "client_id": CLIENT},  # no sub
])
def test_token_audience_rejects_forged_claims(claims):
    from backend.src.auth.cognito import check_token_audience

    with pytest.raises(JWTError):
        check_token_audience(claims, CLIENT)


def test_token_audience_fails_closed_without_a_configured_client():
    from backend.src.auth.cognito import check_token_audience

    with pytest.raises(JWTError):
        check_token_audience({"token_use": "access", "client_id": "", "sub": "u"}, "")


def test_validate_token_checks_audience_on_a_signed_token(monkeypatch):
    """End to end through jose with a real RS256 key: a token for another client is refused."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk, jwt

    from backend.src.auth import cognito

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pub = jwk.construct(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo),
                        "RS256").to_dict()
    pub["kid"] = "k1"
    monkeypatch.setattr(settings, "aws_region", "us-east-1")
    monkeypatch.setattr(settings, "cognito_user_pool_id", "us-east-1_pool")
    monkeypatch.setattr(settings, "cognito_app_client_id", CLIENT)
    v = cognito.CognitoJWTValidator()
    monkeypatch.setattr(v, "fetch_jwks", lambda: {"keys": [pub]})

    def tok(**claims):
        base = {"iss": v.issuer, "sub": "u1", "exp": 4102444800, "cognito:groups": ["Engineer"]}
        return jwt.encode({**base, **claims}, pem, algorithm="RS256", headers={"kid": "k1"})

    assert v.validate_token(tok(token_use="access", client_id=CLIENT))["groups"] == ["Engineer"]
    for bad in (tok(token_use="access", client_id="other"), tok(token_use="id", aud="other"),
                tok(client_id=CLIENT)):
        with pytest.raises(JWTError):
            v.validate_token(bad)


# ── R8: hosted-mode guard ─────────────────────────────────────────────────


def _cfg(**kw):
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize("kw", [
    {"enable_public_hosting": True, "dev_auth": True, "cognito_user_pool_id": "p", "cognito_app_client_id": "c"},
    {"enable_public_hosting": True, "dev_auth": False},
    {"enable_public_hosting": True, "dev_auth": False, "cognito_user_pool_id": "p"},
    {"run_mode": "api", "dev_auth": True, "cognito_user_pool_id": "p", "cognito_app_client_id": "c"},
    {"run_mode": "api", "dev_auth": False},
    {"dev_auth": True, "cognito_user_pool_id": "p"},
])
def test_startup_guard_refuses_unsafe_auth(kw):
    from backend.src.auth.startup_guard import InsecureAuthConfigError, assert_auth_config_safe

    with pytest.raises(InsecureAuthConfigError):
        assert_auth_config_safe(_cfg(**kw))


@pytest.mark.parametrize("kw", [
    {"dev_auth": True},  # local dev
    {"dev_auth": False, "cognito_user_pool_id": "p", "cognito_app_client_id": "c"},
    {"enable_public_hosting": True, "dev_auth": False, "cognito_user_pool_id": "p", "cognito_app_client_id": "c"},
    {"run_mode": "code-analysis", "dev_auth": True},  # a worker MODE is not hosted
])
def test_startup_guard_allows_safe_auth(kw):
    from backend.src.auth.startup_guard import assert_auth_config_safe

    assert_auth_config_safe(_cfg(**kw))


def test_mode_env_marks_hosted(monkeypatch):
    from backend.src.auth.startup_guard import is_hosted

    monkeypatch.setenv("MODE", "api")
    assert is_hosted(Settings(_env_file=None))


def test_lifespan_refuses_to_start_when_unsafe(monkeypatch):
    monkeypatch.setattr(settings, "enable_public_hosting", True)
    from backend.src.auth.startup_guard import InsecureAuthConfigError

    with pytest.raises(InsecureAuthConfigError), TestClient(app):
        pass


def test_docs_are_not_mounted_without_dev_auth():
    env = {**os.environ, "DEV_AUTH": "false", "S3_BUCKET": "test-bucket", "AWS_ACCESS_KEY_ID": "x",
           "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1", "PYTHONPATH": str(REPO)}
    code = ("from backend.src.main import app; "
            "print(app.docs_url, app.redoc_url, app.openapi_url, "
            "any(getattr(r, 'path', '') in ('/docs', '/openapi.json') for r in app.routes))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "None None None False"


# ── L8: presign cap ───────────────────────────────────────────────────────


def test_presign_lifetime_is_capped_at_15_minutes():
    from backend.src.db.reports import _ttl

    assert settings.presign_max_seconds == 900 and settings.presign_ttl_seconds <= settings.presign_max_seconds
    assert _ttl(10**9) == 900 and _ttl(None) == settings.presign_ttl_seconds and _ttl(1) == settings.presign_min_seconds


# ── DEV_AUTH only on loopback; Cognito alg / pool pinning (PCSR) ──────────


def test_dev_auth_defaults_off(monkeypatch):
    monkeypatch.delenv("DEV_AUTH", raising=False)
    assert Settings(_env_file=None).dev_auth is False


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "my-laptop.local"])  # nosec B104 - test input: bind hosts the guard must refuse/parse
def test_startup_guard_refuses_dev_auth_on_a_non_loopback_bind(host):
    from backend.src.auth.startup_guard import InsecureAuthConfigError, assert_auth_config_safe

    with pytest.raises(InsecureAuthConfigError, match="non-loopback"):
        assert_auth_config_safe(_cfg(dev_auth=True), host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_startup_guard_allows_dev_auth_on_loopback(host):
    from backend.src.auth.startup_guard import assert_auth_config_safe

    assert_auth_config_safe(_cfg(dev_auth=True), host=host)
    assert_auth_config_safe(_cfg(dev_auth=False), host="0.0.0.0")  # Cognito path: any bind is fine  # nosec B104 - test input: bind hosts the guard must refuse/parse


def test_bind_host_reads_uvicorn_cli_and_env():
    from backend.src.auth.startup_guard import bind_host

    assert bind_host(["uvicorn", "app", "--host", "0.0.0.0"], {}) == "0.0.0.0"  # nosec B104 - test input: bind hosts the guard must refuse/parse
    assert bind_host(["uvicorn", "app", "--host=10.1.1.1"], {}) == "10.1.1.1"
    assert bind_host(["uvicorn", "app"], {"UVICORN_HOST": "0.0.0.0"}) == "0.0.0.0"  # nosec B104 - test input: bind hosts the guard must refuse/parse
    assert bind_host(["uvicorn", "app"], {}) == "127.0.0.1"


def test_validate_token_refuses_non_rs256_and_a_malformed_pool(monkeypatch):
    from jose import jwt

    from backend.src.auth import cognito

    monkeypatch.setattr(settings, "aws_region", "us-east-1")
    monkeypatch.setattr(settings, "cognito_app_client_id", CLIENT)
    monkeypatch.setattr(settings, "cognito_user_pool_id", "us-east-1_pool")
    v = cognito.CognitoJWTValidator()
    monkeypatch.setattr(v, "fetch_jwks", lambda: (_ for _ in ()).throw(AssertionError("no key lookup")))
    hs = jwt.encode({"iss": v.issuer, "sub": "u", "token_use": "access", "client_id": CLIENT, "exp": 4102444800},
                    "secret", algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(JWTError, match="RS256"):
        v.validate_token(hs)
    for pool in ("evil.com/x", "us-west-2_pool", "us-east-1_po/ol"):  # wrong shape / other region than the issuer's
        monkeypatch.setattr(settings, "cognito_user_pool_id", pool)
        with pytest.raises(JWTError, match="pool"):
            cognito.CognitoJWTValidator().validate_token(hs)
