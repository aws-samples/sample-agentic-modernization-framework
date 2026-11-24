"""Gateway Tool target: Portfolio — App/Machine/Repo/Owner operations.

Each capability is a granular, individually-described tool registered on the
shared ToolRegistry (composability framework). The Lambda is only the host.
"""
import json
import os

import boto3
from agent_tools import DecimalEncoder, ToolRegistry, json_safe, query_all
from boto3.dynamodb.conditions import Key

TABLE = os.environ["DYNAMODB_TABLE"]
REGION = os.environ["AWS_REGION"]  # set by the Lambda runtime
ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(TABLE)

registry = ToolRegistry("portfolio")

# Read limits (env vars set in lambdas.tf; defaults are the original values).
# get_app_context: CONN# records read, and how many are returned to the agent.
APP_CONTEXT_CONN_READ_LIMIT = int(os.environ.get("APP_CONTEXT_CONN_READ_LIMIT", "50"))
APP_CONTEXT_MAX_CONNECTIONS = int(os.environ.get("APP_CONTEXT_MAX_CONNECTIONS", "20"))
# search_machines: machines read (paginated up to this cap) and matches returned.
SEARCH_MACHINES_MAX_SCANNED = int(os.environ.get("SEARCH_MACHINES_MAX_SCANNED", "200"))
SEARCH_MACHINES_MAX_RESULTS = int(os.environ.get("SEARCH_MACHINES_MAX_RESULTS", "20"))

_INTERNAL = ("PK", "SK", "GSI1PK", "GSI1SK")

# update_app_field is agent-callable (Gateway) and the agent is prompt-injectable (repo
# content, docs, chat), so it may only write these META fields.
# Source: what the ASL prompts ask for (status, language/framework/build_tool,
# exec_database_status), the discovery skill (language, framework, runtime) and the chat
# assistant's job of filling the completeness fields (core/completeness.py) that have no
# dedicated tool. Everything else is denied — notably migration_strategy (recommend_strategy /
# the gates), target_compute/target_database/cicd_target, target_account/target_region/vpc_cidr,
# expose_publicly, deploy_*/approval fields, validation_write_tests, machine_ids
# (assign_machines_to_app), dynamodb_* wiring and every run-derived field
# (api/v1/workflow.RUN_DERIVED_META_FIELDS) except the one exec_database_status the ASL asks for.
AGENT_EDITABLE_FIELDS = frozenset({
    # phase progress the ASL prompts ask the agent to record (values restricted below)
    "status", "exec_database_status",
    # detected stack (Assessment prompt step 4, discovery skill)
    "language", "framework", "build_tool", "runtime", "architecture",
    # descriptive business facts the chat assistant collects (completeness categories)
    "app_name", "description", "business_unit", "tier", "compliance", "rpo", "rto", "data_classification",
    "uptime_sla", "change_freeze_windows", "dependencies_upstream", "dependencies_downstream", "licensing",
    "db_migration_method", "startup_shutdown_procedures",
})
_AGENT_FIELD_VALUES = {
    "status": frozenset({"discovered", "assessed", "cutover_ready", "completed"}),
    "exec_database_status": frozenset({"done", "not_required"}),
}


def _strip(items):
    return [{k: v for k, v in i.items() if k not in _INTERNAL} for i in items]


def handler(event, ctx=None):
    return registry.dispatch(event, ctx)


def _machines_for(app_id: str, meta: dict) -> tuple[list, list]:
    """(machines, unresolved_ids) for an app.

    Machines are stored as their own entity (PK=MACHINE#<id>, SK=META — see
    backend/src/db/keys.py / repository.read_full_app) and linked to an app only
    through META.machine_ids. Querying APP#<id>/MACHINE# returned [] for every
    ADS-discovered app, so resolve the ids with BatchGetItem (100 keys per call,
    retrying UnprocessedKeys), and fall back to the APP#/MACHINE# sibling query
    for records written that way.
    """
    ids = [m for m in dict.fromkeys(str(x) for x in (meta.get("machine_ids") or [])) if m]
    found: dict = {}
    client = table.meta.client
    for i in range(0, len(ids), 100):
        request = {table.name: {"Keys": [{"PK": f"MACHINE#{m}", "SK": "META"} for m in ids[i:i + 100]]}}
        for _attempt in range(3):
            resp = client.batch_get_item(RequestItems=request)
            for item in resp.get("Responses", {}).get(table.name, []):
                found[item["PK"].split("#", 1)[1]] = item
            request = resp.get("UnprocessedKeys") or {}
            if not request:
                break
    machines = [found[m] for m in ids if m in found]
    if not machines:
        machines = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("MACHINE#")
        ).get("Items", [])
    resolved = set(found) | {str(m.get("machine_id", "")) for m in machines}
    return _strip(machines), [m for m in ids if m not in resolved]


@registry.tool(
    "get_app_context",
    description="Return the full application context (meta, machines, repos, connections, analysis).",
    schema={"app_id": {"type": "string", "required": True, "description": "Application id"}},
)
def get_app_context(args):
    app_id = args["app_id"]
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item")
    if not meta:
        return {"error": f"App {app_id} not found"}

    def q(prefix, **kw):
        return table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(prefix), **kw
        ).get("Items", [])

    # A machine-lookup failure must not fail the whole context: the agent then
    # runs blind and invents the strategy/facts it could not read (APP-CLI/HE/TE).
    machines_error = None
    try:
        machines, unresolved = _machines_for(app_id, meta)
    except Exception as e:  # noqa: BLE001
        machines, unresolved = [], [str(m) for m in (meta.get("machine_ids") or [])]
        machines_error = f"machine lookup failed: {e}"
    out = {
        "app_id": app_id,
        "meta": {k: v for k, v in meta.items() if k not in _INTERNAL},
        "machines": machines,
        # ids on META.machine_ids with no MACHINE# record (e.g. L2-supplied ids
        # never ingested via ADS) — reported, not silently dropped.
        "unresolved_machine_ids": unresolved,
        "owners": _strip(q("OWNER#")),
        "repos": _strip(q("REPO#")),
        "connections": _strip(q("CONN#", Limit=APP_CONTEXT_CONN_READ_LIMIT)[:APP_CONTEXT_MAX_CONNECTIONS]),
        "analysis": _strip(q("ANALYSIS#")),
    }
    if machines_error:
        out["machines_error"] = machines_error
    return json_safe(out)


@registry.tool(
    "update_app_field",
    description="Set a single descriptive field (status, detected stack, business facts) on an existing "
                "application's META record. Strategy, targets, account/network, exposure and approval fields "
                "are refused.",
    schema={
        "app_id": {"type": "string", "required": True},
        "field": {"type": "string", "required": True},
        "value": {"type": "string", "description": "New value"},
    },
)
def update_app_field(args):
    app_id, field, value = str(args.get("app_id") or ""), str(args.get("field") or ""), args.get("value")
    if field not in AGENT_EDITABLE_FIELDS:
        return {"error": f"Field {field!r} is not agent-editable. Agent-editable fields: "
                         f"{', '.join(sorted(AGENT_EDITABLE_FIELDS))}. Strategy, targets, account/network, exposure, "
                         "approval/deploy and run-derived fields are set by the strategy tools, approval gates and "
                         "the system — not by update_app_field."}
    allowed_values = _AGENT_FIELD_VALUES.get(field)
    if allowed_values is not None and value not in allowed_values:
        return {"error": f"{field} must be one of {', '.join(sorted(allowed_values))} (got {value!r})"}
    if not app_id:
        return {"error": "app_id is required"}
    try:
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression="SET #f = :v",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames={"#f": field},
            ExpressionAttributeValues={":v": value},
        )
    except Exception as e:  # noqa: BLE001 — boto3 may be stubbed in tests; match on the error code
        if (getattr(e, "response", None) or {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {"error": f"Application {app_id} not found"}
        raise
    return {"success": True, "app_id": app_id, "field": field}


@registry.tool(
    "search_machines",
    description="Search machines by a free-text query.",
    schema={"query": {"type": "string", "required": True}},
)
def search_machines(args):
    query = args["query"].lower()
    machines = query_all(table, max_items=SEARCH_MACHINES_MAX_SCANNED, IndexName="GSI1",
                         KeyConditionExpression=Key("GSI1PK").eq("TYPE#MACHINE"))
    matches = [i for i in machines if query in json.dumps(i, cls=DecimalEncoder).lower()]
    return {"matches": _strip(matches[:SEARCH_MACHINES_MAX_RESULTS]), "count": len(matches)}


@registry.tool(
    "assign_machines_to_app",
    description="Attach one or more machine ids (comma-separated) to an application.",
    schema={
        "app_id": {"type": "string", "required": True},
        "machine_ids": {"type": "string", "required": True, "description": "Comma-separated machine ids"},
    },
)
def assign_machines_to_app(args):
    app_id = args["app_id"]
    machine_ids = [m.strip() for m in args["machine_ids"].split(",") if m.strip()]
    if not machine_ids:
        return {"error": "machine_ids required"}
    current = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {})
    updated = sorted(set((current.get("machine_ids") or []) + machine_ids))
    table.update_item(
        Key={"PK": f"APP#{app_id}", "SK": "META"},
        UpdateExpression="SET machine_ids = :m, machine_count = :c",
        ExpressionAttributeValues={":m": updated, ":c": len(updated)},
    )
    return {"success": True, "app_id": app_id, "machine_count": len(updated), "added": len(machine_ids)}


@registry.tool(
    "get_portfolio_summary",
    description="Return total application count and migration-strategy distribution.",
    schema={},
)
def get_portfolio_summary(args):
    apps = query_all(table, IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TYPE#APP"))
    strategies: dict = {}
    for a in apps:
        s = a.get("migration_strategy", "undecided")
        strategies[s] = strategies.get(s, 0) + 1
    return {"total_apps": len(apps), "strategy_distribution": strategies}


@registry.tool(
    "add_owner",
    description="Append an owner (name/email/role) to an application.",
    schema={
        "app_id": {"type": "string", "required": True},
        "name": {"type": "string", "required": True},
        "email": {"type": "string", "required": True},
        "role": {"type": "string", "description": "Owner role (default 'owner')"},
    },
)
def add_owner(args):
    app_id = args["app_id"]
    current = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {})
    owners = (current.get("owners") or [])
    owners.append({"name": args["name"], "email": args["email"], "role": args.get("role", "owner")})
    table.update_item(
        Key={"PK": f"APP#{app_id}", "SK": "META"},
        UpdateExpression="SET owners = :o",
        ExpressionAttributeValues={":o": owners},
    )
    return {"success": True, "app_id": app_id, "owners_count": len(owners)}
