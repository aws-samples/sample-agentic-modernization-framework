"""Plain-assert tests for tool_documents tool resolution (run: python3 test_tool_documents.py).

No pytest / AWS calls required — stubs boto3 so the module imports and its
ToolRegistry tool-name resolution + shape inference can be exercised (R30.3).
"""
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DYNAMODB_TABLE", "test-table")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Stub boto3 so importing the module doesn't require AWS.
if "boto3" not in sys.modules:
    b = types.ModuleType("boto3")
    b.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: None)
    b.client = lambda *a, **k: None
    sys.modules["boto3"] = b
    sys.modules["boto3.dynamodb"] = types.ModuleType("boto3.dynamodb")
    cond = types.ModuleType("boto3.dynamodb.conditions")
    cond.Key = lambda *a, **k: None
    sys.modules["boto3.dynamodb.conditions"] = cond

import tool_documents as td  # noqa: E402
from agent_tools import resolve_tool_name  # noqa: E402


def _resolve(event):
    """Mirror the registry's resolution: explicit name, else shape inference."""
    name = resolve_tool_name(event) or td.registry._infer_from_shape(event.get("arguments", event))
    return name


def check(name, event, expected):
    got = _resolve(event)
    assert got == expected, f"{name}: expected {expected!r}, got {got!r}"
    print(f"  OK  {name} -> {got}")


def main():
    print("tool_documents registry resolution (R30.3 argument inference):")
    check("explicit tool_name wins", {"tool_name": "get_design_document", "content": "x", "version": "v1"}, "get_design_document")
    check("store_design_document by content+version", {"content": "# doc", "version": "v2", "app_id": "APP-1"}, "store_design_document")
    check("store_analysis_result by result+analysis_type", {"result": {}, "analysis_type": "assessment-summary", "app_id": "APP-1"}, "store_analysis_result")
    check("get_analysis_result by analysis_type", {"analysis_type": "comprehensive-codebase-analysis", "app_id": "APP-1"}, "get_analysis_result")
    check("get_design_document by app_id only", {"app_id": "APP-1"}, "get_design_document")
    check("registry exports all 4 tools", {"tool_name": "store_design_document"}, "store_design_document")
    assert set(td.registry.names()) == {
        "store_design_document", "get_design_document", "store_analysis_result", "get_analysis_result",
        "compute_risk_score",
    }
    print("ALL PASS")


if __name__ == "__main__":
    main()
