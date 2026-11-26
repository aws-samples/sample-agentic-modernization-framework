"""tunables in Settings, non-blocking/cached reads, paginated lists."""

from __future__ import annotations

import time

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from backend.src.config import Settings, settings

# ── capabilities ────────────────────────────────────────────────────────────


def test_capabilities_expose_regions_and_poll_cadence():
    from backend.src.main import app

    body = TestClient(app).get("/api/v1/system/capabilities").json()
    assert body["supported_target_regions"] == ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]
    assert body["validation_poll_ms"] == 15000 and isinstance(body["validation_poll_ms"], int)
    assert body["validation_poll_max"] == 60
    assert body["workflow_poll_ms"] == 10000


def test_new_settings_keep_todays_values():
    s = Settings(_env_file=None)
    assert s.risk_eosl_horizon_year == 2032
    # presign lifetime capped at 15 minutes.
    assert s.presign_ttl_seconds == 900 and s.presign_max_seconds == 900
    assert s.deploy_pipeline_timeout_seconds == 3600 and s.deploy_pipeline_poll_seconds == 20
    assert s.app_tfstate_bucket_pattern == "{prefix}-tfstate-{account}-{region}"


# ── /workflow/active ────────────────────────────────────────────────────────


def _wf_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(
        TableName="r26_portfolio",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "GSI1",
            "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                          {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


class _Sfn:
    def __init__(self, status="RUNNING"):
        self.described: list[str] = []
        self.status = status

    def describe_execution(self, executionArn):  # noqa: N803 — boto3 signature
        self.described.append(executionArn)
        return {"status": self.status}


@pytest.fixture
def active(monkeypatch):
    with mock_aws():
        t = _wf_table()
        from backend.src.api.v1 import workflow
        from backend.src.core import aws

        sfn = _Sfn()
        monkeypatch.setattr(workflow, "get_table", lambda: t)
        monkeypatch.setattr(aws, "client", lambda service, region=None: sfn)
        for app_id, status in (("APP-RUN", "RUNNING"), ("APP-DONE", "COMPLETED"), ("APP-FAIL", "FAILED"),
                               ("APP-CXL", "CANCELLED"), ("APP-TO", "TIMED_OUT")):
            t.put_item(Item={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE", "GSI1PK": "TYPE#WORKFLOW",
                             "GSI1SK": app_id, "app_id": app_id, "status": status,
                             "execution_arn": f"arn:exec:{app_id}"})
        yield workflow, sfn


def test_active_skips_describe_for_terminal_records(active):
    workflow, sfn = active
    data = workflow.get_active_workflows().data
    assert sfn.described == ["arn:exec:APP-RUN"]  # terminal records are not described
    by_app = {d["app_id"]: d["status"] for d in data}
    assert by_app == {"APP-RUN": "RUNNING", "APP-DONE": "COMPLETED", "APP-FAIL": "FAILED",
                      "APP-CXL": "CANCELLED", "APP-TO": "TIMED_OUT"}


def test_active_is_cached_briefly_and_invalidated(active, monkeypatch):
    workflow, sfn = active
    monkeypatch.setattr(workflow.settings, "active_workflows_cache_seconds", 10)
    workflow.get_active_workflows()
    workflow.get_active_workflows()
    assert len(sfn.described) == 1  # second call served from the cache
    workflow.invalidate_active_workflows_cache()
    workflow.get_active_workflows()
    assert len(sfn.described) == 2


def test_active_reflects_live_sfn_status(active):
    workflow, sfn = active
    sfn.status = "SUCCEEDED"
    by_app = {d["app_id"]: d["status"] for d in workflow.get_active_workflows().data}
    assert by_app["APP-RUN"] == "COMPLETED"


# ── pagination ──────────────────────────────────────────────────────────────


class _PagedTable:
    """Returns its items N per page with LastEvaluatedKey, like DynamoDB at 1 MB."""

    def __init__(self, items, page=2):
        self.items, self.page, self.calls = items, page, 0

    def query(self, **kw):
        self.calls += 1
        start = kw.get("ExclusiveStartKey", {}).get("i", 0)
        chunk = self.items[start:start + self.page]
        resp = {"Items": chunk}
        if start + self.page < len(self.items):
            resp["LastEvaluatedKey"] = {"i": start + self.page}
        return resp


def test_query_all_follows_last_evaluated_key():
    from backend.src.db.helpers import query_all

    t = _PagedTable([{"n": i} for i in range(5)], page=2)
    assert [i["n"] for i in query_all(t, IndexName="GSI1")["Items"]] == [0, 1, 2, 3, 4]
    assert t.calls == 3
    assert len(query_all(_PagedTable([{"n": i} for i in range(5)]), max_items=3)["Items"]) == 3


def test_machine_connections_keep_order_and_duplicates():
    from backend.src.db.accessors import list_machine_connections

    class T:
        def query(self, **kw):
            mid = kw["KeyConditionExpression"]._values[0]._values[1].replace("MACHINE#", "")
            return {"Items": [{"src": mid, "n": 1}, {"src": mid, "n": 2}]}

    out = list_machine_connections(["m1", "m2", "m1"], table=T())
    assert [(c["src"], c["n"]) for c in out] == [("m1", 1), ("m1", 2), ("m2", 1), ("m2", 2), ("m1", 1), ("m1", 2)]


def test_governance_paginates_list_executions(monkeypatch):
    from backend.src.api.v1 import governance

    class Sfn:
        def __init__(self):
            self.pages = 0

        def list_executions(self, **kw):
            self.pages += 1
            if kw.get("nextToken") == "p2":
                return {"executions": [{"name": "APP-1-2", "executionArn": "e2", "status": "SUCCEEDED"}]}
            return {"executions": [{"name": "APP-1-1", "executionArn": "e1", "status": "SUCCEEDED"},
                                   {"name": "APP-10-1", "executionArn": "e3", "status": "SUCCEEDED"}],
                    "nextToken": "p2"}

        def get_execution_history(self, **kw):
            return {"events": []}

    sfn = Sfn()
    monkeypatch.setattr(governance, "_sfn_client", lambda: sfn)
    monkeypatch.setattr(governance, "_state_machine_arn", lambda: "arn:sm")
    executions, note = governance._collect_executions("APP-1")
    assert note is None and sfn.pages == 2
    assert [e["execution"] for e in executions] == ["APP-1-1", "APP-1-2"]  # APP-10 is not APP-1


# ── JWKS refresh rate limit ─────────────────────────────────────────────────


def test_unknown_kid_refetches_jwks_at_most_once_per_window(monkeypatch):
    from jose import JWTError

    from backend.src.auth import cognito

    fetches = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": [{"kid": "known"}]}

    monkeypatch.setattr(cognito.httpx, "get", lambda *a, **k: fetches.append(1) or Resp())
    monkeypatch.setattr(cognito.jwt, "get_unverified_headers", lambda token: {"kid": token})
    monkeypatch.setattr(cognito.settings, "jwks_min_refresh_seconds", 60)
    monkeypatch.setattr(cognito.settings, "aws_region", "us-east-1")
    monkeypatch.setattr(cognito.settings, "cognito_user_pool_id", "us-east-1_pool")
    cognito.reset_jwks_cache()
    v = cognito.CognitoJWTValidator()
    try:
        assert v._get_signing_key("known") == {"kid": "known"}
        assert len(fetches) == 1
        for _ in range(5):
            with pytest.raises(JWTError):
                v._get_signing_key("bogus")
        assert len(fetches) == 2  # one forced refresh, then rate-limited
        monkeypatch.setattr(cognito, "_last_forced_refresh", time.time() - 61)
        with pytest.raises(JWTError):
            v._get_signing_key("bogus")
        assert len(fetches) == 3
    finally:
        cognito.reset_jwks_cache()


# ── worker ──────────────────────────────────────────────────────────────────


def test_state_only_root_fails_closed_without_prefix(tmp_path, monkeypatch):
    from backend.src.worker import deploy_runner as dr

    monkeypatch.setattr(dr.settings, "project_prefix", "")
    with pytest.raises(dr.DeployError):
        dr.state_only_root({"target_region": "us-east-1"}, "APP-X", tmp_path, "123456789012")
    assert not (tmp_path / "main.tf").exists()


def test_state_only_root_naming_unchanged(tmp_path, monkeypatch):
    from backend.src.worker import deploy_runner as dr

    monkeypatch.setattr(dr.settings, "project_prefix", "amf")
    dr.state_only_root({"target_region": "us-west-2"}, "APP-X", tmp_path, "123456789012")
    tf = (tmp_path / "main.tf").read_text()
    assert 'bucket         = "amf-tfstate-123456789012-us-west-2"' in tf
    assert 'dynamodb_table = "amf-tfstate-lock"' in tf
    assert 'aws = { source = "hashicorp/aws", version = "~> 5.0" }' in tf


def test_dockerfile_base_registry_prefixes_every_from(monkeypatch):
    from backend.src.worker import dockerfile_gen as dockerfile

    assert dockerfile._render("java-maven") == dockerfile._TEMPLATES["java-maven"]
    monkeypatch.setattr(dockerfile.settings, "dockerfile_base_registry", "123.dkr.ecr.us-east-1.amazonaws.com/mirror")
    out = dockerfile._render("java-maven")
    assert "FROM 123.dkr.ecr.us-east-1.amazonaws.com/mirror/maven:3.9-eclipse-temurin-21 AS build" in out
    assert "FROM 123.dkr.ecr.us-east-1.amazonaws.com/mirror/eclipse-temurin:21-jre" in out


def test_risk_eosl_horizon_is_a_parameter():
    from backend.src.core.risk_scoring import calculate_risk_score

    def eosl(result):
        return next(c for c in result["categories"] if c["category"] == "Risk (EOSL)")

    assert eosl(calculate_risk_score({"eosl_date": "2033"}))["score"] == 0
    assert eosl(calculate_risk_score({"eosl_date": "2033"}, eosl_horizon_year=2035))["score"] == 13


def test_aws_client_factory_caches_per_service_and_region():
    from backend.src.core import aws

    aws.clear_cache()
    a = aws.client("s3")
    assert aws.client("s3") is a
    other = "eu-west-1" if settings.aws_region != "eu-west-1" else "us-west-2"
    assert aws.client("s3", other) is not a
    cfg = a.meta.config
    assert cfg.connect_timeout == settings.aws_connect_timeout_seconds
    assert cfg.read_timeout == settings.aws_read_timeout_seconds
    assert cfg.retries["mode"] == "standard"


def test_default_table_name_matches_terraform_for_a_hyphenated_prefix(monkeypatch):
    """Terraform names the table replace(project_prefix, "-", "_") + "_portfolio"."""
    from backend.src.config import Settings

    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    assert Settings(project_prefix="my-env", dynamodb_table="").dynamodb_table == "my_env_portfolio"
    assert Settings(project_prefix="uat", dynamodb_table="").dynamodb_table == "uat_portfolio"
