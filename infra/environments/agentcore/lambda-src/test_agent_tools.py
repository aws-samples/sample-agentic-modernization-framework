"""
Tests for the composability framework (agent_tools.ToolRegistry).

Runnable in the base env (no pytest, no AWS):
    python3 infra/environments/agentcore/lambda-src/test_agent_tools.py
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_tools  # noqa: E402
from agent_tools import ToolRegistry, invoked_by_agent, json_safe, resolve_tool_name  # noqa: E402


def test_registration_and_dispatch():
    reg = ToolRegistry("t")

    @reg.tool("echo", description="echo", schema={"msg": {"type": "string", "required": True}})
    def echo(args):
        return {"msg": args["msg"]}

    assert reg.names() == ["echo"]
    # explicit tool_name
    assert reg.dispatch({"tool_name": "echo", "arguments": {"msg": "hi"}}) == {"msg": "hi"}
    # missing required arg → error envelope
    out = reg.dispatch({"tool_name": "echo", "arguments": {}})
    assert "error" in out and "msg" in out["error"]
    # unknown tool → error listing available
    out = reg.dispatch({"tool_name": "nope", "arguments": {}})
    assert "Unknown tool" in out["error"] and "echo" in out["error"]
    print("  ok: registration + dispatch + validation + unknown")


def test_duplicate_registration():
    reg = ToolRegistry("t")

    @reg.tool("x")
    def x(args):
        return {}

    try:
        @reg.tool("x")
        def x2(args):
            return {}
        raise AssertionError("expected ValueError on duplicate")
    except ValueError:
        pass
    print("  ok: duplicate registration rejected")


def test_name_resolution_variants():
    # client-context targetName___toolName
    class Ctx:
        class client_context:
            custom = {"bedrockAgentCoreToolName": "portfolio___get_app_context"}

    assert resolve_tool_name({}, Ctx) == "get_app_context"
    assert resolve_tool_name({"name": "recommend_strategy"}) == "recommend_strategy"
    assert resolve_tool_name({"toolName": "x"}) == "x"
    assert resolve_tool_name({}) == ""
    print("  ok: tool-name resolution variants")


def test_trace_records_actual_caller(monkeypatch=None):
    """Gateway (Harness) calls are 'agent'; Step Functions payload calls are 'workflow'."""
    class Ctx:
        class client_context:
            custom = {"bedrockAgentCoreToolName": "documents___compute_risk_score"}

    assert invoked_by_agent(Ctx) is True
    assert invoked_by_agent(None) is False

    reg = ToolRegistry("documents")

    @reg.tool("compute_risk_score", schema={"app_id": {"type": "string", "required": True}})
    def score(args):
        return {"score": 42}

    seen = []
    original = agent_tools._record_tool_call
    agent_tools._record_tool_call = lambda *a, **k: seen.append(a[-1] if len(a) > 5 else k.get("caller"))
    try:
        reg.dispatch({"arguments": {"app_id": "A"}}, Ctx)                      # via Gateway
        reg.dispatch({"tool_name": "compute_risk_score", "app_id": "A"})       # Score_Discovery
    finally:
        agent_tools._record_tool_call = original
    assert seen == ["agent", "workflow"]
    print("  ok: trace caller agent vs workflow")


def test_shape_inference_fallback():
    reg = ToolRegistry("docs")

    @reg.tool("store_design_document", infer=lambda a: "content" in a and "version" in a)
    def store(args):
        return {"stored": True}

    @reg.tool("get_analysis_result", infer=lambda a: "analysis_type" in a and "result" not in a)
    def get(args):
        return {"got": True}

    # no explicit name → inferred from arg shape
    assert reg.dispatch({"content": "x", "version": "v1"}) == {"stored": True}
    assert reg.dispatch({"analysis_type": "assessment"}) == {"got": True}
    print("  ok: shape-inference fallback")


def test_tool_exception_is_enveloped():
    reg = ToolRegistry("t")

    @reg.tool("boom")
    def boom(args):
        raise RuntimeError("kaboom")

    out = reg.dispatch({"tool_name": "boom", "arguments": {}})
    assert "error" in out and "kaboom" in out["error"]
    print("  ok: tool exception returned as error envelope (no stack leak)")


def test_json_safe_decimal():
    assert json_safe({"n": Decimal("3.5"), "i": Decimal("4")}) == {"n": 3.5, "i": 4}
    print("  ok: json_safe decimal coercion")


def test_export_schema():
    reg = ToolRegistry("t")

    @reg.tool("f", description="d", schema={"a": {"type": "string", "required": True}, "b": {"type": "string"}})
    def f(args):
        return {}

    schema = reg.export_schema()
    assert schema[0]["name"] == "f"
    assert schema[0]["inputSchema"]["required"] == ["a"]
    assert "a" in schema[0]["inputSchema"]["properties"]
    print("  ok: export_schema shape")


def test_portfolio_tools_registered():
    # Import the real tool module with a stubbed boto3 so registration runs.
    os.environ["DYNAMODB_TABLE"] = "test-table"
    import types

    if "boto3" not in sys.modules:
        b = types.ModuleType("boto3")
        b.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: None)
        b.client = lambda *a, **k: None
        sys.modules["boto3"] = b
        cond = types.ModuleType("boto3.dynamodb.conditions")
        cond.Key = lambda *a, **k: None
        sys.modules["boto3.dynamodb"] = types.ModuleType("boto3.dynamodb")
        sys.modules["boto3.dynamodb.conditions"] = cond

    import tool_portfolio

    expected = {"get_app_context", "update_app_field", "search_machines",
                "assign_machines_to_app", "get_portfolio_summary", "add_owner"}
    assert set(tool_portfolio.registry.names()) == expected
    print("  ok: tool_portfolio registers all 6 granular tools")


def _stub_boto3():
    import types
    if "boto3" not in sys.modules:
        b = types.ModuleType("boto3")
        b.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: None)
        b.client = lambda *a, **k: None
        sys.modules["boto3"] = b
        sys.modules["boto3.dynamodb"] = types.ModuleType("boto3.dynamodb")
        cond = types.ModuleType("boto3.dynamodb.conditions")
        cond.Key = lambda *a, **k: None
        sys.modules["boto3.dynamodb.conditions"] = cond


def test_all_tool_lambdas_register():
    os.environ.setdefault("DYNAMODB_TABLE", "test-table")
    _stub_boto3()
    import importlib

    # tool_documents imports risk_scoring, which lambdas.tf packages from backend/src/core.
    core = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../../backend/src/core")
    if core not in sys.path:
        sys.path.append(core)

    expected = {
        "tool_portfolio": {"get_app_context", "update_app_field", "search_machines",
                           "assign_machines_to_app", "get_portfolio_summary", "add_owner"},
        "tool_strategy": {"recommend_strategy", "calculate_completeness"},
        "tool_planning": {"generate_wave_plan", "store_wave_plan", "get_wave_plan", "store_migration_plan"},
        "tool_documents": {"store_design_document", "get_design_document",
                           "store_analysis_result", "get_analysis_result", "compute_risk_score"},
        "tool_execution": {"start_code_analysis", "start_code_transformation",
                           "get_job_status", "provision_infrastructure", "generate_cicd",
                           "commit_cicd_to_repo", "run_terraform_plan",
                           "validate_deployment",
                           # ASL/API-only (never Gateway-exposed: the agent must not deploy)
                           "run_ci_fix", "deploy_infrastructure", "destroy_infrastructure"},
    }
    for mod_name, tools in expected.items():
        # mod_name iterates a literal in-file list of our own tool modules (test-only, no external input).
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        mod = importlib.import_module(mod_name)
        assert set(mod.registry.names()) == tools, f"{mod_name}: {mod.registry.names()}"
        # every tool exports a schema entry
        assert len(mod.registry.export_schema()) == len(tools)
        print(f"  ok: {mod_name} registers {len(tools)} tools + schema")


def main():
    print("test_agent_tools:")
    test_registration_and_dispatch()
    test_duplicate_registration()
    test_name_resolution_variants()
    test_shape_inference_fallback()
    test_tool_exception_is_enveloped()
    test_json_safe_decimal()
    test_export_schema()
    test_portfolio_tools_registered()
    test_all_tool_lambdas_register()
    print("ALL PASS")


if __name__ == "__main__":
    main()


def test_every_registered_tool_is_the_function_of_the_same_name():
    """Guards against a helper sitting between @registry.tool and its def."""
    import importlib

    for mod_name in ("tool_portfolio", "tool_strategy", "tool_planning", "tool_documents", "tool_execution"):
        # mod_name iterates a literal in-file list of our own tool modules (test-only, no external input).
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        mod = importlib.import_module(mod_name)
        for name, spec in mod.registry._tools.items():
            assert spec["fn"].__name__ == name, f"{mod_name}: tool {name!r} is bound to {spec['fn'].__name__}()"
