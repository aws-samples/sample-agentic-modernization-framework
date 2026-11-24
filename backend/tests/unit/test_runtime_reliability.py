"""Regression tests for demo/runtime reliability failures."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.api.v1 import design, portfolio
from backend.src.db import helpers


def _expired_token() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ExpiredTokenException", "Message": "security token expired"}},
        "Query",
    )


def test_credential_operation_resets_and_retries_once(monkeypatch):
    calls = {"operation": 0, "reset": 0}

    def operation():
        calls["operation"] += 1
        if calls["operation"] == 1:
            raise _expired_token()
        return "recovered"

    monkeypatch.setattr(
        helpers,
        "reset_aws_session",
        lambda: calls.__setitem__("reset", calls["reset"] + 1),
    )

    assert helpers.call_with_credential_refresh(operation) == "recovered"
    assert calls == {"operation": 2, "reset": 1}


def test_noncredential_error_is_not_retried(monkeypatch):
    calls = {"reset": 0}
    monkeypatch.setattr(
        helpers,
        "reset_aws_session",
        lambda: calls.__setitem__("reset", calls["reset"] + 1),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        helpers.call_with_credential_refresh(
            lambda: (_ for _ in ()).throw(RuntimeError("database unavailable"))
        )
    assert calls["reset"] == 0


def test_design_s3_recreates_client_after_expired_credentials():
    stale_client = Mock()
    stale_client.get_object.side_effect = _expired_token()
    fresh_client = Mock()
    expected = {"Body": object()}
    fresh_client.get_object.return_value = expected

    with (
        patch.object(design.boto3, "client", side_effect=[stale_client, fresh_client]) as client_factory,
        patch.object(helpers, "reset_aws_session") as reset_session,
    ):
        result = design._s3_call("get_object", Bucket="design-bucket", Key="design.md")

    assert result is expected
    assert client_factory.call_count == 2
    reset_session.assert_called_once_with()


def _portfolio_client() -> TestClient:
    app = FastAPI()
    app.include_router(portfolio.router, prefix="/api/v1/portfolio")
    return TestClient(app)


def test_expired_credentials_return_503_not_empty_apps():
    with patch.object(portfolio, "call_with_credential_refresh", side_effect=_expired_token()):
        response = _portfolio_client().get("/api/v1/portfolio/apps")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "AWS_CREDENTIALS_EXPIRED"
    assert body["data"] is None


def test_dependency_failure_returns_503_not_empty_machines():
    with patch.object(
        portfolio,
        "call_with_credential_refresh",
        side_effect=RuntimeError("DynamoDB unavailable"),
    ):
        response = _portfolio_client().get("/api/v1/portfolio/machines")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "PORTFOLIO_UNAVAILABLE"
    assert body["data"] is None


def test_readiness_reports_expired_credentials():
    from backend.src.main import app

    with (
        patch("backend.src.db.helpers.call_with_credential_refresh", side_effect=_expired_token()),
        patch("backend.src.db.helpers.is_credential_error", return_value=True),
    ):
        response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AWS_CREDENTIALS_EXPIRED"


def test_cloudfront_spa_rewrite_excludes_api_and_preserves_errors():
    repo_root = Path(__file__).resolve().parents[3]
    terraform = (
        repo_root / "infra/environments/agentcore/public-hosting.tf"
    ).read_text()

    assert 'resource "aws_cloudfront_function" "spa_rewrite"' in terraform
    assert "uri.startsWith('/api/')" in terraform
    assert "uri.startsWith('/health')" in terraform
    assert "request.uri = '/index.html'" in terraform
    assert "custom_error_response" not in terraform


def test_start_script_uses_one_fail_closed_local_path():
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts/start.sh").read_text()

    assert 'PROJECT_PREFIX is required in .env' in script
    assert 'SFN_STATE_MACHINE_NAME="${SFN_STATE_MACHINE_NAME:-${PROJECT_PREFIX}-agentcore-workflow}"' in script
    assert 'VITE_API_PROXY_TARGET="${VITE_API_PROXY_TARGET:-http://127.0.0.1:8000}"' in script
    assert "http://127.0.0.1:8000/health/ready" in script
    assert "--host 127.0.0.1" in script
    assert "frontend/.env.local" not in script
