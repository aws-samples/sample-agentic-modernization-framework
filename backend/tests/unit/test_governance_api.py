"""
Unit tests for the Governance API endpoints.

DynamoDB is mocked with moto. Step Functions is STUBBED (moto's stepfunctions
support is too thin to drive a real execution history): the router's
``_sfn_client`` is monkeypatched to return a fake client that yields a
synthetic execution list plus a synthetic history containing an ``Approval_*``
state and a failed execution.

The governance router is mounted on a throwaway FastAPI app here — main.py's
router registration is owned by a separate task and is intentionally not
depended upon.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from backend.src.api.v1 import governance

# ── Synthetic Step Functions data ──────────────────────────────────────

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _ts(minute: int) -> datetime:
    return datetime(2026, 1, 1, 0, minute, 0, tzinfo=UTC)


# A SUCCEEDED run for test-app-001 whose history has two Approval_* gates:
# one approved (TaskSucceeded), one rejected (TaskFailed).
_HISTORY_SUCCEEDED = [
    {
        "type": "ExecutionStarted",
        "timestamp": _ts(0),
        "executionStartedEventDetails": {"input": '{"app_id": "test-app-001", "started_by": "user"}'},
    },
    {"type": "TaskStateEntered", "timestamp": _ts(1), "stateEnteredEventDetails": {"name": "Approval_DiscoveryReview"}},
    {"type": "TaskSucceeded", "timestamp": _ts(2)},
    {"type": "TaskStateEntered", "timestamp": _ts(3), "stateEnteredEventDetails": {"name": "Approval_DesignReview"}},
    {"type": "TaskFailed", "timestamp": _ts(4)},
]

# A FAILED run whose single Approval_* gate is still pending (never resolved).
_HISTORY_FAILED = [
    {
        "type": "ExecutionStarted",
        "timestamp": _ts(0),
        "executionStartedEventDetails": {"input": '{"app_id": "test-app-001"}'},
    },
    {
        "type": "TaskStateEntered",
        "timestamp": _ts(1),
        "stateEnteredEventDetails": {"name": "Approval_AssessmentReview"},
    },
]


class _FakeSfn:
    """Minimal Step Functions stub driven by canned executions + histories."""

    def __init__(self, executions: list[dict], histories: dict[str, list[dict]], raise_on_list: bool = False):
        self._executions = executions
        self._histories = histories
        self._raise_on_list = raise_on_list

    def list_executions(self, **_kwargs):
        if self._raise_on_list:
            raise RuntimeError("StateMachineDoesNotExist: simulated Step Functions failure")
        return {"executions": self._executions}

    def get_execution_history(self, executionArn="", **_kwargs):
        return {"events": self._histories.get(executionArn, [])}


def _default_executions() -> list[dict]:
    return [
        {
            "name": "test-app-001-20260101-000000",
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:sm:run-succeeded",
            "status": "SUCCEEDED",
            "startDate": _ts(0),
            "stopDate": _ts(5),
        },
        {
            "name": "test-app-001-20260102-000000",
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:sm:run-failed",
            "status": "FAILED",
            "startDate": _ts(10),
            "stopDate": _ts(12),
        },
        # An unrelated app's run — must be filtered out of per-app views.
        {
            "name": "other-app-20260103-000000",
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:sm:run-other",
            "status": "RUNNING",
            "startDate": _ts(20),
            "stopDate": None,
        },
    ]


def _default_histories() -> dict[str, list[dict]]:
    return {
        "arn:aws:states:us-east-1:123456789012:execution:sm:run-succeeded": _HISTORY_SUCCEEDED,
        "arn:aws:states:us-east-1:123456789012:execution:sm:run-failed": _HISTORY_FAILED,
        "arn:aws:states:us-east-1:123456789012:execution:sm:run-other": [],
    }


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def dynamodb_table():
    """Create a mocked DynamoDB table seeded with governance artifacts."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="amf_portfolio",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="amf_portfolio")

        # App META (Inception is also evidenced by the ANALYSIS# row below).
        table.put_item(
            Item={
                "PK": "APP#test-app-001",
                "SK": "META",
                "app_id": "test-app-001",
                "app_name": "Test Application",
                "status": "migrating",
                "GSI1PK": "TYPE#APP",
                "GSI1SK": "test-app-001",
            }
        )
        # Inception artifact — an assessment analysis.
        table.put_item(
            Item={
                "PK": "APP#test-app-001",
                "SK": "ANALYSIS#modernization-readiness-analysis#2026-01-01T00:01:00+00:00",
                "app_id": "test-app-001",
                "analysis_type": "modernization-readiness-analysis",
                "timestamp": "2026-01-01T00:01:00+00:00",
                "GSI1PK": "TYPE#ANALYSIS",
                "GSI1SK": "test-app-001#modernization-readiness-analysis",
            }
        )
        # Construction artifact — a design document.
        table.put_item(
            Item={
                "PK": "APP#test-app-001",
                "SK": "DESIGN#v1",
                "app_id": "test-app-001",
                "generated_at": "2026-01-01T00:03:00+00:00",
                "GSI1PK": "TYPE#DESIGN",
                "GSI1SK": "test-app-001#v1",
            }
        )
        yield table


@pytest.fixture
def client(dynamodb_table, monkeypatch):
    """FastAPI test client with the governance router mounted + SFN stubbed."""
    from unittest.mock import patch

    # Point the DB layer at the moto table (get_table reads helpers._table).
    monkeypatch.setattr("backend.src.db.helpers._table", dynamodb_table)

    # Stub Step Functions by default with the synthetic runs + histories.
    monkeypatch.setattr(
        governance,
        "_sfn_client",
        lambda: _FakeSfn(_default_executions(), _default_histories()),
    )

    with mock_aws():
        with patch("backend.src.db.helpers._table", dynamodb_table):
            from fastapi import FastAPI
            from fastapi.testclient import TestClient

            app = FastAPI()
            app.include_router(governance.router, prefix="/api/v1/governance")
            yield TestClient(app)


def _stub_sfn(monkeypatch, *, raise_on_list=False):
    monkeypatch.setattr(
        governance,
        "_sfn_client",
        lambda: _FakeSfn(_default_executions(), _default_histories(), raise_on_list=raise_on_list),
    )


# ── Tests ──────────────────────────────────────────────────────────────


class TestSummary:
    def test_summary_aggregates_runs_and_gates(self, client):
        """/summary rolls up runs by status and gates by outcome from history."""
        resp = client.get("/api/v1/governance/summary")
        assert resp.status_code == 200
        data = resp.json()["data"]

        # 3 executions in the stub (2 for test-app-001, 1 for other-app).
        assert data["total_runs"] == 3
        assert data["runs_by_status"]["SUCCEEDED"] == 1
        assert data["runs_by_status"]["FAILED"] == 1
        assert data["runs_by_status"]["RUNNING"] == 1

        # Gates: succeeded run has approved + rejected; failed run has 1 pending.
        assert data["gates"]["total"] == 3
        assert data["gates"]["approved"] == 1
        assert data["gates"]["rejected"] == 1
        assert data["gates"]["pending"] == 1

        # Only test-app-001 is a known app in DynamoDB → 1 app under governance.
        assert data["apps_under_governance"] == 1
        assert data["governed_app_ids"] == ["test-app-001"]

    def test_summary_not_swallowed_by_app_id_route(self, client):
        """GET /summary hits the summary handler, not /{app_id} with 'summary'."""
        resp = client.get("/api/v1/governance/summary")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # Summary-shaped payload; would be app-detail-shaped if misrouted.
        assert "total_runs" in data
        assert "app_id" not in data

    def test_summary_includes_failed_run(self, client):
        """A FAILED run is represented, not filtered out."""
        data = client.get("/api/v1/governance/summary").json()["data"]
        assert data["runs_by_status"].get("FAILED") == 1


class TestAppDetail:
    def test_detail_bands_into_three_phases(self, client):
        """Every entry is banded into Inception / Construction / Operation."""
        resp = client.get("/api/v1/governance/test-app-001")
        assert resp.status_code == 200
        data = resp.json()["data"]

        phases = data["phases"]
        # Inception: the assessment analysis. Construction: design + gates.
        # Operation holds handover gates only (none in this fixture).
        assert set(phases) >= {"Inception", "Construction", "Operation"}
        assert len(phases["Inception"]) >= 1
        assert len(phases["Construction"]) >= 1
        assert "audit" not in data

        # Only this app's runs are included (other-app filtered out).
        assert all(e["execution"].startswith("test-app-001") for e in data["executions"])
        assert len(data["executions"]) == 2

    def test_detail_represents_rejected_and_failed(self, client):
        """Rejected gate and FAILED run both survive into the detail payload."""
        data = client.get("/api/v1/governance/test-app-001").json()["data"]

        outcomes = {g["outcome"] for g in data["gates"]}
        assert "rejected" in outcomes
        assert "approved" in outcomes
        assert "pending" in outcomes

        statuses = {e["status"] for e in data["executions"]}
        assert "FAILED" in statuses

    def test_unknown_app_returns_404(self, client):
        """Unknown app (no DDB rows, no matching executions) → HTTP 404."""
        resp = client.get("/api/v1/governance/does-not-exist")
        assert resp.status_code == 404
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "NOT_FOUND"

    def test_gates_endpoint(self, client):
        """/{app_id}/gates returns the gate list with outcomes."""
        resp = client.get("/api/v1/governance/test-app-001/gates")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["summary"]["total"] == 3
        assert data["summary"]["approved"] == 1
        assert data["summary"]["rejected"] == 1
        assert data["summary"]["pending"] == 1


class TestGracefulDegradation:
    def test_detail_survives_sfn_failure(self, client, monkeypatch):
        """When Step Functions raises, detail still returns 200 + DDB data + note."""
        _stub_sfn(monkeypatch, raise_on_list=True)
        resp = client.get("/api/v1/governance/test-app-001")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # No executions/gates from SFN, but DynamoDB artifacts are still present.
        assert data["executions"] == []
        assert "note" in data
        assert data["artifacts"]["analyses"]  # assessment analysis survives

    def test_summary_survives_sfn_failure(self, client, monkeypatch):
        """When Step Functions raises, /summary returns 200 with a note, no 500."""
        _stub_sfn(monkeypatch, raise_on_list=True)
        resp = client.get("/api/v1/governance/summary")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_runs"] == 0
        assert "note" in data
