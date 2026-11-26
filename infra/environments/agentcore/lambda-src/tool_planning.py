"""Gateway Tool target: Planning — wave plans, migration plans, dependencies."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from agent_tools import DecimalEncoder, ToolRegistry, json_safe, query_all
from boto3.dynamodb.conditions import Key

TABLE = os.environ["DYNAMODB_TABLE"]
BUCKET = os.environ.get("S3_BUCKET", "")
REGION = os.environ["AWS_REGION"]  # set by the Lambda runtime
ddb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
table = ddb.Table(TABLE)

registry = ToolRegistry("planning")

# Wave planner = backend/src/core/wave_planner.py, zipped next to this file (lambdas.tf).
from wave_planner import PlannerConfig
from wave_planner import generate_wave_plan as _plan

# Same tunables and defaults as backend Settings (wave_*), set via tool_planning_tunables in lambdas.tf.
WAVE_PILOT_SIZE = int(os.environ.get("WAVE_PILOT_SIZE", "3"))
WAVE_BAND_LIMITS = [int(x) for x in os.environ.get("WAVE_BAND_LIMITS", "3,8,12,10").split(",") if x.strip()]
WAVE_SPILL_LIMIT = int(os.environ.get("WAVE_SPILL_LIMIT", "10"))
WAVE_TIER1_MAX_SHARE = float(os.environ.get("WAVE_TIER1_MAX_SHARE", "0.30"))
WAVE_MAX_CYCLE_COWAVE = int(os.environ.get("WAVE_MAX_CYCLE_COWAVE", "3"))
# Parallel per-machine CONN# reads (DynamoDB has no multi-partition begins_with batch).
WAVE_CONN_QUERY_CONCURRENCY = max(1, int(os.environ.get("WAVE_CONN_QUERY_CONCURRENCY", "8")))


def handler(event, ctx=None):
    return registry.dispatch(event, ctx)


def _app_entry(a):
    return {"app_id": a.get("app_id"), "app_name": a.get("app_name", ""), "strategy": a.get("migration_strategy", "")}


def _machine_conns(mid: str) -> list:
    return query_all(table, KeyConditionExpression=Key("PK").eq(f"MACHINE#{mid}") & Key("SK").begins_with("CONN#"))


def _machine_connections(apps: list) -> list:
    """Every machine's CONN# records (paginated, concurrent), tagged with their source machine."""
    mids = sorted({str(m) for a in apps for m in (a.get("machine_ids") or [])})
    if not mids:
        return []
    out = []
    with ThreadPoolExecutor(max_workers=min(WAVE_CONN_QUERY_CONCURRENCY, len(mids))) as pool:
        for mid, conns in zip(mids, pool.map(_machine_conns, mids), strict=True):
            out.extend({**c, "source_machine_id": mid} for c in conns)
    return out


@registry.tool(
    "generate_wave_plan",
    description="Generate a wave plan per the planning playbook (Pilot/Foundation/Core/Customer-Facing/Long Tail, "
                "dependency order, shared-DB co-location, tier_1 spread). Read-only: returns the plan; the UI's "
                "Generate button stores it.",
    schema={
        "pilot_size": {"type": "string", "description": f"Pilot wave size (default {WAVE_PILOT_SIZE})"},
    },
)
def generate_wave_plan(args):
    apps = query_all(table, IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TYPE#APP"))
    if not apps:
        return {"error": "No applications in portfolio"}
    cfg = PlannerConfig(pilot_size=int(args.get("pilot_size") or WAVE_PILOT_SIZE),
                        band_limits=dict(enumerate(WAVE_BAND_LIMITS)), spill_limit=WAVE_SPILL_LIMIT,
                        tier1_max_share=WAVE_TIER1_MAX_SHARE, max_cycle_cowave=WAVE_MAX_CYCLE_COWAVE)
    return json_safe(_plan(apps, _machine_connections(apps), config=cfg))


@registry.tool(
    "store_wave_plan",
    description="Persist a wave plan (latest wins, keyed by timestamp).",
    schema={"wave_plan": {"type": "object", "required": True}},
)
def store_wave_plan(args):
    plan = args["wave_plan"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    table.put_item(Item={
        "PK": "WAVE_PLAN", "SK": f"PLAN#{now}",
        "GSI1PK": "TYPE#WAVE_PLAN", "GSI1SK": now, "wave_plan": plan, "updated_at": now,
    })
    if BUCKET:
        s3.put_object(Bucket=BUCKET, Key=f"wave-plans/{now.replace(':', '-')}.json",
                      Body=json.dumps(plan, cls=DecimalEncoder), ContentType="application/json")
    return {"success": True, "updated_at": now}


@registry.tool(
    "get_wave_plan",
    description="Return the most recent wave plan.",
    schema={},
)
def get_wave_plan(args):
    resp = table.query(
        IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TYPE#WAVE_PLAN"),
        ScanIndexForward=False, Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return {"wave_plan": None, "message": "No wave plan generated yet"}
    return json_safe({"wave_plan": items[0].get("wave_plan"), "updated_at": items[0].get("updated_at")})


@registry.tool(
    "store_migration_plan",
    description="Persist a per-application migration plan.",
    schema={"app_id": {"type": "string", "required": True}, "plan": {"type": "object", "required": True}},
)
def store_migration_plan(args):
    app_id, plan = args["app_id"], args["plan"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    table.put_item(Item={
        "PK": f"APP#{app_id}", "SK": "MIGRATION_PLAN",
        "GSI1PK": "TYPE#MIGRATION_PLAN", "GSI1SK": app_id, "plan": plan, "updated_at": now,
    })
    if BUCKET:
        s3.put_object(Bucket=BUCKET, Key=f"migration-plans/{app_id}/plan.json",
                      Body=json.dumps(plan, cls=DecimalEncoder), ContentType="application/json")
    return {"success": True, "app_id": app_id}
