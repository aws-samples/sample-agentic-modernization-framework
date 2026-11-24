"""Terraform-set env tunables, paginated reads, fail-closed prefix."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("DYNAMODB_TABLE", "test_table")

import agent_tools  # noqa: E402
import tool_documents  # noqa: E402
import tool_execution  # noqa: E402
import tool_planning  # noqa: E402
import validation_probe  # noqa: E402


def _fresh(module: str, monkeypatch, **env):
    """Import a private copy of a tool module with `env` set (module-load config)."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location(f"{module}_envtest", HERE / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PagedTable:
    """Fake DynamoDB table: serves `pages` per partition, following ExclusiveStartKey."""

    def __init__(self, pages_by_key):
        self.pages_by_key = pages_by_key
        self.calls = []

    @staticmethod
    def _key(kw):
        expr = kw["KeyConditionExpression"]
        # Key("PK").eq(x) & Key("SK").begins_with(y) -> the first operand's value
        while expr.get_expression()["values"] and hasattr(expr.get_expression()["values"][0], "get_expression"):
            expr = expr.get_expression()["values"][0]
        return expr.get_expression()["values"][1]

    def query(self, **kw):
        self.calls.append(kw)
        pages = self.pages_by_key.get(self._key(kw), [[]])
        i = kw.get("ExclusiveStartKey", {}).get("page", 0)
        resp = {"Items": pages[i]}
        if i + 1 < len(pages):
            resp["LastEvaluatedKey"] = {"page": i + 1}
        return resp


# ── pagination ────────────────────────────────────────────────────────────────

def test_query_all_follows_last_evaluated_key():
    from boto3.dynamodb.conditions import Key

    t = PagedTable({"TYPE#APP": [[{"n": 1}, {"n": 2}], [{"n": 3}], [{"n": 4}]]})
    items = agent_tools.query_all(t, IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TYPE#APP"))
    assert [i["n"] for i in items] == [1, 2, 3, 4]
    assert len(t.calls) == 3 and t.calls[2]["ExclusiveStartKey"] == {"page": 2}


def test_query_all_stops_at_max_items():
    from boto3.dynamodb.conditions import Key

    t = PagedTable({"TYPE#MACHINE": [[{"n": 1}, {"n": 2}], [{"n": 3}, {"n": 4}], [{"n": 5}]]})
    items = agent_tools.query_all(t, max_items=3, IndexName="GSI1",
                                  KeyConditionExpression=Key("GSI1PK").eq("TYPE#MACHINE"))
    assert [i["n"] for i in items] == [1, 2, 3]
    assert len(t.calls) == 2  # never read the third page


def test_wave_dependencies_read_every_conn_page_and_map_target_machines(monkeypatch):
    apps = [{"app_id": "A", "machine_ids": ["m1", "m2"]}, {"app_id": "B", "machine_ids": ["m3"]},
            {"app_id": "C", "machine_ids": ["m4"]}]
    t = PagedTable({
        # m1's second page holds the A->B edge (an unpaginated read would miss it).
        "MACHINE#m1": [[{"target_machine_id": "m2"}], [{"target_machine_id": "m3"}]],
        "MACHINE#m3": [[{"target_app_id": "C"}]],
    })
    monkeypatch.setattr(tool_planning, "table", t)
    from wave_planner import build_dependencies  # the shared planner module
    deps, _shared = build_dependencies(apps, tool_planning._machine_connections(apps))
    assert dict(deps) == {"A": {"B"}, "B": {"C"}}  # A->A (m1->m2) is internal, dropped
    keys = [PagedTable._key(c) for c in t.calls]
    assert keys.count("MACHINE#m1") == 2 and sorted(set(keys)) == ["MACHINE#m1", "MACHINE#m2", "MACHINE#m3",
                                                                    "MACHINE#m4"]


def test_atx_job_lookup_reads_all_pages(monkeypatch):
    jobs = [[{"app_id": "APP-1", "mode": "code-analysis", "status": "COMPLETED", "job_id": "old",
              "created_at": "2026-01-01"}],
            [{"app_id": "APP-1", "mode": "code-analysis", "status": "COMPLETED", "job_id": "new",
              "created_at": "2026-06-01"}]]
    monkeypatch.setattr(tool_documents, "table", PagedTable({"TYPE#JOB": jobs}))
    assert tool_documents._atx_job_for("APP-1", None)["job_id"] == "new"


def test_find_http_api_follows_next_token():
    class Apigw:
        def __init__(self):
            self.calls = []

        def get_apis(self, **kw):
            self.calls.append(kw)
            if not kw:
                return {"Items": [{"Name": "other-api"}], "NextToken": "t1"}
            return {"Items": [{"Name": "APP-X-api", "ApiId": "a2"}]}

    gw = Apigw()
    assert validation_probe._find_http_api(gw, "APP-X-api")["ApiId"] == "a2"
    assert gw.calls == [{}, {"NextToken": "t1"}]
    assert validation_probe._find_http_api(gw, "missing-api") is None


# ── fail closed on an empty PROJECT_PREFIX ────────────────────────────────────

def test_provision_infrastructure_fails_closed_without_prefix(monkeypatch):
    statuses = []
    monkeypatch.setattr(tool_execution, "PROJECT_PREFIX", "")
    monkeypatch.setattr(tool_execution, "_set_exec_status", lambda *a: statuses.append(a))

    class NoTable:
        def get_item(self, **_):
            raise AssertionError("must fail before reading the app")

    monkeypatch.setattr(tool_execution, "table", NoTable())
    out = tool_execution.provision_infrastructure({"app_id": "APP-1"})
    assert out["status"] == "error" and "PROJECT_PREFIX" in out["message"]
    assert statuses == [("APP-1", "exec_infra_status", "error")]


# ── env overrides (defaults = the original literals) ──────────────────────────

def test_tfstate_names_default_and_override(monkeypatch):
    assert tool_execution._tfstate_names("amf", "123456789012", "us-east-1") == (
        "amf-tfstate-123456789012-us-east-1", "amf-tfstate-lock")
    mod = _fresh("tool_execution", monkeypatch, APP_TFSTATE_BUCKET_PATTERN="{prefix}-state-{region}-{account}",
                 APP_TFSTATE_LOCK_PATTERN="{prefix}-locks")
    assert mod._tfstate_names("uat", "1", "eu-west-1") == ("uat-state-eu-west-1-1", "uat-locks")


def test_multi_az_sla_and_runtask_overrides(monkeypatch):
    assert tool_execution._db_multi_az({"uptime_sla": "99.9"})[0] is False
    mod = _fresh("tool_execution", monkeypatch, MULTI_AZ_SLA_THRESHOLD="99.9", RUNTASK_MAX_ATTEMPTS="2",
                 COVERAGE_THRESHOLD_DEFAULT="80")
    flag, reason = mod._db_multi_az({"uptime_sla": "99.9"})
    assert flag is True and "99.9%" in reason
    assert mod.RUNTASK_MAX_ATTEMPTS == 2 and mod.COVERAGE_THRESHOLD_DEFAULT == 80


def test_strategy_tunables_override(monkeypatch):
    default = _fresh("tool_strategy", monkeypatch)
    assert default._adjust_for_completeness(70, 90) == 80
    assert default._adjust_for_completeness(50, 10) == 40
    mod = _fresh("tool_strategy", monkeypatch, STRATEGY_COMPLETENESS_BONUS="20",
                 STRATEGY_COMPLETENESS_MAX_CONFIDENCE="85", STRATEGY_CONFIDENCE_FLOOR="45")
    assert mod._adjust_for_completeness(70, 90) == 85
    assert mod._adjust_for_completeness(50, 10) == 45


@pytest.mark.parametrize("module", ["tool_execution", "tool_planning"])
def test_region_comes_from_the_runtime(module, monkeypatch):
    mod = _fresh(module, monkeypatch, AWS_REGION="eu-central-1")
    assert mod.REGION == "eu-central-1"


def test_lambda_tool_and_api_share_one_planner(monkeypatch):
    """The Harness tool returns exactly the API planner's plan for the same estate."""
    import wave_planner
    apps = [{"app_id": "A", "app_name": "A", "migration_strategy": "replatform", "tier": "tier_3"},
            {"app_id": "B", "app_name": "B", "migration_strategy": "replatform", "tier": "tier_2", "depends_on": ["A"]},
            {"app_id": "R", "app_name": "R", "migration_strategy": "retire", "tier": "tier_1"},
            {"app_id": "U", "app_name": "U", "migration_strategy": "", "tier": "tier_3"}]
    monkeypatch.setattr(tool_planning, "query_all", lambda *a, **k: apps)
    via_tool = tool_planning.generate_wave_plan({})
    direct = wave_planner.generate_wave_plan(apps, [], config=wave_planner.PlannerConfig())
    assert via_tool["waves"] == direct["waves"] and via_tool["not_ready"] == direct["not_ready"]
    assert tool_planning._plan is wave_planner.generate_wave_plan
