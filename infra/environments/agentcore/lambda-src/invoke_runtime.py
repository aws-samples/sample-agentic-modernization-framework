"""
Lambda bridge: SFN → AgentCore Harness (invoke_harness).

Each SFN state calls this Lambda with {app_id, mode, prompt}.
The Lambda invokes the Harness which:
  1. Reasons about the prompt
  2. Calls Gateway tools (get_app_context, recommend_strategy, etc.)
  3. Returns a structured response with real data from DynamoDB

For store_token mode: stores SFN task token in DynamoDB for approval UI.
"""
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import risk_scoring
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION = os.environ["AWS_REGION"]  # set by the Lambda runtime
HARNESS_ARN = os.environ.get("HARNESS_ARN", "")
DDB_TABLE = os.environ["DYNAMODB_TABLE"]  # required — no baked resource literal
# Harness invocations stream for minutes; boto3's 60 s default read timeout is too short.
HARNESS_READ_TIMEOUT_SECONDS = int(os.environ.get("HARNESS_READ_TIMEOUT_SECONDS", "600"))
# Cap on the Harness response text returned to Step Functions (state payloads are limited to 256 KB).
HARNESS_RESPONSE_MAX_CHARS = int(os.environ.get("HARNESS_RESPONSE_MAX_CHARS", "10000"))
# Cap on the recorded-facts string embedded in phase prompts (ResolveStrategy app_facts).
APP_FACTS_MAX_CHARS = int(os.environ.get("APP_FACTS_MAX_CHARS", "6000"))

agentcore = boto3.client("bedrock-agentcore", region_name=REGION,
                         config=Config(read_timeout=HARNESS_READ_TIMEOUT_SECONDS))
ddb = boto3.resource("dynamodb", region_name=REGION)


# ── Strategy rules ────────────────────────────────────────────────────────────
# Mirror of backend/src/core/strategy_options.py (the Lambda zip cannot import
# backend/) — keep in sync. Used by the execution pre-hook to reject selections
# that are invalid for the confirmed 7R strategy.
_COMPUTE_LIFT = {"ec2"}
# Scope: ECS Fargate + Lambda (EC2 for rehost/relocate only), CodePipeline only.
_COMPUTE_MODERN = {"ecs_fargate", "lambda"}
_DB_LIFT = {"same_as_source", "dynamodb", "rds_mysql"}
_DB_MODERN = {"aurora_postgresql", "aurora_mysql", "rds_postgresql", "rds_mysql", "dynamodb", "none"}
_CICD_FULL = {"codepipeline"}
_CLOSE_OUT = ("retire", "retain", "repurchase")
_AWS_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
_VPC_CIDR_RE = re.compile(
    r"^(?:10\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}|192\.168\.\d{1,3})\.\d{1,3}/(?:1[6-9]|2[0-4])$"  # /16–/24 (mirrors core/strategy_options.py)
)


def _is_network_address(cidr):
    """No host bits set (10.0.0.0/24 ok, 10.0.0.1/24 not) — CreateVpc rejects the latter."""
    import ipaddress

    try:
        ipaddress.ip_network(cidr, strict=True)
    except ValueError:
        return False
    return True


# Mirror of backend/src/api/v1/portfolio._CLONE_URL_RE — ATX clones REPO#.url as-is.
_CLONE_URL_RE = re.compile(r"^https://git-codecommit\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/v1/repos/[A-Za-z0-9._-]{1,100}$|^codecommit::[a-z0-9-]+://[A-Za-z0-9._@-]{1,200}$")


def _compute_options_for(strategy):
    if strategy in ("rehost", "relocate"):
        return _COMPUTE_LIFT
    if strategy in _CLOSE_OUT:
        return {"none"}
    return _COMPUTE_MODERN


def _database_options_for(strategy):
    if strategy in ("rehost", "relocate"):
        return _DB_LIFT
    if strategy in _CLOSE_OUT:
        return {"none"}
    return _DB_MODERN


def _cicd_options_for(strategy):
    return {"none"} if strategy in _CLOSE_OUT else _CICD_FULL


# Conditional sizing inputs captured at assessment approval (mirror of
# strategy_options.requires_ami / db_instance_classes_for — keep in sync).
_AMI_ID_RE = re.compile(r"^ami-(?:[0-9a-f]{8}|[0-9a-f]{17})$")
_MANAGED_DB = {"rds_postgresql", "rds_mysql", "aurora_postgresql", "aurora_mysql"}
_DB_CLASSES_GRAVITON = {"db.t4g.medium", "db.t4g.large", "db.m6g.large", "db.m6g.xlarge", "db.m6g.2xlarge",
                        "db.r6g.large", "db.r6g.xlarge", "db.r6g.2xlarge", "db.r6g.4xlarge"}
_DB_CLASSES_AURORA = {"db.t4g.medium", "db.t4g.large", "db.r6g.large", "db.r6g.xlarge", "db.r6g.2xlarge",
                      "db.r6g.4xlarge"}


def _db_instance_classes_for(target_database):
    if target_database.startswith("aurora_"):
        return _DB_CLASSES_AURORA
    return _DB_CLASSES_GRAVITON if target_database in _MANAGED_DB else set()


# Mirror of the "assessment"/"design" mandatory lists in
# backend/src/core/completeness.py PHASE_GATES — keep in sync. Advisory only:
# surfaced as pre-hook warnings + in the gate decision_summary, never blocking.
_COMPLETENESS_GATES = {
    "assessment": ["app_name", "tier", "machine_ids", "compliance", "rpo", "rto", "target_region"],
    "design": ["app_name", "tier", "machine_ids", "compliance", "rpo", "rto", "target_region",
               "migration_strategy", "owners", "data_classification", "uptime_sla"],
}


def _is_filled(value):
    """Mirror of completeness._is_filled."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value > 0
    return bool(value)


# owners / repos live as OWNER# / REPO# sibling items, not on META (mirror of
# completeness._COUNT_FALLBACK): count the siblings when META lacks the field.
_SIBLING_PREFIX = {"owners": ("owner_count", "OWNER#"), "repos": ("repo_count", "REPO#")}


def _completeness_missing(meta, gate, table=None, app_id=""):
    from boto3.dynamodb.conditions import Key
    missing = []
    for field in _COMPLETENESS_GATES.get(gate, []):
        if _is_filled(meta.get(field)):
            continue
        if field in _SIBLING_PREFIX:
            count_field, prefix = _SIBLING_PREFIX[field]
            if _is_filled(meta.get(count_field)):
                continue
            if table is not None and app_id:
                try:
                    if table.query(
                        KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(prefix),
                        Limit=1,
                    ).get("Items"):
                        continue
                except Exception as e:
                    logger.warning("completeness sibling query %s failed for %s: %s", prefix, app_id, e)
        missing.append(field)
    return missing


def _parse_ts(value):
    """Parse the ISO-8601 timestamps used across the table / SFN context
    ('2026-10-01T14:29:27Z', '...+00:00', '....312Z'). None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run_started_at(event, table, app_id):
    """When the current workflow run started: the SFN execution StartTime passed
    by the ASL (`run_started_at`), else WORKFLOW#ACTIVE.started_at. None if unknown
    (freshness checks are then skipped rather than failing closed on old runs)."""
    started = _parse_ts(event.get("run_started_at"))
    if started:
        return started
    try:
        wf = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"}).get("Item", {}) or {}
        return _parse_ts(wf.get("started_at"))
    except Exception as e:
        logger.warning("WORKFLOW#ACTIVE read failed for %s: %s", app_id, e)
        return None


def _analysis_ts(item):
    """Timestamp of an ANALYSIS#<type>#<ts> record (SK suffix, else updated_at)."""
    sk = item.get("SK", "")
    return _parse_ts(sk.split("#", 2)[2] if sk.count("#") >= 2 else "") or _parse_ts(item.get("updated_at"))


def _current_assessment_summary(table, app_id, started):
    """Latest ANALYSIS#assessment-summary record, and whether it belongs to the
    current run (newer than `started`; True when the run start is unknown)."""
    item = _latest_item_by_prefix(table, app_id, "ANALYSIS#assessment-summary#")
    if not item:
        return {}, False
    ts = _analysis_ts(item)
    return item, (started is None or (ts is not None and ts >= started))


def _version_num(version):
    m = re.search(r"\d+", str(version or ""))
    return int(m.group(0)) if m else 0


def _latest_design(table, app_id, meta):
    """The design doc version actually stored most recently. Prefers the version
    store_design_document mirrored onto META; falls back to the newest DESIGN#
    item (by updated_at, then numeric version — never assumes v1)."""
    from boto3.dynamodb.conditions import Key
    version = (meta.get("design_version") or "").strip()
    if version:
        try:
            item = table.get_item(Key={"PK": f"APP#{app_id}", "SK": f"DESIGN#{version}"}).get("Item")
            if item:
                return item
        except Exception as e:
            logger.warning("DESIGN#%s read failed for %s: %s", version, app_id, e)
    try:
        items = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("DESIGN#"),
        ).get("Items", [])
    except Exception as e:
        logger.warning("DESIGN# query failed for %s: %s", app_id, e)
        return {}
    if not items:
        return {}
    items.sort(key=lambda i: (i.get("updated_at", ""), _version_num(i.get("version") or i.get("SK"))))
    return items[-1]


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def handler(event, context=None):
    start = time.time()
    mode = event.get("mode", "orchestrate")
    app_id = event.get("app_id", "")
    prompt = event.get("prompt", "")

    # Handle task token storage for approval gates
    if mode == "store_token":
        return _store_task_token(event)

    # Resolve the selected 7R strategy so the state machine can branch on it
    # explicitly (Requirement 31.5) after Discovery has set it.
    if mode == "resolve_strategy":
        return _resolve_strategy(event)

    # Deterministically finalize the journey (does not rely on the harness having
    # called update_app_field). Sets app status + WORKFLOW#ACTIVE to completed.
    if mode == "mark_completed":
        return _mark_completed(event)

    # Deterministically verify a phase produced its required outputs. Replaces the
    # old ECS "_verify_and_complete" anti-pattern (which silently re-ran tools in
    # Python behind the agent). Here the phase contract lives in ONE place and the
    # result is an explicit ASL signal — a Choice state proceeds on valid=true or
    # routes to a bounded phase re-run / MarkFailed on valid=false.
    if mode == "validate_phase":
        return _validate_phase(event)

    # Deterministic PRE-hook: check a phase's preconditions BEFORE it runs, reading
    # only persisted state. Returns {ready: bool, blockers: [str]} so a Choice can
    # gate entry to the phase (proceed on ready, else fail fast) — the mirror image
    # of validate_phase, which runs AFTER a phase.
    if mode == "prehook_phase":
        return _prehook_phase(event)

    if not prompt:
        return {"status": "error", "message": "No prompt provided.", "app_id": app_id}

    logger.info("[%s] app=%s invoking Harness", mode, app_id)

    # Update workflow phase in DynamoDB (workflow phases only — not chat/orchestrate)
    if app_id and mode not in ("store_token", "orchestrate", "chat"):
        _update_workflow_phase(app_id, mode)

    # Invoke the Harness. A caller-supplied session_id (e.g. chat) gives multi-turn
    # continuity; otherwise generate a fresh one per invocation.
    try:
        session_id = _harness_session_id(event.get("session_id") or f"{app_id}-{mode}-{uuid.uuid4().hex}")

        response = agentcore.invoke_harness(
            harnessArn=HARNESS_ARN,
            runtimeSessionId=session_id,
            messages=[{
                "role": "user",
                "content": [{"text": prompt}]
            }]
        )

        # Consume streaming response
        full_text = ""
        tool_calls = []

        for event_item in response.get("stream", []):
            keys = list(event_item.keys())
            if not keys:
                continue
            etype = keys[0]

            if etype == "contentBlockDelta":
                delta = event_item["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    full_text += delta["text"]
                elif "toolUse" in delta:
                    pass  # Tool input streaming
                elif "toolResult" in delta:
                    pass  # Tool result streaming
            elif etype == "contentBlockStart":
                start_info = event_item["contentBlockStart"].get("start", {})
                if "toolUse" in start_info:
                    tool_calls.append(start_info["toolUse"].get("name", ""))

        elapsed = time.time() - start
        logger.info("[%s] done in %.1fs | tools=%s | response=%d chars",
                    mode, elapsed, tool_calls, len(full_text))

        return {
            "status": "success",
            "app_id": app_id,
            "mode": mode,
            "response": full_text[:HARNESS_RESPONSE_MAX_CHARS],
            "tools_called": tool_calls,
            "elapsed_seconds": round(elapsed, 2),
        }

    except Exception as e:
        elapsed = time.time() - start
        logger.exception("[%s] Harness invocation FAILED app=%s", mode, app_id)
        return {
            "status": "error",
            "message": str(e),
            "error_type": type(e).__name__,
            "app_id": app_id,
            "mode": mode,
            "elapsed_seconds": round(elapsed, 2),
        }


def _resolve_strategy(event):
    """Return the application's selected 7R strategy for a Choice-state branch.

    Reads the persisted migration_strategy set by recommend_strategy during
    Discovery. Normalizes to a lowercase 7R value; defaults to 'replatform'
    if unset so the workflow still proceeds down the build path.
    """
    app_id = event.get("app_id", "")
    table = ddb.Table(DDB_TABLE)
    strategy = ""
    item = {}
    try:
        item = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
        strategy = (item.get("migration_strategy") or "").strip().lower()
    except Exception as e:
        logger.warning("resolve_strategy read failed for %s: %s", app_id, e)
    valid = {"rehost", "replatform", "refactor", "repurchase", "retain", "retire", "relocate"}
    if strategy not in valid:
        strategy = "replatform"
    # Coarse branch class the Choice state keys on.
    if strategy in ("retire", "retain", "repurchase"):
        branch = "close_out"       # skip execution/validation/cutover
    elif strategy in ("rehost", "relocate"):
        branch = "rehost_relocate" # infra/relocation, no code transform
    else:
        branch = "build"           # replatform/refactor: full execution
    return {"app_id": app_id, "migration_strategy": strategy, "branch": branch,
            "app_facts": _app_facts(table, app_id, item, strategy)}


def _app_facts(table, app_id, meta, strategy):
    """One-paragraph digest of the app's RECORDED facts, injected verbatim into the
    Assessment / close-out prompts. The agent must not depend on a tool call to
    learn the confirmed strategy or the facts it writes about: when
    get_app_context failed (missing IAM action) it invented them (APP-CLI/HE/TE)."""
    from boto3.dynamodb.conditions import Key

    def j(v):
        if isinstance(v, (list, tuple, set)):
            return ", ".join(str(x) for x in v) or None
        if isinstance(v, dict):
            # ", " inside a value so it can't be mistaken for the top-level "; " separator.
            return ", ".join(f"{k}={j(x)}" for k, x in v.items() if x not in (None, "", [], {})) or None
        return str(v) if v not in (None, "") else None

    try:
        owners = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("OWNER#"),
        ).get("Items", [])
    except Exception as e:
        logger.warning("app_facts OWNER# read failed for %s: %s", app_id, e)
        owners = []
    m = meta or {}
    # dr_availability repeats rpo/rto/uptime_sla, which are listed on their own;
    # keep only what it adds (APP-CLI's facts listed rpo and rto twice).
    dr = m.get("dr_availability")
    dr = {k: v for k, v in dr.items() if k not in ("rpo", "rto", "uptime_sla", "source")} if isinstance(dr, dict) else dr
    pairs = [
        ("confirmed migration_strategy", strategy),
        ("app_name", m.get("app_name")), ("tier", m.get("tier")), ("business_unit", m.get("business_unit")),
        ("architecture", m.get("architecture")), ("language", m.get("language")), ("framework", m.get("framework")),
        ("deployment_method", m.get("deployment_method")), ("containerization", m.get("containerization")),
        ("current_compute", m.get("current_compute")),
        ("machines", f"{m.get('machine_count') or len(m.get('machine_ids') or [])} ({j(m.get('machine_ids'))})"),
        ("owners", j([f"{o.get('name') or o.get('email')} ({o.get('role', '')})" for o in owners])),
        ("database", j(m.get("database"))), ("dependencies_upstream", j(m.get("dependencies_upstream"))),
        ("dependencies_downstream", j(m.get("dependencies_downstream"))), ("integrations", j(m.get("integrations"))),
        ("compliance", j(m.get("compliance"))), ("data_classification", m.get("data_classification")),
        ("rpo", m.get("rpo")), ("rto", m.get("rto")), ("uptime_sla", m.get("uptime_sla")), ("dr", j(dr)),
        ("current_cost", j(m.get("current_cost"))), ("constraints", j(m.get("constraints"))),
        ("hard_deadline", m.get("hard_deadline")), ("change_freeze_windows", j(m.get("change_freeze_windows"))),
        ("risk_score", f"{m.get('risk_score')}/100 ({m.get('risk_tier')})" if m.get("risk_score") is not None else None),
        ("engine recommendation", f"{m.get('recommended_strategy')} ({m.get('recommended_confidence')}%)"
         if m.get("recommended_strategy") else None),
        ("target_compute", m.get("target_compute")), ("target_database", m.get("target_database")),
        ("cicd_target", m.get("cicd_target")),
    ]
    facts = "; ".join(f"{k}={v}" for k, v in pairs if v not in (None, "", "None"))
    return facts[:APP_FACTS_MAX_CHARS]


def _mark_completed(event):
    """Deterministically finalize a completed journey.

    Sets the app META status and the WORKFLOW#ACTIVE record to completed so the
    UI reflects "Done" without depending on the harness having called
    update_app_field. Idempotent.
    """
    import time as _t

    app_id = event.get("app_id", "")
    if not app_id:
        return {"status": "error", "message": "app_id required"}
    table = ddb.Table(DDB_TABLE)
    now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    try:
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression="SET #s = :c, completed_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": "completed", ":t": now},
        )
    except Exception as e:
        logger.warning("mark_completed META update failed for %s: %s", app_id, e)
    try:
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"},
            # current_step too: it otherwise keeps the last *_running value
            # (APP-HE/TE showed COMPLETED with current_step=assessment_running).
            UpdateExpression="SET #s = :c, current_phase = :p, current_step = :p, completed_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": "COMPLETED", ":p": "completed", ":t": now},
        )
    except Exception as e:
        logger.warning("mark_completed WORKFLOW update failed for %s: %s", app_id, e)
    return {"app_id": app_id, "status": "completed", "completed_at": now}


def _harness_session_id(session_id):
    """InvokeHarness needs runtimeSessionId of 33–256 chars. Chat's ids ("chat-APP-CLI",
    "chat-portfolio") are shorter, so pad them deterministically — the same chat id
    always maps to the same Harness session, keeping multi-turn memory."""
    sid = str(session_id)
    if len(sid) >= 33:
        return sid[:256]
    import hashlib

    return f"{sid}-{hashlib.sha256(sid.encode()).hexdigest()[:32]}"


def _prehook_phase(event):
    """Deterministic pre-hook: verify a phase's preconditions BEFORE it runs.

    Reads only persisted DynamoDB state; never mutates. Returns:
        {"app_id", "phase", "ready": bool, "blockers": [str]}

    Wired in the ASL as a Task directly BEFORE the phase, gated by a Choice:

        "PreHook_Discovery": {Task → mode=prehook_phase, phase=discovery} → "PreCheck_Discovery"
        "PreCheck_Discovery": {Choice: ready → Discovery, Default → MarkFailed}

    Contracts (blockers → not ready; warnings are advisory, never blocking):
      discovery  — app exists + app_name (no linked repo → warning only).
      assessment — migration_strategy + a linked repo whose url is clonable (not a
                   bare repo name); warns on the completeness
                   gate (completeness.PHASE_GATES["assessment"]).
      design     — migration_strategy + an ANALYSIS#assessment-summary record
                   from the CURRENT run (newer than run_started_at / the
                   WORKFLOW#ACTIVE start); warns on the design completeness gate.
      execution  — migration_strategy + target_compute + target_database +
                   cicd_target (each valid for the strategy per strategy_options)
                   + 12-digit target_account + RFC1918 vpc_cidr + a linked repo.
    """
    from boto3.dynamodb.conditions import Key

    app_id = event.get("app_id", "")
    phase = (event.get("phase") or "").strip().lower()
    if not app_id:
        return {"app_id": app_id, "phase": phase, "ready": False, "blockers": ["app_id"], "warnings": []}

    table = ddb.Table(DDB_TABLE)
    blockers = []
    warnings = []

    def _has_sk_prefix(prefix):
        try:
            resp = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(prefix),
                Limit=1,
            )
            return len(resp.get("Items", [])) > 0
        except Exception as e:
            logger.warning("prehook query %s failed for %s: %s", prefix, app_id, e)
            return False

    if phase == "discovery":
        try:
            meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
        except Exception as e:
            logger.warning("prehook META read failed for %s: %s", app_id, e)
            meta = {}
        if not meta:
            blockers.append("app_not_found")
        elif not (meta.get("app_name") or "").strip():
            blockers.append("app_name")
        # Repos arrive with L2 enrichment; Discovery (L1 machines → 7R strategy)
        # doesn't need one. Assessment's pre-hook is what requires a clonable repo.
        if not _has_sk_prefix("REPO#"):
            warnings.append("no_repo_linked")
    elif phase == "assessment":
        # Assessment launches ATX (clones the repo) and reasons over the confirmed
        # strategy — require both before entering the phase.
        try:
            meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
        except Exception as e:
            logger.warning("prehook META read failed for %s: %s", app_id, e)
            meta = {}
        if not (meta.get("migration_strategy") or "").strip():
            blockers.append("migration_strategy")
        if not _has_sk_prefix("REPO#"):
            blockers.append("no_repo_linked")
        else:
            # ATX clones each url as-is — a bare repo name fails the clone.
            try:
                repos = table.query(
                    KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("REPO#"),
                ).get("Items", [])
            except Exception as e:
                logger.warning("prehook REPO# read failed for %s: %s", app_id, e)
                repos = []
            for r in repos:
                url = str(r.get("url") or "").strip()  # the worker strips whitespace
                if url and not _CLONE_URL_RE.match(url):
                    blockers.append(f"repo_url_invalid:{r.get('name') or url}")
        # Completeness phase gate (completeness.PHASE_GATES["assessment"]) is
        # advisory: most discovered apps lack rpo/rto/target_region, so blocking
        # would stop every journey. Surfaced as warnings + at the gate.
        missing = _completeness_missing(meta, "assessment", table, app_id)
        if missing:
            warnings.append("completeness gate (assessment) missing: " + ", ".join(missing))
    elif phase == "design":
        # Design reasons over THIS run's assessment — require the assessment-summary
        # record (not just any ANALYSIS#: the Discovery risk-score record would
        # satisfy that) produced since the workflow started, and a strategy.
        try:
            meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
        except Exception as e:
            logger.warning("prehook META read failed for %s: %s", app_id, e)
            meta = {}
        strategy = (meta.get("migration_strategy") or "").strip().lower()
        if not strategy:
            blockers.append("migration_strategy")
        # Design writes the architecture for the compute/DB/CI-CD approved at the
        # assessment gate — without them it would pick its own (and the design
        # post-check would have nothing to compare against).
        for field, options_for in (("target_compute", _compute_options_for),
                                   ("target_database", _database_options_for),
                                   ("cicd_target", _cicd_options_for)):
            value = (meta.get(field) or "").strip().lower()
            if not value:
                blockers.append(field)
            elif strategy and value not in options_for(strategy):
                blockers.append(f"{field}_invalid_for_{strategy}")
        summary, current = _current_assessment_summary(table, app_id, _run_started_at(event, table, app_id))
        if not summary:
            blockers.append("assessment_summary")
        elif not current:
            blockers.append("assessment_summary_stale")
        missing = _completeness_missing(meta, "design", table, app_id)
        if missing:
            warnings.append("completeness gate (design) missing: " + ", ".join(missing))
    elif phase == "execution":
        # Execution provisions infra + CI/CD and transforms code — require every
        # approved selection it consumes, each valid for the strategy (mirrored
        # strategy_options rules), plus a linked repo. Fail fast rather than emit
        # [REQUIRES INPUT] / generate IaC for an invalid combination.
        try:
            meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
        except Exception as e:
            logger.warning("prehook META read failed for %s: %s", app_id, e)
            meta = {}
        strategy = (meta.get("migration_strategy") or "").strip().lower()
        if not strategy:
            blockers.append("migration_strategy")
        for field, options_for in (("target_compute", _compute_options_for),
                                   ("target_database", _database_options_for),
                                   ("cicd_target", _cicd_options_for)):
            value = (meta.get(field) or "").strip().lower()
            if not value:
                blockers.append(field)
            elif strategy and value not in options_for(strategy):
                blockers.append(f"{field}_invalid_for_{strategy}")
        acct = str(meta.get("target_account") or "").strip()
        if not acct:
            blockers.append("target_account")
        elif not _AWS_ACCOUNT_ID_RE.match(acct):
            blockers.append("target_account_invalid")
        cidr = (meta.get("vpc_cidr") or "").strip()
        if not cidr:
            blockers.append("vpc_cidr")
        elif not _VPC_CIDR_RE.match(cidr) or not _is_network_address(cidr):
            blockers.append("vpc_cidr_invalid")
        # The generated ec2 / rds modules can't plan without these sizing inputs.
        if (meta.get("target_compute") or "").strip().lower() == "ec2":
            ami = str(meta.get("ami_id") or "").strip()
            if not ami:
                blockers.append("ami_id")
            elif ami != "al2023" and not _AMI_ID_RE.match(ami):
                blockers.append("ami_id_invalid")
        target_db = (meta.get("target_database") or "").strip().lower()
        if target_db in _MANAGED_DB:
            cls = str(meta.get("db_instance_class") or "").strip()
            if not cls:
                blockers.append("db_instance_class")
            elif cls not in _db_instance_classes_for(target_db):
                blockers.append(f"db_instance_class_invalid_for_{target_db}")
        if not _has_sk_prefix("REPO#"):
            blockers.append("no_repo_linked")
    # Other phases have no pre-hook contract yet -> ready (non-blocking).

    ready = len(blockers) == 0
    logger.info("prehook_phase app=%s phase=%s ready=%s blockers=%s warnings=%s",
                app_id, phase, ready, blockers, warnings)
    return {"app_id": app_id, "phase": phase, "ready": ready, "blockers": blockers, "warnings": warnings}


def _ensure_risk_score(table, app_id, meta):
    """Deterministic post-hook: ensure a risk score exists for the app.

    Idempotent and non-blocking — if META already carries `risk_score` it's a
    no-op; otherwise it computes the score from persisted discovery data (same
    `risk_scoring` module the Gateway tool + backend use), writes an
    `ANALYSIS#risk-score` record, and mirrors `risk_score`/`risk_tier` onto META.
    Any failure is swallowed so it can never fail the discovery post-check.
    """
    try:
        if meta.get("risk_score") is not None:
            return
        from boto3.dynamodb.conditions import Key
        app_data = {k: v for k, v in meta.items() if k not in ("PK", "SK", "GSI1PK", "GSI1SK")}
        if not app_data.get("repo_count"):
            repos = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("REPO#"),
                Select="COUNT",
            )
            app_data["repo_count"] = repos.get("Count", 0)
        result = risk_scoring.calculate_risk_score(app_data)
        report_md = risk_scoring.render_report_md(app_data.get("app_name", app_id), result)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        table.put_item(Item={
            "PK": f"APP#{app_id}", "SK": f"ANALYSIS#risk-score#{now}",
            "GSI1PK": "TYPE#ANALYSIS", "GSI1SK": f"{app_id}#risk-score",
            "app_id": app_id, "analysis_type": "risk-score",
            "result": result, "data": {"report_md": report_md}, "updated_at": now,
        })
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression="SET risk_score = :s, risk_tier = :t, risk_scored_at = :n",
            ExpressionAttributeValues={":s": result["overall"], ":t": result["tier"], ":n": now},
        )
        logger.info("post-hook computed risk score app=%s overall=%s tier=%s",
                    app_id, result["overall"], result["tier"])
    except Exception as e:  # noqa: BLE001 — post-hook must never block discovery
        logger.warning("risk-score self-heal skipped for %s: %s", app_id, e)


def _validate_phase(event):
    """Deterministically check that a harness phase produced its required outputs.

    Single source of truth for each phase's completion contract. Reads only
    persisted state (DynamoDB) — never re-runs tools, never mutates. Returns:
        {"app_id", "phase", "valid": bool, "missing": [str], "attempt": int}

    Wire in the ASL directly after each harness phase, e.g.:

        "Validate_Discovery": {
          "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
          "Arguments": {"FunctionName": "...:amf-ac-invoke-runtime",
            "Payload": {"mode": "validate_phase", "phase": "discovery",
                        "app_id": "{% $states.input.app_id %}",
                        "attempt": "{% $states.input.attempt ? $states.input.attempt : 1 %}"}},
          "Output": {"app_id": "...", "validation": "{% $states.result.Payload %}"},
          "Next": "Check_Discovery"
        },
        "Check_Discovery": {  // Choice
          "Choices": [
            {"Condition": "{% $states.input.validation.valid %}", "Next": "Approval_Discovery"},
            {"Condition": "{% $states.input.validation.attempt >= 2 %}", "Next": "MarkFailed"}
          ],
          "Default": "Discovery"   // bounded re-run of the phase
        }

    This makes "did the phase actually do its job?" an explicit, observable SFN
    decision instead of a hidden Python fallback.

    Contracts:
      discovery  — migration_strategy + strategy_confidence (+ risk-score self-heal).
      assessment — an ANALYSIS#assessment-summary record from the CURRENT run
                   with NO consistency_contradictions (written for another strategy
                   → `blockers`); its consistency_warnings are returned as `warnings`.
      design     — the LATEST stored DESIGN# version, stored during the current
                   run, whose store-time consistency_check has NO contradictions
                   with the approved META selections (strategy, target_compute,
                   target_database, cicd_target, target_account, vpc_cidr).
                   Contradictions → valid=false + `blockers`; missing sections,
                   premature "Approved" status etc. → `warnings` (gate only).
      closeout   — as design, for the close-out document (checked against the
                   app's recorded facts: machines, owners, RPO/RTO, deadlines…).
    """
    app_id = event.get("app_id", "")
    phase = (event.get("phase") or "").strip().lower()
    try:
        attempt = int(event.get("attempt", 1) or 1)
    except (TypeError, ValueError):
        attempt = 1
    if not app_id:
        return {"app_id": app_id, "phase": phase, "valid": False, "missing": ["app_id"], "attempt": attempt,
                "blockers": [], "warnings": []}

    table = ddb.Table(DDB_TABLE)
    missing = []
    blockers = []   # human-readable reasons behind `missing` (design contradictions)
    warnings = []   # advisory — surfaced at the approval gate, never blocking
    design_version = None

    def _meta():
        try:
            return table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
        except Exception as e:
            logger.warning("validate_phase META read failed for %s: %s", app_id, e)
            return {}

    def _has_sk_prefix(prefix):
        """True if at least one item exists under APP#<id> with SK begins_with(prefix)."""
        from boto3.dynamodb.conditions import Key
        try:
            resp = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(prefix),
                Limit=1,
            )
            return len(resp.get("Items", [])) > 0
        except Exception as e:
            logger.warning("validate_phase query %s failed for %s: %s", prefix, app_id, e)
            return False

    if phase == "discovery":
        meta = _meta()
        if not (meta.get("migration_strategy") or "").strip():
            missing.append("migration_strategy")
        if meta.get("strategy_confidence") in (None, ""):
            missing.append("strategy_confidence")
        # Post-hook self-heal: guarantee a deterministic risk score exists after
        # discovery. Never blocks — computes + persists it if the phase didn't.
        _ensure_risk_score(table, app_id, meta)
    elif phase == "assessment":
        # The assessment-summary from THIS run — any ANALYSIS# (e.g. Discovery's
        # risk-score record) used to satisfy this check.
        summary, current = _current_assessment_summary(table, app_id, _run_started_at(event, table, app_id))
        if not summary:
            missing.append("assessment_summary")
        elif not current:
            missing.append("assessment_summary_stale")
        else:
            warnings.extend(summary.get("consistency_warnings")
                            or (summary.get("result") or {}).get("consistency_warnings") or [])
            # Written for a different strategy than the confirmed one → one bounded
            # re-run with these in the prompt, then MarkFailed (Check_Assessment).
            contradictions = list(summary.get("consistency_contradictions")
                                  or (summary.get("result") or {}).get("consistency_contradictions") or [])
            if contradictions:
                blockers.extend(contradictions)
                missing.append("assessment_contradicts_confirmed_strategy")
    elif phase in ("design", "design_v1", "design_v2", "closeout"):
        # The LATEST stored design version (the agent may store v2 directly) from
        # this run, checked against the approved META selections. `closeout`: the
        # retire/retain/repurchase document (DESIGN#final), whose store-time check
        # ran against the app's recorded facts instead (tool_documents
        # _check_closeout_document) — same contradiction contract. The content
        # check itself runs in store_design_document (this Lambda has no S3
        # access); here we enforce its persisted result.
        meta = _meta()
        design = _latest_design(table, app_id, meta)
        started = _run_started_at(event, table, app_id)
        if not design:
            missing.append("design_document")
        else:
            design_version = design.get("version") or design.get("SK", "").replace("DESIGN#", "")
            updated = _parse_ts(design.get("updated_at"))
            check = design.get("consistency_check")
            if started and (updated is None or updated < started):
                missing.append("design_document_stale")
            elif not isinstance(check, dict):
                warnings.append("design consistency check not recorded (document stored by an older tool version)")
            else:
                contradictions = list(check.get("contradictions") or [])
                warnings.extend(check.get("warnings") or [])
                # The check must have run against the selections that are approved NOW.
                against = check.get("checked_against") or {}
                for field in ("migration_strategy", "target_compute", "target_database",
                              "cicd_target", "target_account", "vpc_cidr"):
                    now_val = str(meta.get(field) or "").strip().lower() or None
                    then_val = str(against.get(field) or "").strip().lower() or None
                    if field == "target_compute" and then_val and not now_val:
                        continue  # strategy-default compute (rehost/relocate → ec2)
                    if field in against and now_val != then_val:
                        contradictions.append(
                            f"{field}: design was checked against '{then_val}' but the approved value is now '{now_val}'."
                        )
                blockers.extend(contradictions)
                if contradictions:
                    missing.append("design_contradicts_approved_selections")
    # Other phases have no hard DDB contract yet -> non-blocking (valid).

    valid = len(missing) == 0
    logger.info("validate_phase app=%s phase=%s attempt=%d valid=%s missing=%s blockers=%s",
                app_id, phase, attempt, valid, missing, blockers)
    out = {"app_id": app_id, "phase": phase, "valid": valid, "missing": missing, "attempt": attempt,
           "blockers": blockers, "warnings": warnings}
    if design_version:
        out["design_version"] = design_version
    return out


def _get_meta(table, app_id):
    try:
        return table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    except Exception as e:
        logger.warning("_get_meta failed for %s: %s", app_id, e)
        return {}


# META fields the code-analysis worker writes from its repo scan.
_DETECTED_WIRING_FIELDS = ("detected_build_dir", "detected_language", "detected_runtime_version",
                           "detected_container_port", "detected_health_check_path", "detected_lambda_handler",
                           "detected_lambda_functions", "detected_dynamodb_tables")


def _latest_item_by_prefix(table, app_id, prefix):
    """Latest item under APP#<id> with SK begins_with(prefix), by lexical SK (ISO
    timestamps in these SKs sort chronologically). Returns {} if none found."""
    from boto3.dynamodb.conditions import Key
    try:
        resp = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(prefix),
        )
        items = resp.get("Items", [])
        if not items:
            return {}
        items.sort(key=lambda i: i.get("SK", ""))
        return items[-1]
    except Exception as e:
        logger.warning("_latest_item_by_prefix(%s) failed for %s: %s", prefix, app_id, e)
        return {}


def _build_decision_summary(step, app_id, table):
    """Deterministically compute decision_summary from already-persisted state.

    This is the fallback source of truth for every approval gate — see
    the decision-support design notes (R1/R2) and each phase skill's
    "Gate decision-summary" section. Those skills instruct the Harness agent to
    attach decision_summary when requesting a gate, but the ASL's store_token
    Arguments.Payload is a hardcoded JSON object the agent has no way to inject
    into (the agent's invokeHarness Task state has already completed and
    returned by the time the separate Approval_* Task runs). So decision_summary
    was never actually populated on any of the 6 gates since the feature was
    speced — this function closes that gap by reading the same persisted
    records _validate_phase already reads, deterministically, every time.

    Returns {} (never None) on any failure or unrecognized step — decision_summary
    is advisory UI content, so a computation failure must never block the gate
    the way a validate_phase failure correctly does.
    """
    try:
        meta = _get_meta(table, app_id)

        if step == "discovery_review":
            strategy = meta.get("migration_strategy") or "unknown"
            confidence = meta.get("strategy_confidence") or "unknown"
            rationale = meta.get("strategy_rationale") or ""
            return {
                "what_changed": f"Recommend {str(strategy).upper()} ({confidence}% confidence).",
                "confidence_or_risk": rationale or f"strategy_confidence={confidence}%",
                "if_approve": "Assessment runs the strategy-gated analyses for the recommended 7R strategy",
                "if_reject": "Discovery re-runs / strategy is manually overridden before assessment",
                "if_retry": "Re-enrich (e.g. after linking repos or supplying missing critical fields) to raise confidence",
            }

        if step == "assessment_review":
            analysis = _latest_item_by_prefix(table, app_id, "ANALYSIS#assessment-summary")
            result = (analysis.get("data") or {}).get("result") or analysis.get("result") or {}
            transformations = result.get("transformations") or {}
            n_rec = len(transformations.get("recommended") or [])
            n_gaps = len(transformations.get("gaps") or [])
            cost = result.get("cost_projection") or {}
            cost_status = (cost.get("current_annual_cost") or {}).get("status", "unknown")
            qual = result.get("qualification_criteria") or []
            n_qual_fail = sum(1 for c in qual if isinstance(c, dict) and c.get("pass") is False)
            consistency = list(analysis.get("consistency_warnings") or result.get("consistency_warnings")
                               or meta.get("assessment_warnings") or [])
            completeness = _completeness_missing(meta, "assessment", table, app_id)
            summary = {
                "what_changed": (
                    f"Assessed {app_id}: {meta.get('migration_strategy', 'unknown')} strategy; "
                    f"{n_rec} ATX-automatable transformation(s), {n_gaps} custom-authoring gap(s)."
                ),
                "confidence_or_risk": (
                    f"strategy_confidence={meta.get('strategy_confidence', 'unknown')}%; "
                    f"cost_projection.current_annual_cost status={cost_status}; "
                    f"qualification_criteria: {n_qual_fail} of {len(qual)} checks failed"
                    + (f"; {len(consistency)} consistency warning(s)" if consistency else "")
                    + (f"; completeness gate missing: {', '.join(completeness)}" if completeness else "")
                ),
                "if_approve": "Design phase generates V1 using the confirmed target_compute + transformation_steps",
                "if_reject": "Assessment re-runs with revised strategy/compute selection",
                "if_retry": "Re-run assessment analysis (e.g. after linking repos that were missing)",
            }
            warnings = consistency + ([f"completeness gate (assessment) missing: {', '.join(completeness)}"]
                                      if completeness else [])
            # CI readiness + detected wiring from the code-analysis worker.
            ci = (_latest_item_by_prefix(table, app_id, "ANALYSIS#ci-readiness").get("result") or {})
            readiness = ci.get("readiness") or {}
            items = [i for i in (readiness.get("items") or []) if isinstance(i, dict)]
            blockers = [f"CI readiness: {i.get('check')}: {i.get('evidence')}" for i in items if i.get("status") == "blocker"]
            fixable = [f"{i.get('check')}: {i.get('evidence')}" for i in items if i.get("status") == "fixable"]
            if readiness:
                summary["confidence_or_risk"] += (f"; ci_readiness={readiness.get('status', 'unknown')} "
                                                  f"({len(blockers)} blocker(s), {len(fixable)} auto-fixable at Execution)")
            warnings += blockers
            if fixable:
                summary["auto_fixable_at_execution"] = fixable
            wiring = {k[len("detected_"):]: meta[k] for k in _DETECTED_WIRING_FIELDS if meta.get(k) not in (None, "", [])}
            if wiring:
                summary["detected_wiring"] = wiring
            if warnings:
                summary["warnings"] = warnings
            return summary

        if step == "design_review":
            design = _latest_design(table, app_id, meta)
            version = meta.get("design_version") or design.get("version") or "unknown"
            check = design.get("consistency_check") if isinstance(design.get("consistency_check"), dict) else {}
            contradictions = list(check.get("contradictions") or meta.get("design_contradictions") or [])
            design_warnings = list(check.get("warnings") or meta.get("design_warnings") or [])
            completeness = _completeness_missing(meta, "design", table, app_id)
            if completeness:
                design_warnings.append(f"completeness gate (design) missing: {', '.join(completeness)}")
            summary = {
                "what_changed": (
                    f"Design {version} generated for {app_id} (strategy={meta.get('migration_strategy', 'unknown')}, "
                    f"target_compute={meta.get('target_compute', 'unknown')}, "
                    f"target_database={meta.get('target_database', 'unknown')}, "
                    f"cicd_target={meta.get('cicd_target', 'unknown')})."
                ),
                "confidence_or_risk": (
                    f"design_status={meta.get('design_status', 'unknown')}; "
                    f"{len(contradictions)} contradiction(s) with approved selections; "
                    f"{len(design_warnings)} warning(s)"
                ),
                "if_approve": "User provides required inputs (Account ID) → Design V2 (execution-ready) → Execution begins",
                "if_reject": "Design regenerated with corrected assumptions / different target selection",
                "if_retry": "Re-run design generation (e.g. after re-reading updated assessment findings)",
            }
            if contradictions:
                summary["contradictions"] = contradictions
            if design_warnings:
                summary["warnings"] = design_warnings
            return summary

        if step == "execution_review":
            # What was built for the approved compute + Execution's own notes, so the
            # reviewer sees e.g. a placeholder Lambda handler or a CI-only pipeline.
            iac = _latest_item_by_prefix(table, app_id, "EXECUTION#iac#")
            cicd = _latest_item_by_prefix(table, app_id, "EXECUTION#cicd#")
            approved = meta.get("target_compute", "unknown")
            built = iac.get("target_compute") or "none"
            warnings = list(meta.get("execution_database_warnings") or [])
            if iac and built != approved:
                warnings.insert(0, f"IaC was generated for target_compute={built}, but the approved value is {approved}.")
            warnings += [f"CI/CD: {w}" for w in (cicd.get("warnings") or [])]
            notes = [f"IaC: {n}" for n in (iac.get("notes") or [])] + [f"CI/CD: {n}" for n in (cicd.get("notes") or [])]
            ci_fix = _latest_item_by_prefix(table, app_id, "ANALYSIS#ci-fix").get("result") or {}
            if ci_fix:
                notes += [f"CI fix: {c}" for c in (ci_fix.get("changes") or [])]
                warnings += [f"CI fix: {w}" for w in (ci_fix.get("warnings") or [])]
                if ci_fix.get("status") in ("error", "partial"):
                    warnings.append(f"CI readiness fixer finished '{ci_fix.get('status')}' — the first pipeline run may fail.")
            summary = {
                "what_changed": (
                    f"Execution sub-tasks complete for {app_id} (target_compute={approved}, "
                    f"modules={', '.join(iac.get('modules') or []) or 'none'}, "
                    f"cicd_target={meta.get('cicd_target', 'unknown')}): "
                    f"infra={meta.get('exec_infra_status', 'unknown')}, "
                    f"cicd={meta.get('exec_cicd_status', 'unknown')}, "
                    f"database={meta.get('exec_database_status', 'unknown')}"
                ),
                "confidence_or_risk": (
                    f"{len(warnings)} warning(s); see notes for generated-artifact caveats" if warnings or notes
                    else "See per-sub-task status fields above for any skipped/failed step"
                ),
                "if_approve": ("Deploy: terraform apply of the infra/ committed at the reviewed commit, then the app's first "
                               "CodePipeline run on it, then Validation (platform + smoke/contract checks) on the live stack"),
                "if_reject": "Execution regenerates IaC / CI fix / CI-CD from the migration branch and returns to this gate",
                "if_retry": "Edit the migration branch, then Retry to regenerate before deploying",
            }
            # Deploy gate: the exact commit the approval deploys.
            head = str(meta.get("exec_head_commit") or "")
            repo_name = str(cicd.get("repo_name") or iac.get("repo_name") or "")
            if head:
                summary["head_commit"] = head
                summary["review_branch"] = cicd.get("branch") or iac.get("branch") or ""
                if repo_name:
                    summary["review_url"] = (f"https://{REGION}.console.aws.amazon.com/codesuite/codecommit/repositories/"
                                             f"{repo_name}/commit/{head}?region={REGION}")
            if meta.get("exec_deploy_status") in ("error", "apply_failed", "pipeline_failed", "branch_moved", "plan_destroys"):
                warnings.insert(0, f"Last deploy attempt: {meta.get('exec_deploy_status')} — {meta.get('exec_deploy_error') or 'see the worker log'}")
            elif meta.get("exec_deploy_status") == "deployed" and meta.get("validation_status") == "fail":
                # Back here from a failed Validation (Check_Validation): say what failed.
                vr = _latest_item_by_prefix(table, app_id, "ANALYSIS#validation-results#")
                failed = [f"{c.get('check')}: {str(c.get('detail') or '')[:160]}"
                          for c in ((vr.get("result") or {}).get("checks") or []) if c.get("result") == "fail"]
                warnings.insert(0, "Last Validation FAILED on the deployed commit — fix on the migration branch, then "
                                   "Retry (regenerate) and approve the new head: " + "; ".join(failed[:6]))
            if warnings:
                summary["warnings"] = warnings
            if notes:
                summary["notes"] = notes[:20]
            return summary

        if step == "validation_review":
            # tool_execution.validate_deployment's deterministic checks.
            rec = _latest_item_by_prefix(table, app_id, "ANALYSIS#validation-results#")
            res = rec.get("result") or {}
            checks = res.get("checks") or []
            summary = {
                "what_changed": f"Validation for {app_id}: verdict={res.get('verdict', meta.get('validation_status', 'unknown'))} "
                                f"({sum(1 for c in checks if c.get('result') == 'pass')}/{len(checks)} checks passed)",
                "confidence_or_risk": "; ".join(f"{c.get('check')}={c.get('result')}" for c in checks) or "no validation record",
                "if_approve": "Proceed to cutover / handover (runbook generation)",
                "if_reject": "Return to execution to remediate the failed check",
                "if_retry": "Deploy (or fix) and re-run validation",
            }
            warns = [f"{c.get('check')}: {c.get('detail')}" for c in checks if c.get("result") in ("warn", "not_deployed")]
            if warns:
                summary["warnings"] = warns
            return summary

        if step == "handover_review":
            # Close-out gate (retire/retain/repurchase): the DESIGN#final document,
            # checked at store time against the app's recorded facts.
            design = _latest_design(table, app_id, meta)
            check = design.get("consistency_check") if isinstance(design.get("consistency_check"), dict) else {}
            contradictions = list(check.get("contradictions") or [])
            close_warnings = list(check.get("warnings") or [])
            strategy = meta.get("migration_strategy", "unknown")
            summary = {
                "what_changed": f"Close-out document ({design.get('version') or 'final'}) generated for {app_id} "
                                f"(strategy={strategy}).",
                "confidence_or_risk": (
                    f"{len(contradictions)} contradiction(s) with the app record; {len(close_warnings)} warning(s)"
                    + (" — the automatic re-run did not resolve them; review before approving" if contradictions else "")
                ),
                "if_approve": f"The {strategy} close-out is recorded as final and the journey completes",
                "if_reject": "The close-out document is regenerated with your comment",
                "if_retry": "Correct the app record (owners, RPO/RTO, deadlines, …) then reject to regenerate",
            }
            if contradictions:
                summary["contradictions"] = contradictions
            if close_warnings:
                summary["warnings"] = close_warnings
            return summary

        if step == "cutover_review":
            return {
                "what_changed": f"Cutover prepared for {app_id} (status={meta.get('status', 'unknown')})",
                "confidence_or_risk": "No dedicated cutover skill contract yet — this summary is a minimal fallback",
                "if_approve": "Proceed to handover",
                "if_reject": "Return to validation / execution to address blockers",
                "if_retry": "Re-prepare cutover",
            }

        return {}
    except Exception as e:
        logger.warning("_build_decision_summary failed for app=%s step=%s: %s", app_id, step, e)
        return {}


def _store_task_token(event):
    """Store SFN task token in DynamoDB for the approval UI.
    Also updates WORKFLOW#ACTIVE to reflect the current phase.

    ``decision_summary`` (optional): a small object the requesting phase can
    attach describing what changed, its confidence/risk, and the concrete
    consequence of approve/reject/retry — see
    the decision-support design notes (R1/R2).

    The ASL's store_token call sites (Approval_* Task states) hardcode their
    Arguments.Payload and run AFTER the Harness agent's invokeHarness Task has
    already completed and returned — there is no mechanism for the agent to
    inject anything into this call. So an explicit caller-provided
    decision_summary is accepted and stored verbatim when present (keeps this
    backward compatible with any future ASL change that does thread it
    through), but when absent — the case for every gate today — this function
    computes a deterministic fallback via _build_decision_summary instead of
    silently omitting the field.
    """
    app_id = event.get("app_id", "")
    step = event.get("step", "")
    token = event.get("token", "")
    decision_summary = event.get("decision_summary")

    if not all([app_id, step, token]):
        return {"status": "error", "message": "app_id, step, and token required"}

    table = ddb.Table(DDB_TABLE)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not (isinstance(decision_summary, dict) and decision_summary):
        try:
            decision_summary = _build_decision_summary(step, app_id, table)
        except Exception:
            # decision_summary is advisory UI content, never the gate mechanism
            # itself (the task_token write below is). A bug in the fallback
            # computation must never block storing the token and hanging the
            # workflow -- defense-in-depth on top of _build_decision_summary's
            # own internal try/except, in case a future edit there removes it.
            logger.warning("decision_summary fallback failed for app=%s step=%s", app_id, step, exc_info=True)
            decision_summary = {}

    # Store the task token. This MUST succeed: the SFN gate is paused on this
    # token via waitForTaskToken, so if we cannot persist it the approval UI has
    # nothing to send SendTaskSuccess against and the execution would hang
    # forever. Raising here makes the invoke fail, so the gate's
    # `States.ALL -> MarkFailed` Catch fires (fail loud) instead of a silent hang.
    try:
        item = {
            "PK": f"APP#{app_id}",
            "SK": f"TASK_TOKEN#{step}",
            "GSI1PK": "TYPE#TASK_TOKEN",
            "GSI1SK": f"{app_id}#{step}",
            "app_id": app_id,
            "step": step,
            "task_token": token,
            "message": f"Approval required: {step.replace('_', ' ').title()}",
            "received_at": now,
        }
        if isinstance(decision_summary, dict) and decision_summary:
            item["decision_summary"] = decision_summary
        table.put_item(Item=item)
    except Exception:
        logger.exception("FATAL: could not persist the approval-gate record app=%s step=%s — failing gate", app_id, step)
        raise

    # Update WORKFLOW#ACTIVE with the current phase
    # The step name tells us which phase just completed
    step_to_phase = {
        "discovery_review": "discovery",
        "assessment_review": "assessment",
        "design_review": "design",
        "planning_review": "planning",
        "execution_review": "execution",
        "validation_review": "validation",
        "cutover_review": "cutover",
        "handover_review": "handover",
        "final_approval": "validation",
    }
    phase = step_to_phase.get(step, "unknown")

    try:
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"},
            UpdateExpression="SET current_phase = :p, current_step = :s, #st = :status",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":p": phase, ":s": step, ":status": "RUNNING"},
        )
    except Exception:
        # WORKFLOW#ACTIVE might not exist if started via direct SFN (not API)
        # Create it
        table.put_item(Item={
            "PK": f"APP#{app_id}",
            "SK": "WORKFLOW#ACTIVE",
            "GSI1PK": "TYPE#WORKFLOW",
            "GSI1SK": app_id,
            "app_id": app_id,
            "status": "RUNNING",
            "current_phase": phase,
            "current_step": step,
            "started_at": now,
        })

    logger.info("Approval gate recorded: app=%s step=%s phase=%s", app_id, step, phase)
    return {"status": "token_stored", "app_id": app_id, "step": step}


def _update_workflow_phase(app_id, mode):
    """Update WORKFLOW#ACTIVE so frontend shows correct phase."""
    try:
        table = ddb.Table(DDB_TABLE)
        phase_map = {
            "discovery": "discovery", "design_v1": "discovery",
            "assessment": "assessment", "design_v2": "assessment",
            "planning": "planning", "design_v3": "planning",
            "execution": "execution", "design_v4": "execution",
            "validation": "validation", "design_v5": "validation",
            "handover": "handover",
        }
        phase = phase_map.get(mode, mode)
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"},
            UpdateExpression="SET current_phase = :p, current_step = :s",
            ExpressionAttributeValues={":p": phase, ":s": f"{mode}_running"},
        )
    except Exception as e:
        logger.warning("Phase update failed for %s: %s", app_id, e)
