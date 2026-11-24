"""Gateway Tool target: Strategy — 7R recommendation, completeness scoring, repo detection."""
import json
import logging
import os
import re

import boto3
from agent_tools import DecimalEncoder, ToolRegistry
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

TABLE = os.environ["DYNAMODB_TABLE"]
REGION = os.environ["AWS_REGION"]  # set by the Lambda runtime
ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(TABLE)

registry = ToolRegistry("strategy")


def handler(event, ctx=None):
    return registry.dispatch(event, ctx)


# ── Strategy recommendation ──────────────────────────────────────────

# Fields the strategy scan is allowed to read. Deliberately an allowlist.
#
# Scanning the whole META record is what caused APP-CATALOG to be classified
# 'repurchase': the trigger "erp" matched inside "enterprise" in the string
# "licensing details (byol, open-source, enterprise)" held in completeness_detail
# — a field this tool's own sibling produces. Worse, strategy_rationale then
# recorded "matched trigger(s): ['erp']", so the next run matched "erp" in its
# OWN previous output and the app could never escape the classification.
#
# So: read only fields a human authored to describe the application, never a
# field derived by this pipeline (strategy_rationale, recommended_rationale,
# completeness_detail, risk_*, … are all deliberately absent).
_STRATEGY_FIELDS = (
    "app_name", "description", "business_unit", "architecture", "notes",
    "data_classification", "compliance", "language", "framework", "app_server",
    "tier", "deployment_model", "deployment_method", "migration_notes",
    # L2 environment fields (discovery.md Step 1 "Environment")
    "current_compute", "current_region", "containerization", "licensing",
    "db_migration_method", "target_db_engine",
)


def _strategy_corpus(meta: dict) -> str:
    """Human-authored description text only, lowercased."""
    parts = []
    for f in _STRATEGY_FIELDS:
        v = meta.get(f)
        if v in (None, "", [], {}):
            continue
        parts.append(v if isinstance(v, str) else json.dumps(v, cls=DecimalEncoder))
    return " ".join(parts).lower()


def _mentions(corpus: str, trigger: str) -> bool:
    """Whole-word (or whole-phrase) match, not a substring match.

    "erp" must not match "enterprise", "simple" must not match "simpler", and
    "crm" must not match a base64 fragment. Word boundaries are what make a
    three-letter trigger safe to keep in the rule table.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(trigger)}(?![a-z0-9])", corpus) is not None


def _human_has_approved(app_id: str, meta: dict) -> bool:
    """True when a human has approved a gate for this app IN THE CURRENT RUN.

    Deliberately reads the APPROVAL_AUDIT# records rather than a field on META.
    The approve endpoint writes last_approved_at to the WORKFLOW#ACTIVE record,
    NOT to META — an earlier version of this guard checked META for it, found
    nothing, and so never fired.

    The audit trail is append-only and survives the new-run reset, so it must be
    scoped to the run: a previous run's discovery approval used to lock the
    strategy for every later run, even though each new run re-decides it and the
    reviewer re-confirms it at that run's discovery gate. The approve endpoint
    stamps execution_arn on each audit item; only items matching the
    WORKFLOW#ACTIVE execution_arn count. Legacy items without execution_arn never
    count for a run. With no active run on record (direct tool call outside a
    workflow) any audit item counts, as before.

    An explicit strategy_confirmed_at on META is honoured too, for callers that
    want to mark a confirmation without going through a gate.
    """
    if meta.get("strategy_confirmed_at"):
        return True
    try:
        wf = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "WORKFLOW#ACTIVE"}).get("Item") or {}
        run_arn = wf.get("execution_arn") or ""
        kw = {
            "KeyConditionExpression": Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("APPROVAL_AUDIT#"),
        }
        while True:
            resp = table.query(**kw)
            for item in resp.get("Items", []):
                if item.get("approved") is False:
                    continue  # a rejection is not a sign-off
                if not run_arn or item.get("execution_arn") == run_arn:
                    return True
            if not resp.get("LastEvaluatedKey"):
                return False
            kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception:
        # Fail SAFE: if the audit trail cannot be read we cannot prove the
        # strategy is unconfirmed, so treat it as confirmed and decline to
        # overwrite. Refusing to change a decision is recoverable; silently
        # re-routing a workflow is not.
        return True


# ── 7R rules (backend/resources/skills/discovery.md "7R Strategy Decision Rules") ──
#
# Two kinds of evidence:
#   • keyword rules — whole-word triggers in the human-authored corpus above;
#   • signal rules  — deterministic checks on structured L2 fields
#     (current_compute, containerization, deployment_method, change_history, …),
#     implementing discovery.md's compound triggers.
#
# Precedence (discovery.md: "evaluated in priority order … highest confidence wins"):
#   1. close-out intent (retire/retain/repurchase) — explicit human statements;
#   2. structured signal rules (relocate/rehost/replatform/refactor);
#   3. keyword-only modernization matches — a lone "microservices" in a
#      description is one weak signal (refactor needs 3+ per discovery.md §7), so it
#      may not override compound evidence from the structured fields;
#   within a class the higher confidence wins, ties broken by _PRIORITY order.
# Every candidate lists the triggers it matched, so the choice stays explainable.

STRATEGY_RULES = [
    {"strategy": "retire", "triggers": ["deprecated", "decommission", "replaced", "zero users"], "confidence": 88},
    {"strategy": "retain", "triggers": ["restricted data", "legal hold", "fedramp", "vendor-locked"], "confidence": 72},
    {"strategy": "repurchase", "triggers": ["crm", "erp", "itsm", "saas evaluation"], "confidence": 70},
    {"strategy": "rehost", "triggers": ["lift-and-shift", "datacenter closure", "simple"], "confidence": 80},
    {"strategy": "replatform", "triggers": ["containerize", "managed db", "modernize infra"], "confidence": 75},
    {"strategy": "refactor", "triggers": ["microservices", "cloud-native", "re-architect"], "confidence": 65},
]

_PRIORITY = ["retire", "retain", "repurchase", "relocate", "rehost", "replatform", "refactor"]
_CLOSE_OUT = {"retire", "retain", "repurchase"}

# Scoring tunables — env vars set in lambdas.tf; defaults are the original values.
DEFAULT_CONFIDENCE = int(os.environ.get("STRATEGY_DEFAULT_CONFIDENCE", "60"))
DEFAULT_RECOMMENDATION = {"strategy": "rehost", "confidence": DEFAULT_CONFIDENCE,
                          "rationale": "Default: insufficient data for a confident recommendation"}
# Confidence floor after the completeness penalty; also the confidence reported
# for a human-confirmed strategy the engine found no evidence for.
CONFIDENCE_FLOOR = int(os.environ.get("STRATEGY_CONFIDENCE_FLOOR", "40"))
# Annual license cost (USD) above which licensing counts as a replatform signal.
LICENSE_COST_THRESHOLD = float(os.environ.get("STRATEGY_LICENSE_COST_THRESHOLD", "30000"))
# Replatform confidence = base + per_signal x signals, capped.
REPLATFORM_BASE_CONFIDENCE = int(os.environ.get("STRATEGY_REPLATFORM_BASE_CONFIDENCE", "60"))
REPLATFORM_PER_SIGNAL = int(os.environ.get("STRATEGY_REPLATFORM_PER_SIGNAL", "8"))
REPLATFORM_MAX_CONFIDENCE = int(os.environ.get("STRATEGY_REPLATFORM_MAX_CONFIDENCE", "85"))
# Completeness (%) adjustment: above HIGH add BONUS (capped at MAX); below LOW subtract PENALTY (floored).
COMPLETENESS_HIGH = int(os.environ.get("STRATEGY_COMPLETENESS_HIGH", "80"))
COMPLETENESS_LOW = int(os.environ.get("STRATEGY_COMPLETENESS_LOW", "40"))
COMPLETENESS_BONUS = int(os.environ.get("STRATEGY_COMPLETENESS_BONUS", "10"))
COMPLETENESS_PENALTY = int(os.environ.get("STRATEGY_COMPLETENESS_PENALTY", "15"))
COMPLETENESS_MAX_CONFIDENCE = int(os.environ.get("STRATEGY_COMPLETENESS_MAX_CONFIDENCE", "95"))
_VMWARE = ("vmware", "vsphere", "esxi")
_DC_EXIT_PHRASES = ("dc exit", "datacenter exit", "data center exit", "datacenter closure",
                    "data center closure", "lease expiry", "lease expiring", "no code change", "no-change")
_LEGACY_LANGS = ("cobol", "rpg", "fortran", "pl/i", "pl1", "natural")
_MODERN_LANG_WORDS = ("java", "python", "node", "nodejs", "javascript", "typescript", "go", "golang", "ruby", "php")
_MODERN_LANG_SUBSTR = ("c#", ".net", "dotnet", "node.js")
_NOT_CONTAINERIZED = ("none", "no", "false", "not containerized", "n/a")
_CONTAINERIZED = ("yes", "full", "docker", "kubernetes", "k8s", "eks", "ecs", "containerized", "containerised")


def _s(v) -> str:
    return str(v if v is not None else "").strip().lower()


def _sub(meta: dict, obj: str, key: str):
    o = meta.get(obj)
    return o.get(key) if isinstance(o, dict) else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_modern_language(lang: str) -> bool:
    return any(_mentions(lang, w) for w in _MODERN_LANG_WORDS) or any(t in lang for t in _MODERN_LANG_SUBSTR)


# Runtimes past end of life — an upgrade path that replatforming delivers.
_EOL_RUNTIME_RE = re.compile(
    r"\b(?:python\s*(?:2(?:\.\d+)?|3\.[0-8])(?!\d)"
    r"|(?:java|jdk|jre|openjdk|corretto)\s*(?:1\.)?[5-8](?!\d)"
    r"|node(?:\.?js)?\s*(?:[4-9]|1[0-6])(?!\d))", re.I)
_SERVERLESS_SHAPES = ("serverless", "function", "lambda", "faas")
_ORCHESTRATED = ("kubernetes", "k8s", "eks", "ecs", "openshift", "nomad", "swarm")


def _eol_runtime(meta: dict) -> str:
    """The first end-of-life runtime named in the structured runtime fields / description."""
    rt = meta.get("runtime") if isinstance(meta.get("runtime"), dict) else {}
    fields = [meta.get("runtime_version"), meta.get("language_version"), meta.get("description"),
              *(f"{k} {v}" for k, v in rt.items())]
    lang = str(meta.get("language") or "")
    if meta.get("language_version"):
        fields.append(f"{lang} {meta.get('language_version')}")
    for f in fields:
        m = _EOL_RUNTIME_RE.search(str(f or ""))
        if m:
            return m.group(0)
    return ""


def _signal_candidates(meta: dict, corpus: str) -> list[dict]:
    """discovery.md's structured (non-keyword) triggers → candidate recommendations."""
    out = []
    lang, arch = _s(meta.get("language")), _s(meta.get("architecture"))
    compute, containers = _s(meta.get("current_compute")), _s(meta.get("containerization"))
    deploy, tier = _s(meta.get("deployment_method")), _s(meta.get("tier"))
    active_dev = _sub(meta, "change_history", "active_development")
    monolith = any(t in arch for t in ("monolith", "soa", "n-tier", "3-tier", "layered"))

    # §4 RELOCATE — VMware-dependent workload with a DC-exit / lease / no-change signal.
    if any(t in compute for t in _VMWARE):
        exits = []
        lease = _sub(meta, "constraints", "dc_lease_end") or meta.get("dc_lease_end")
        if lease:
            exits.append(f"dc_lease_end={lease}")
        exits += [f"'{p}'" for p in _DC_EXIT_PHRASES if _mentions(corpus, p)]
        if exits:
            out.append({"strategy": "relocate", "confidence": min(70 + 5 * (len(exits) - 1), 80),
                        "matched": [f"current_compute={meta.get('current_compute')}"] + exits})

    # §5 REHOST — legacy language; tier-3 monolith in maintenance mode.
    rehost = []
    legacy = [lg for lg in _LEGACY_LANGS if _mentions(lang, lg)]
    if legacy:
        rehost.append(f"legacy language ({legacy[0]})")
    if tier == "tier_3" and monolith and active_dev is False:
        rehost.append("tier_3 monolith, no active development")
    if rehost:
        out.append({"strategy": "rehost", "confidence": min(70 + 5 * (len(rehost) - 1), 80), "matched": rehost})

    # §6 REPLATFORM — compound, needs 2+ signals.
    rp = []
    # "partial (Docker)": an image exists but nothing orchestrates it — still to containerize.
    partial = "partial" in containers or ("docker" in containers and not any(o in containers for o in _ORCHESTRATED))
    if _is_modern_language(lang) and monolith and (containers in _NOT_CONTAINERIZED or partial):
        state = "partially containerized" if partial else "not containerized"
        rp.append(f"modern language ({meta.get('language')}) + {meta.get('architecture')} + {state}")
    if partial and not monolith:
        rp.append(f"partially containerized ({meta.get('containerization')}) — no orchestrator yet")
    # Function-shaped code still run on servers (cron / HTTP shim) → Lambda.
    framework = _s(meta.get("framework"))
    if (any(t in arch for t in _SERVERLESS_SHAPES) or "lambda handler" in framework or "serverless" in framework) \
            and "lambda" not in compute:
        rp.append(f"function-shaped app ({meta.get('architecture') or meta.get('framework')}) not on Lambda "
                  f"(current_compute={meta.get('current_compute') or 'unknown'})")
    eol = _eol_runtime(meta)
    if eol:
        rp.append(f"end-of-life runtime ({eol})")
    engine = _s(_sub(meta, "database", "engine"))
    if any(t in engine for t in ("dynamo", "aurora", "rds")):
        rp.append(f"managed database source ({_sub(meta, 'database', 'engine')}) — no data migration")
    lic = _s(_sub(meta, "database", "licensing") or meta.get("licensing"))
    if (("oracle" in engine or "sql server" in engine or "sqlserver" in engine or "mssql" in engine)
            and any(t in lic for t in ("enterprise", "commercial", "byol", "license", "proprietary"))):
        rp.append(f"{_sub(meta, 'database', 'engine')} with {lic} licensing")
    if "manual" in deploy:
        rp.append("manual deployment")
    annual = _num(_sub(meta, "current_cost", "annual_license"))
    if annual is not None and annual > LICENSE_COST_THRESHOLD:
        rp.append(f"annual license ${annual:,.0f} > ${LICENSE_COST_THRESHOLD / 1000:g}K")
    if len(rp) >= 2:
        conf = min(REPLATFORM_BASE_CONFIDENCE + REPLATFORM_PER_SIGNAL * len(rp), REPLATFORM_MAX_CONFIDENCE)
        out.append({"strategy": "replatform", "confidence": conf, "matched": rp})

    # §7 REFACTOR — compound, needs 3+ signals.
    rf = []
    if tier == "tier_1" and active_dev is True:
        rf.append("tier_1 with active development")
    team = _num(_sub(meta, "change_history", "team_size"))
    if team is not None and team >= 6 and monolith:
        rf.append(f"team of {team:.0f} on a monolith")
    if "microservice" in arch and containers.startswith(_CONTAINERIZED):
        rf.append("already-containerized microservices")
    freq = _s(_sub(meta, "change_history", "release_frequency"))
    if "daily" in freq or "multiple" in freq or "continuous" in freq:
        rf.append(f"release frequency {freq}")
    if len(rf) >= 3:
        out.append({"strategy": "refactor", "confidence": min(65 + 8 * (len(rf) - 2), 90), "matched": rf})

    for c in out:
        c["evidence"] = "signals"
    return out


def _keyword_candidates(corpus: str) -> list[dict]:
    out = []
    for rule in STRATEGY_RULES:
        matched = [t for t in rule["triggers"] if _mentions(corpus, t)]
        if matched:
            out.append({"strategy": rule["strategy"], "confidence": min(rule["confidence"] + len(matched) * 5, 95),
                        "matched": matched, "evidence": "keywords"})
    return out


def _evidence_class(c: dict) -> int:
    if c["strategy"] in _CLOSE_OUT:
        return 2
    return 1 if c["evidence"] == "signals" else 0


def _rank(c: dict):
    return (_evidence_class(c), c["confidence"], -_PRIORITY.index(c["strategy"]))


def _describe(c: dict) -> str:
    if c["evidence"] == "keywords":
        return f"Matched trigger(s): {c['matched']}"
    return f"Matched {c['strategy']} signal(s): {c['matched']}"


def _evaluate_rules(meta: dict) -> tuple[dict, list[dict]]:
    """(best recommendation, all candidates) — deterministic and explainable."""
    corpus = _strategy_corpus(meta)
    candidates = _signal_candidates(meta, corpus) + _keyword_candidates(corpus)
    if not candidates:
        return dict(DEFAULT_RECOMMENDATION), []
    candidates.sort(key=_rank, reverse=True)
    top = candidates[0]
    rationale = _describe(top)
    others = [c for c in candidates[1:] if c["strategy"] != top["strategy"]]
    if others:
        rationale += "; also considered: " + "; ".join(
            f"{c['strategy']} ({c['confidence']}%, {c['evidence']}: {c['matched']})" for c in others
        )
    return {"strategy": top["strategy"], "confidence": top["confidence"], "rationale": rationale}, candidates


def _adjust_for_completeness(conf: int, completeness: int) -> int:
    if completeness > COMPLETENESS_HIGH:
        return min(conf + COMPLETENESS_BONUS, COMPLETENESS_MAX_CONFIDENCE)
    if completeness < COMPLETENESS_LOW:
        return max(conf - COMPLETENESS_PENALTY, CONFIDENCE_FLOOR)
    return conf


def _confirmed_note(confirmed: str, best: dict) -> str:
    """strategy_rationale for a human-confirmed strategy.

    Same wording as backend/src/api/v1/workflow._confirmed_strategy_rationale
    (which applies it when the reviewer changes the strategy at discovery_review)
    — keep in sync.
    """
    if best["strategy"] == confirmed:
        return f"Human-confirmed {confirmed}; engine agrees ({best['confidence']}%): {best['rationale']}"
    return (f"Human-confirmed {confirmed}; engine recommended {best['strategy']} "
            f"({best['confidence']}%) because {best['rationale']}")


# ── Completeness ─────────────────────────────────────────────────────────────
#
# Duplicate of backend/src/core/completeness.py (COMPLETENESS_FIELDS, PHASE_GATES,
# _is_filled and the scoring formula) — this Lambda cannot import backend/. KEEP IN
# SYNC: backend/tests/unit/test_journey_discovery_rules.py asserts the two agree,
# so the tool's gates and missing-field list match what the UI shows.

COMPLETENESS_FIELDS = {
    "basic": {"weight": 15, "fields": ["app_name", "description", "business_unit", "tier"]},
    "infrastructure": {"weight": 15, "fields": ["machine_ids", "machine_count"]},
    "technical": {"weight": 15, "fields": ["language", "framework", "architecture"]},
    "requirements": {"weight": 20, "fields": ["compliance", "rpo", "rto", "target_region", "data_classification"]},
    "ownership": {"weight": 5, "fields": ["owners"]},
    "code": {"weight": 10, "fields": ["repos"]},
    "operational": {"weight": 10, "fields": ["change_freeze_windows", "dependencies_upstream", "dependencies_downstream"]},
    "migration_readiness": {"weight": 10, "fields": ["licensing", "db_migration_method", "startup_shutdown_procedures"]},
}

_GATE_BASE = ["app_name", "tier", "machine_ids", "compliance", "rpo", "rto", "target_region"]
PHASE_GATES = {
    "assessment": _GATE_BASE,
    "wave_planning": _GATE_BASE + ["migration_strategy", "owners"],
    "design": _GATE_BASE + ["migration_strategy", "owners", "data_classification", "uptime_sla"],
    "transformation": _GATE_BASE + ["migration_strategy", "owners", "data_classification", "repos"],
}

# owners/repos live as OWNER#/REPO# sibling items, not on META; their counts stand in.
_COUNT_FALLBACK = {"owners": "owner_count", "repos": "repo_count"}


def _is_filled(data: dict, field: str) -> bool:
    value = data.get(field)
    if field in _COUNT_FALLBACK and not value:
        value = data.get(_COUNT_FALLBACK[field])
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    if isinstance(value, bool):
        return value
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return bool(value)


def _with_sibling_counts(app_id: str, meta: dict) -> dict:
    """META plus owner_count/repo_count counted from the OWNER#/REPO# sibling items."""
    data = dict(meta)
    for prefix, field in (("OWNER#", "owner_count"), ("REPO#", "repo_count")):
        try:
            resp = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(prefix),
                Select="COUNT",
            )
            data[field] = resp.get("Count", 0)
        except Exception as _err:
            logger.debug("best-effort step ignored: %s", _err)  # keep whatever META carries; completeness is advisory
    return data


@registry.tool(
    "recommend_strategy",
    description=(
        "Recommend a 7R migration strategy for an app. Persists the recommendation UNLESS a "
        "human has already confirmed a strategy at a gate of the current run, in which case "
        "the recommendation is returned (and stored as recommended_*) without overwriting "
        "the confirmed value."
    ),
    schema={
        "app_id": {"type": "string", "required": True},
        "force": {"type": "boolean", "description": "Overwrite a human-confirmed strategy (requires an explicit decision)"},
    },
)
def recommend_strategy(args):
    app_id = args["app_id"]
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item")
    if not meta:
        return {"error": f"App {app_id} not found"}

    best, candidates = _evaluate_rules(meta)
    completeness = _calc_completeness(_with_sibling_counts(app_id, meta))
    best["confidence"] = _adjust_for_completeness(best["confidence"], completeness)
    # Per-strategy engine confidence, so a strategy the human confirms instead of
    # the recommendation can still be reported with the engine's own number.
    scores: dict = {}
    for c in candidates:
        adj = _adjust_for_completeness(c["confidence"], completeness)
        scores[c["strategy"]] = max(scores.get(c["strategy"], 0), adj)
    scores[best["strategy"]] = best["confidence"]

    recommended = {
        ":rs": best["strategy"], ":rc": str(best["confidence"]), ":rr": best["rationale"],
        ":sc": {k: str(v) for k, v in scores.items()},
    }
    rec_expr = "recommended_strategy = :rs, recommended_confidence = :rc, recommended_rationale = :rr, strategy_scores = :sc"

    # A strategy confirmed by a human at a gate is a DECISION, not a suggestion to
    # be re-derived. Overwriting it silently re-routes the workflow: a confirmed
    # 'refactor' replaced by 'repurchase' sends the run straight to close-out,
    # skipping assessment, design and transform, and the run still reports SUCCESS.
    # So recommend, report the disagreement, and leave the decision alone.
    confirmed = (meta.get("migration_strategy") or "").strip().lower()
    if confirmed and not args.get("force") and _human_has_approved(app_id, meta):
        # The strategy itself stays locked (never silently re-routed — see above),
        # but strategy_confidence/strategy_rationale still need writing every run:
        # Validate_Discovery's deterministic check (invoke_runtime.py's
        # validate_phase) requires strategy_confidence to be non-empty before
        # Discovery can pass, and nothing else in this phase sets it. Without
        # this, a confirmed strategy wedges Discovery into an infinite re-run
        # loop (bounded to 2 attempts, then MarkFailed) on every future run —
        # discovered 2026-10-02 testing APP-CATALOG after the Harness fix.
        #
        # They must describe the CONFIRMED strategy, not the recommendation
        # (APP-TE showed 'rehost' with refactor's "Matched trigger(s):
        # ['microservices']"); the recommendation goes to recommended_*.
        confidence = scores.get(confirmed, CONFIDENCE_FLOOR)
        note = _confirmed_note(confirmed, best)
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression=f"SET strategy_confidence = :c, strategy_rationale = :r, {rec_expr}",
            ExpressionAttributeValues={":c": str(confidence), ":r": note, **recommended},
        )
        return {
            "app_id": app_id,
            "strategy": confirmed,
            "confidence": confidence,
            "confirmed": True,
            "persisted": False,
            "recommendation": best["strategy"],
            "recommendation_confidence": best["confidence"],
            "agrees_with_confirmed": best["strategy"] == confirmed,
            "rationale": f"{note} Pass force=true to override a confirmed decision.",
            "completeness": completeness,
        }

    table.update_item(
        Key={"PK": f"APP#{app_id}", "SK": "META"},
        UpdateExpression=f"SET migration_strategy = :s, strategy_confidence = :c, strategy_rationale = :r, {rec_expr}",
        ExpressionAttributeValues={":s": best["strategy"], ":c": str(best["confidence"]), ":r": best["rationale"],
                                   **recommended},
    )
    return {"app_id": app_id, **best, "persisted": True, "completeness": completeness}


@registry.tool(
    "calculate_completeness",
    description="Score an app's data completeness by category and report phase gates + missing fields.",
    schema={"app_id": {"type": "string", "required": True}},
)
def calculate_completeness(args):
    app_id = args["app_id"]
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item")
    if not meta:
        return {"error": f"App {app_id} not found"}
    return {"app_id": app_id, **_calc_completeness_detailed(_with_sibling_counts(app_id, meta))}


def _calc_completeness(meta):
    return _calc_completeness_detailed(meta)["score_percent"]


def _calc_completeness_detailed(meta):
    """Same formula as core/completeness.calculate_completeness (per-category rounding)."""
    categories, missing, earned = {}, [], 0
    total = sum(c["weight"] for c in COMPLETENESS_FIELDS.values())
    for name, cat in COMPLETENESS_FIELDS.items():
        filled = [f for f in cat["fields"] if _is_filled(meta, f)]
        empty = [f for f in cat["fields"] if not _is_filled(meta, f)]
        earned += round(cat["weight"] * (len(filled) / len(cat["fields"])))
        categories[name] = {"score": round(len(filled) / len(cat["fields"]) * 100), "filled": filled, "missing": empty}
        missing.extend(empty)
    score = round(earned / total * 100)
    gate_missing = {g: [f for f in fields if not _is_filled(meta, f)] for g, fields in PHASE_GATES.items()}
    return {
        "score_percent": score,
        "categories": categories,
        "missing_fields": missing,
        "phase_gates": {g: not m for g, m in gate_missing.items()},
        "phase_gate_missing": gate_missing,
    }



