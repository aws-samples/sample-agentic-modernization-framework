"""
Unit tests for GET /workflow/execution/{app_id} — the consolidated
execution-phase view (ECS jobs, per-step progress, sub-status, reports).
Uses moto to mock DynamoDB.
"""

from __future__ import annotations

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def dynamodb_table():
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

        app_id = "exec-app"
        table.put_item(Item={
            "PK": f"APP#{app_id}", "SK": "META", "app_id": app_id, "app_name": "Exec App",
            "exec_database_status": "done", "exec_cicd_status": "done",  # infra intentionally unset
        })
        table.put_item(Item={
            "PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE",
            "GSI1PK": "TYPE#WORKFLOW", "GSI1SK": app_id,
            "app_id": app_id, "current_phase": "execution", "current_step": "execution_review",
            "execution_arn": "arn:aws:states:us-east-1:1:execution:sm:run-1",
        })
        # ECS job (own partition + TYPE#JOB GSI, GSI1SK == job_id)
        table.put_item(Item={
            "PK": "JOB#transform-exec-app-abc123", "SK": "META",
            "GSI1PK": "TYPE#JOB", "GSI1SK": "transform-exec-app-abc123",
            "job_id": "transform-exec-app-abc123", "app_id": app_id, "mode": "workbench",
            "td_name": "AWS/multi", "status": "COMPLETED", "created_at": "2026-08-24T15:00:00Z",
            "task_arn": "arn:aws:ecs:us-east-1:1:task/amf-cluster/deadbeef",
        })
        # A prior code-analysis job (assessment phase) — counted separately.
        table.put_item(Item={
            "PK": "JOB#analysis-exec-app-111", "SK": "META",
            "GSI1PK": "TYPE#JOB", "GSI1SK": "analysis-exec-app-111",
            "job_id": "analysis-exec-app-111", "app_id": app_id, "mode": "code-analysis",
            "td_name": "AWS/comprehensive-codebase-analysis", "status": "COMPLETED",
            "created_at": "2026-08-20T12:00:00Z",
            "task_arn": "arn:aws:ecs:us-east-1:1:task/amf-cluster/cafef00d",
        })
        # A job for a DIFFERENT app must not leak in.
        table.put_item(Item={
            "PK": "JOB#transform-other-xyz", "SK": "META",
            "GSI1PK": "TYPE#JOB", "GSI1SK": "transform-other-xyz",
            "job_id": "transform-other-xyz", "app_id": "other-app", "mode": "workbench",
            "status": "RUNNING",
        })
        # Transform PLAN with per-step status
        table.put_item(Item={
            "PK": f"APP#{app_id}", "SK": "PLAN#transform-exec-app-abc123",
            "GSI1PK": "TYPE#PLAN", "GSI1SK": "transform-exec-app-abc123",
            "plan_id": "transform-exec-app-abc123", "app_id": app_id, "status": "completed",
            "created_at": "2026-08-24T15:00:00Z",
            "steps": [
                {"step_number": 1, "td_name": "AWS/java-version-upgrade", "repo_name": "svc",
                 "repo_branch": "main-amf", "status": "completed", "message": "Pushed to main-amf"},
                {"step_number": 2, "td_name": "AWS/spring-boot-version-upgrade", "repo_name": "svc",
                 "repo_branch": "main-amf", "status": "running", "message": ""},
            ],
        })
        # Execution report (cicd present, others absent)
        table.put_item(Item={
            "PK": f"APP#{app_id}", "SK": "ANALYSIS#execution-cicd#2026-08-24T15:35:00Z",
            "GSI1PK": "TYPE#ANALYSIS", "GSI1SK": f"{app_id}#execution-cicd",
            "app_id": app_id, "analysis_type": "execution-cicd", "status": "completed",
            "updated_at": "2026-08-24T15:35:00Z", "data": {"report_md": "pipeline"},
        })
        yield table


@pytest.fixture
def app_client(dynamodb_table):
    with patch("backend.src.db.helpers._table", dynamodb_table):
        with patch("backend.src.db.helpers.get_table", return_value=dynamodb_table):
            from fastapi.testclient import TestClient

            from backend.src.main import app
            yield TestClient(app)


class TestExecutionDetails:
    def test_returns_jobs_steps_status_reports(self, app_client):
        resp = app_client.get("/api/v1/workflow/execution/exec-app")
        assert resp.status_code == 200
        data = resp.json()["data"]

        # Sub-phase status flags surfaced from META (infra left unset).
        assert data["sub_status"] == {"database": "done", "infra": "", "cicd": "done"}

        # This app's jobs only (transform + analysis), other-app excluded.
        assert {j["job_id"] for j in data["jobs"]} == {
            "transform-exec-app-abc123", "analysis-exec-app-111"}
        job = next(j for j in data["jobs"] if j["job_id"] == "transform-exec-app-abc123")
        assert job["task_id"] == "deadbeef"
        assert job["status"] == "COMPLETED"
        # CloudWatch log stream is derived from the task id (durable link).
        assert job["log_stream"] == "assessment/migration-worker/deadbeef"
        assert job["log_group"].endswith("migration-worker")

        # code-analysis runs are counted separately from the transformation.
        assert data["analysis_run_count"] == 1

        # Transform block points at the workbench task + its log stream.
        assert data["transform"]["task_id"] == "deadbeef"
        assert data["transform"]["plan_status"] == "completed"
        assert data["transform"]["log_stream"] == "assessment/migration-worker/deadbeef"

        # Per-step progress from the PLAN# record.
        assert data["plan_status"] == "completed"
        assert [s["status"] for s in data["steps"]] == ["completed", "running"]
        assert data["steps"][0]["repo_branch"] == "main-amf"

        # Reports: cicd present, database/infra absent.
        by_type = {r["type"]: r for r in data["reports"]}
        assert by_type["execution-cicd"]["present"] is True
        assert by_type["execution-database"]["present"] is False
        assert by_type["execution-infra"]["present"] is False

    def test_unknown_app_is_empty(self, app_client):
        resp = app_client.get("/api/v1/workflow/execution/nope")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["jobs"] == []
        assert data["steps"] == []
        assert all(not r["present"] for r in data["reports"])
