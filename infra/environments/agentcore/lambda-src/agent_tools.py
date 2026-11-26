"""
Composability framework for AgentCore Gateway tools.

One granular tool = one decorated function with an explicit input schema. The
Lambda that hosts a set of tools is just the deployment unit; each *tool* is the
composable, independently-described capability.

Usage in a tool Lambda:

    from agent_tools import ToolRegistry

    registry = ToolRegistry("portfolio")

    @registry.tool(
        "get_app_context",
        description="Return the full application context.",
        schema={"app_id": {"type": "string", "required": True}},
    )
    def get_app_context(args):
        ...

    def handler(event, ctx=None):
        return registry.dispatch(event, ctx)

The registry centralizes:
  • tool registration (name → function + schema),
  • Gateway tool-name resolution (explicit field, client-context, or shape),
  • uniform argument extraction and error envelopes,
  • schema export for the Terraform Gateway target definitions and for tests.
"""

from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Callable

logger = logging.getLogger()
if not logger.handlers:
    logging.basicConfig(level="INFO")

_TOOLCALL_TABLE = None


def _toolcall_table():
    """Lazily resolve the DynamoDB table used to persist tool-call traces."""
    global _TOOLCALL_TABLE
    if _TOOLCALL_TABLE is None:
        name = os.environ.get("DYNAMODB_TABLE", "")
        if not name:
            return None
        import boto3

        region = os.environ["AWS_REGION"]  # set by the Lambda runtime
        _TOOLCALL_TABLE = boto3.resource("dynamodb", region_name=region).Table(name)
    return _TOOLCALL_TABLE


def _trunc_json(value: Any, limit: int = 6000) -> str:
    """JSON-encode (Decimal-safe) and truncate — kept well under the DDB item limit."""
    try:
        s = json.dumps(json_safe(value), default=str)
    except Exception:
        s = str(value)
    return s if len(s) <= limit else s[:limit] + " …(truncated)"


def _record_tool_call(
    target: str, tool: str, args: dict, result: Any, elapsed_ms: int, caller: str = "workflow"
) -> None:
    """Persist a per-phase tool-call trace so the UI can show tool logs in-app.

    Best-effort: never raises. Scoped to workflow tools that carry an app_id.
    """
    app_id = args.get("app_id") or args.get("appId")
    if not app_id:
        return
    table = _toolcall_table()
    if table is None:
        return
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int((time.time() % 1) * 1e6):06d}Z"
        phase = ""
        try:
            wf = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"}).get("Item", {}) or {}
            phase = wf.get("current_phase", "") or ""
        except Exception as _err:
            logger.debug("best-effort step ignored: %s", _err)
        is_err = isinstance(result, dict) and "error" in result
        table.put_item(
            Item={
                "PK": f"APP#{app_id}",
                "SK": f"TOOLCALL#{phase}#{now}#{tool}",
                "GSI1PK": "TYPE#TOOLCALL",
                "GSI1SK": f"{app_id}#{now}",
                "app_id": app_id,
                "phase": phase,
                "target": target,
                "tool": tool,
                "caller": caller,
                "status": "error" if is_err else "ok",
                "latency_ms": elapsed_ms,
                "input": _trunc_json(args),
                "output": _trunc_json(result),
                "at": now,
                "ttl": int(time.time()) + 30 * 24 * 3600,
            }
        )
    except Exception as exc:
        logger.warning("tool-call trace write failed (%s/%s): %s", target, tool, exc)


def query_all(table: Any, max_items: int | None = None, **kwargs: Any) -> list:
    """``table.query(**kwargs)`` following ``LastEvaluatedKey`` to the end.

    A single DynamoDB Query page stops at 1 MB, so an unpaginated GSI1/partition
    read silently truncates once a type grows. ``max_items`` bounds the total
    returned (``Limit`` in ``kwargs`` stays the per-page size).
    """
    items: list = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if max_items is not None and len(items) >= max_items:
            return items[:max_items]
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs = {**kwargs, "ExclusiveStartKey": last}


class DecimalEncoder(json.JSONEncoder):
    """Serialize DynamoDB Decimal values as plain numbers."""

    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == int(o) else float(o)
        return super().default(o)


def json_safe(value: Any) -> Any:
    """Round-trip a value through the Decimal-aware encoder to make it JSON-safe."""
    return json.loads(json.dumps(value, cls=DecimalEncoder))


def resolve_tool_name(event: dict, ctx: Any = None) -> str:
    """Resolve the invoked tool name from any of the shapes the Gateway uses.

    Precedence: explicit event field → AgentCore client-context
    (`targetName___toolName`) → empty (caller may then infer from arg shape).
    """
    tool = event.get("tool_name") or event.get("name") or event.get("toolName") or event.get("tool") or ""
    if tool:
        return tool
    if ctx is not None and hasattr(ctx, "client_context"):
        try:
            custom = ctx.client_context.custom or {}
            full = custom.get("bedrockAgentCoreToolName", "")
            return full.split("___", 1)[1] if "___" in full else full
        except Exception:
            return ""
    return ""


def invoked_by_agent(ctx: Any = None) -> bool:
    """True when the MCP Gateway (i.e. the Harness) invoked the tool.

    The Gateway names the tool in the Lambda client context; Step Functions and
    other direct callers pass `tool_name` in the payload and have no such context.
    """
    try:
        return bool((ctx.client_context.custom or {}).get("bedrockAgentCoreToolName"))
    except Exception:
        return False


def extract_arguments(event: dict) -> dict:
    """Pull the tool arguments out of the event envelope."""
    args = event.get("arguments", event.get("input", event))
    return args if isinstance(args, dict) else {}


class ToolRegistry:
    """A per-Lambda registry of granular tools."""

    def __init__(self, target_name: str):
        self.target_name = target_name
        self._tools: dict[str, dict] = {}

    def tool(
        self,
        name: str,
        *,
        description: str = "",
        schema: dict | None = None,
        infer: Callable[[dict], bool] | None = None,
    ):
        """Register a granular tool.

        - `schema`: {arg_name: {"type": ..., "required": bool, "description": ...}}
        - `infer`: optional predicate(args) → bool used only as a last-resort
          fallback when the Gateway does not supply a tool name.
        """

        def decorator(fn: Callable[[dict], Any]):
            if name in self._tools:
                raise ValueError(f"Tool '{name}' already registered on target '{self.target_name}'")
            self._tools[name] = {
                "fn": fn,
                "description": description or (fn.__doc__ or "").strip().split("\n")[0],
                "schema": schema or {},
                "infer": infer,
            }
            return fn

        return decorator

    # ── Dispatch ──────────────────────────────────────────────────────

    def _infer_from_shape(self, args: dict) -> str:
        for name, spec in self._tools.items():
            pred = spec.get("infer")
            if pred is not None:
                try:
                    if pred(args):
                        return name
                except Exception as _err:
                    logger.debug("best-effort step skipped: %s", _err)
                    continue
        return ""

    def validate(self, name: str, args: dict) -> str | None:
        """Return an error message if a required arg is missing, else None."""
        for arg, spec in self._tools[name]["schema"].items():
            if spec.get("required") and not args.get(arg):
                return f"Missing required argument '{arg}' for tool '{name}'"
        return None

    def dispatch(self, event: dict, ctx: Any = None) -> dict:
        """Resolve, validate, and invoke a tool; return a JSON-safe envelope."""
        args = extract_arguments(event)
        name = resolve_tool_name(event, ctx) or self._infer_from_shape(args)

        if name not in self._tools:
            return {"error": f"Unknown tool: {name!r}. Available: {sorted(self._tools)}"}

        missing = self.validate(name, args)
        if missing:
            return {"error": missing}

        logger.info("%s tool=%s args=%s", self.target_name, name, sorted(args.keys()))
        started = time.time()
        try:
            result = self._tools[name]["fn"](args)
        except Exception as exc:  # tools should be resilient; never leak a stack to the model
            logger.exception("Tool '%s' failed", name)
            result = {"error": f"Tool '{name}' failed: {exc}"}
        elapsed_ms = int((time.time() - started) * 1000)
        # Persist a tool-call trace for in-app tool logs (best-effort).
        caller = "agent" if invoked_by_agent(ctx) else "workflow"
        _record_tool_call(self.target_name, name, args, result, elapsed_ms, caller)
        return json_safe(result)

    # ── Introspection (for Terraform schema + tests) ──────────────────

    def names(self) -> list[str]:
        return sorted(self._tools)

    def export_schema(self) -> list[dict]:
        """Export tool definitions in a Gateway-target-friendly shape."""
        out = []
        for name in self.names():
            spec = self._tools[name]
            props = {
                arg: {k: v for k, v in meta.items() if k != "required"}
                for arg, meta in spec["schema"].items()
            }
            required = [a for a, meta in spec["schema"].items() if meta.get("required")]
            out.append({
                "name": name,
                "description": spec["description"],
                "inputSchema": {"type": "object", "properties": props, "required": required},
            })
        return out
