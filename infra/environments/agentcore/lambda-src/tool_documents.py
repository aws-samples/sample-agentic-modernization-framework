"""Gateway Tool target: Documents — design docs, analysis results, reports."""
import json
import logging
import os
import re
import time

import boto3
import risk_scoring
from agent_tools import ToolRegistry, json_safe, query_all
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

TABLE = os.environ["DYNAMODB_TABLE"]
BUCKET = os.environ.get("S3_BUCKET", "")
REGION = os.environ["AWS_REGION"]  # set by the Lambda runtime
ddb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
table = ddb.Table(TABLE)

registry = ToolRegistry("documents")

# Characters of a stored design document returned inline by get_design_document (rest stays in S3).
DESIGN_CONTENT_MAX_CHARS = int(os.environ.get("DESIGN_CONTENT_MAX_CHARS", "8000"))

_INTERNAL = ("PK", "SK", "GSI1PK", "GSI1SK")


def handler(event, ctx=None):
    return registry.dispatch(event, ctx)


# ── Strategy rules ────────────────────────────────────────────────────────────
# Mirror of backend/src/core/strategy_options.py (the Lambda zip cannot import
# backend/) — keep in sync. Only replatform/refactor change code; rehost /
# relocate run AS-IS on EC2; retire / retain / repurchase are close-outs (no AWS target).
_TRANSFORM_STRATEGIES = {"replatform", "refactor", "rearchitect"}  # rearchitect: legacy alias of refactor
_LIFT_STRATEGIES = {"rehost", "relocate"}
_CLOSE_OUT_STRATEGIES = {"retire", "retain", "repurchase"}



def _strategy_default_compute(strategy):
    if strategy in _LIFT_STRATEGIES:
        return "ec2"
    if strategy in _CLOSE_OUT_STRATEGIES:
        return "none"
    return ""


def _expected_compute(strategy, meta):
    """The compute the assessment/design must be consistent with: the strategy
    default for lift/close-out strategies, otherwise the confirmed META value."""
    return _strategy_default_compute(strategy) or (meta.get("target_compute") or "").strip().lower()


# ── Explainable keyword checks ────────────────────────────────────────────────
# Each compute/DB/CI-CD option → the regex that names it in prose. Matches are
# ignored when the clause that precedes them negates or describes the SOURCE
# ("no Aurora", "currently on ECS", "migrating from Lambda"), so the checks
# only fire on statements that actually PROPOSE the named target.
_COMPUTE_PATTERNS = {
    "ecs_fargate": r"\bfargate\b",
    "eks": r"\beks\b|\bkubernetes\b",
    "lambda": r"\blambda\b",
    "ecs": r"\becs\b",
    "ec2": r"\bec2\b",
}
_COMPUTE_LABELS = {"ecs_fargate": "ECS Fargate", "eks": "EKS", "lambda": "Lambda", "ecs": "ECS", "ec2": "EC2"}
# Computes that contradict each confirmed target. "ECS" stays allowed for an
# ecs_fargate target (Fargate IS an ECS launch type); everything else is a
# different compute. eks is no longer selectable but stays detectable.
_COMPUTE_FORBIDDEN = {
    "ec2": ("ecs_fargate", "eks", "lambda", "ecs"),   # VMs: no containers / serverless
    "ecs_fargate": ("eks", "lambda", "ec2"),
    "eks": ("ecs_fargate", "lambda", "ec2"),
    "lambda": ("ecs_fargate", "eks", "ecs", "ec2"),
}
# A compute-section sentence that PROPOSES a compute (vs. merely describing one).
_COMPUTE_PROPOSAL_RE = re.compile(
    r"\b(?:instead|deploy(?:ed|s|ing)?\s+(?:on|to|onto)|run(?:s|ning)?\s+on|host(?:ed|s)?\s+on|"
    r"migrat\w*\s+(?:to|onto)|move[sd]?\s+(?:to|onto)|target(?:s|ed)?|recommend\w*|use[sd]?|using|"
    r"will\s+(?:run|use|be)|go(?:es)?\s+with|choose|chosen|selected)\b",
    re.I,
)
_COMPUTE_SECTION_HEADING_RE = re.compile(
    r"^(#{2,6})\s*(?:[\d.]+\s*)?(?:target\s+)?compute(?:\s+(?:strategy|platform|architecture|design|layer|target))?\b",
    re.I,
)
# CodePipeline is the only cicd_target.
_CICD_PATTERNS = {"codepipeline": r"\bcodepipeline\b"}
_CICD_LABELS = {"codepipeline": "AWS CodePipeline"}
_DB_ENGINE_PATTERNS = {
    "postgresql": r"\bpostgres(?:ql)?\b",
    "mysql": r"\bmysql\b|\bmariadb\b",
    "sqlserver": r"\bsql\s*server\b",
    "oracle": r"\boracle\b",
}
_SEVEN_RS = ("rehost", "relocate", "replatform", "refactor", "repurchase", "retain", "retire")

_NEGATION_BEFORE = re.compile(
    r"\b(?:no|not|non|without|never|instead\s+of|rather\s+than|replac\w*|from|source|"
    r"current(?:ly)?|existing|legacy|already|today|on-prem\w*|deferred|day-2|future|later|"
    r"post-migration|vs\.?|versus|alternatives?|considered|consider|avoid\w*|out\s+of\s+scope|"
    r"eliminat\w*|remov\w*|rejected|ruled\s+out)\b"
    r"[^.;|\n]{0,40}$",
    re.I,
)
_NEGATION_AFTER = re.compile(
    # A table cell answering the row ("| DMS required | No — table already in AWS |", APP-CLI 2026-10-27).
    r"^[^.;\n]{0,30}?\|\s*(?:no|none|n/?a|not\s+(?:required|needed|applicable))\b|"
    r"^[^.;|\n]{0,40}?\b(?:not\s+(?:required|needed|applicable|used|in\s+scope)|n/a|"
    r"out\s+of\s+scope|deferred|is\s+not|(?:is|as|remains)\s+an?\s+alternative|alternative|"
    r"(?:was|were)\s+(?:considered|rejected|ruled\s+out|evaluated)|rejected|ruled\s+out)\b",
    re.I,
)


def _mention(text, pattern):
    """First non-negated match of `pattern` in `text` (the matched snippet), else None."""
    if not text:
        return None
    for m in re.finditer(pattern, text, re.I):
        before = text[max(0, m.start() - 60):m.start()]
        after = text[m.end():m.end() + 60]
        if _NEGATION_BEFORE.search(before) or _NEGATION_AFTER.search(after):
            continue
        lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
        return " ".join(text[lo:hi].split())
    return None


def _to_text(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


# ── Assessment-summary consistency (deterministic; the Harness ignores "MUST") ─
# Phrases that describe a CODE TRANSFORMATION / re-platforming step. Used only
# for strategies that migrate AS-IS, to move such items out of the plan.
_TRANSFORM_ITEM_RE = re.compile(
    r"\bAWS/[\w.-]+"                                   # an ATX TD name
    r"|\bversion[- ]upgrade\b"
    r"|\bupgrad(?:e|es|ing)\b[^.;\n]{0,40}\b(?:java|jdk|spring|node(?:\.js)?|python|\.net|framework|runtime|angular|vue|ruby)\b"
    r"|\b(?:java|jdk|spring\s*boot|node(?:\.js)?|python|\.net)\s*[\d.]+\s*(?:→|->|to)\s*[\d.]+"
    r"|\bjavax\b|\bjakarta\b"
    r"|\bcontaineri[sz]\w*|\bfargate\b|\beks\b|\bkubernetes\b"
    r"|\bgap\s+transformations?\b|\bengineer-authored\b|\bcode\s+transformations?\b",
    re.I,
)
# Clause that starts the upgrade/transform part of a runtime description,
# e.g. "Java 8 (EOL) → upgrading to Java 17 via AWS/java-version-upgrade".
_TRANSFORM_CLAUSE_RE = re.compile(r"\s*(?:→|->|\bupgrad(?:e|ing)\b|\bvia\s+AWS/|\bAWS/).*$", re.I | re.S)


def _defer(deferred, field, item, strategy):
    deferred.append({"field": field, "item": item, "reason": f"out of scope for {strategy}"})


def _neutralize_list(result, field, strategy, deferred):
    value = result.get(field)
    if isinstance(value, list):
        kept = []
        for item in value:
            if _TRANSFORM_ITEM_RE.search(_to_text(item)):
                _defer(deferred, field, item, strategy)
            else:
                kept.append(item)
        result[field] = kept
    elif isinstance(value, str) and _TRANSFORM_ITEM_RE.search(value):
        _defer(deferred, field, value, strategy)
        result[field] = f"Code transformation / re-platforming steps are out of scope for {strategy} (see deferred_modernization)."


def _assessment_contradictions(result, meta):
    """Hard contradictions between an assessment-summary and the CONFIRMED strategy.

    Run BEFORE _enforce_assessment_consistency rewrites the result. A summary that
    reasons about a different 7R strategy (APP-CLI: confirmed refactor, summary
    written end-to-end for rehost/EC2 because get_app_context failed) cannot be
    repaired field-by-field — the validate_phase post-check fails on these and the
    phase re-runs once with them in its prompt. The stated strategy is overwritten.
    """
    strategy = (meta.get("migration_strategy") or "").strip().lower()
    if not strategy:
        return []
    out = []
    stated = result.get("migration_strategy")
    if isinstance(stated, str) and stated.strip() and stated.strip().lower() != strategy:
        out.append(f"migration_strategy: assessment was written for '{stated.strip().lower()}' but the "
                   f"confirmed strategy is '{strategy}'.")
        result["migration_strategy"] = strategy
    rationale = _to_text(result.get("target_compute_rationale") or "")
    for other in _SEVEN_RS:
        if other != strategy and _mention(rationale, rf"\b{other}\b") and not re.search(rf"\b{strategy}\b", rationale, re.I):
            out.append(f"target_compute_rationale: reasons about the '{other}' strategy, never the confirmed '{strategy}'.")
            break
    labels = {str(d.get("label") or "") for d in (result.get("deferred_modernization") or [])
              if isinstance(d, dict)} if isinstance(result.get("deferred_modernization"), list) else set()
    for other in _SEVEN_RS:
        if other != strategy and any(re.search(rf"out of scope for {other}\b", lab, re.I) for lab in labels):
            out.append(f"deferred_modernization: items are labelled out of scope for '{other}' "
                       f"(confirmed strategy is '{strategy}').")
            break
    return out


def _enforce_assessment_consistency(app_id, result, meta):
    """Make a stored assessment-summary consistent with the confirmed strategy +
    selections, and flag what cannot be fixed deterministically.

    - Non-transform strategies (rehost/relocate/retain/close-out): zero
      `transformations`, move transformation/containerisation recommendations
      out of migration_order / recommendations / containerization_readiness /
      qualification_criteria into `deferred_modernization` (labelled out of scope).
    - Compute/DB/CI-CD named in target_compute_rationale or the cost basis that
      differ from the confirmed selection are FLAGGED (numbers we cannot
      recompute are never rewritten) in `consistency_warnings`.
    Returns the list of warnings (also stored on the result).
    """
    strategy = (meta.get("migration_strategy") or "").strip().lower()
    warnings = []
    if not strategy:
        return warnings
    expected = _expected_compute(strategy, meta)

    if strategy not in _TRANSFORM_STRATEGIES:
        result["transformations"] = {"recommended": [], "gaps": []}
        if strategy in _LIFT_STRATEGIES:
            result["target_compute"] = "ec2"
        deferred = []
        for field in ("migration_order", "migration_order_summary", "recommendations",
                      "transformation_steps", "recommended_next_steps"):
            _neutralize_list(result, field, strategy, deferred)
        cr = result.get("containerization_readiness")
        if cr not in (None, "", {}):
            _defer(deferred, "containerization_readiness", cr, strategy)
            result["containerization_readiness"] = {
                "status": "not_applicable",
                "note": f"Containerization is out of scope for {strategy} — the workload migrates as-is"
                        + (" on EC2." if strategy in _LIFT_STRATEGIES else "."),
            }
        for row in result.get("qualification_criteria") or []:
            if not isinstance(row, dict) or row.get("criterion") not in ("runtime_support", "containerization_readiness"):
                continue
            value = row.get("value")
            if isinstance(value, str) and _TRANSFORM_ITEM_RE.search(value):
                _defer(deferred, f"qualification_criteria.{row.get('criterion')}", value, strategy)
                trimmed = _TRANSFORM_CLAUSE_RE.sub("", value).strip(" ,;—-")
                row["value"] = trimmed or "as-is (no runtime change)"
                row["note"] = f"Runtime migrates as-is for {strategy}; upgrade path deferred (see deferred_modernization)."
        if deferred:
            result["deferred_modernization"] = {
                "label": f"Out of scope for {strategy.upper()} — deferred modernization, NOT part of this migration",
                "items": deferred,
            }
            warnings.append(
                f"{len(deferred)} transformation/containerization item(s) moved to deferred_modernization "
                f"(out of scope for {strategy})."
            )
    if strategy in _TRANSFORM_STRATEGIES and expected and \
            (result.get("target_compute") or "").strip().lower() not in ("", expected):
        warnings.append(
            f"Assessment proposed target_compute='{result.get('target_compute')}' but the confirmed "
            f"target_compute is '{expected}'; overwritten with the confirmed value."
        )
        result["target_compute"] = expected

    # Compute named in prose that contradicts the expected compute.
    if expected and expected in _COMPUTE_FORBIDDEN:
        texts = {
            "target_compute_rationale": _to_text(result.get("target_compute_rationale") or ""),
            "cost_projection.target_ballpark_annual_cost": _to_text(
                (result.get("cost_projection") or {}).get("target_ballpark_annual_cost") or ""
            ),
        }
        for where, text in texts.items():
            for other in _COMPUTE_FORBIDDEN[expected]:
                hit = _mention(text, _COMPUTE_PATTERNS[other])
                if hit:
                    warnings.append(
                        f"{where} names {_COMPUTE_LABELS[other]} but the confirmed target_compute is "
                        f"'{expected}' (\"{hit}\") — not recomputed; Design must use '{expected}'."
                    )
                    break
    # Strategy named in the rationale that is not the confirmed one.
    rationale = _to_text(result.get("target_compute_rationale") or "")
    for other in _SEVEN_RS:
        if other != strategy and _mention(rationale, rf"\b{other}\b"):
            warnings.append(f"target_compute_rationale refers to the '{other}' strategy; confirmed strategy is '{strategy}'.")
            break
    # Database named in the cost basis that contradicts target_database.
    target_db = (meta.get("target_database") or "").strip().lower()
    cost_text = _to_text((result.get("cost_projection") or {}).get("target_ballpark_annual_cost") or "")
    if target_db and cost_text:
        if target_db in ("same_as_source", "none") or target_db.startswith("rds_"):
            hit = _mention(cost_text, r"\baurora\b")
            if hit:
                warnings.append(
                    f"cost_projection basis prices Aurora but target_database is '{target_db}' (\"{hit}\") — "
                    "ballpark not recomputed; refine in Design §9."
                )
    # CI/CD named in the assessment vs the confirmed cicd_target.
    meta_cicd = (meta.get("cicd_target") or "").strip().lower()
    res_cicd = (result.get("cicd_target") or "").strip().lower() if isinstance(result.get("cicd_target"), str) else ""
    if meta_cicd and res_cicd and res_cicd != meta_cicd:
        warnings.append(
            f"Assessment cicd_target='{res_cicd}' differs from the confirmed cicd_target='{meta_cicd}'; "
            "overwritten with the confirmed value."
        )
        result["cicd_target"] = meta_cicd

    # AWS Account ID / VPC CIDR are entered by the human at the assessment gate —
    # a value in the summary that is not the recorded one is invented (APP-CLI
    # stated account 123456789879, recalled from an earlier run) and would leak
    # into Design. Drop it.
    for field in ("target_account", "vpc_cidr"):
        stated = str(result.get(field) or "").strip()
        recorded = str(meta.get(field) or "").strip()
        if stated and stated != recorded:
            result.pop(field, None)
            warnings.append(
                f"{field}='{stated}' in the assessment is not the recorded value "
                f"({recorded or 'not set yet'}) — removed; it is entered at the assessment gate."
            )

    if warnings:
        cp = result.get("cost_projection")
        if isinstance(cp, dict) and any(w.startswith("cost_projection") for w in warnings):
            cp["consistency_flag"] = "Ballpark basis does not match the confirmed selections — treat as indicative only."
    return warnings


def _latest_atx_analysis(app_id):
    """Latest real ATX code-analysis record written by the worker (never the LLM).

    SK = ANALYSIS#comprehensive-codebase-analysis#<ISO ts>, so newest-first + Limit=1
    is the latest — no need to read (and 1 MB-truncate) every past run.
    """
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"APP#{app_id}")
        & Key("SK").begins_with("ANALYSIS#comprehensive-codebase-analysis#"),
        ScanIndexForward=False, Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else {}


def _atx_job_for(app_id, run_id):
    """The JOB# record start_code_analysis wrote for this app's ATX run (GSI1, no scan).

    GSI1SK is the job id `analysis-<app>-<random hex>` — not time-ordered — so every
    page is read (paginated) and the newest COMPLETED job is picked by created_at.
    """
    items = query_all(
        table, IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq("TYPE#JOB") & Key("GSI1SK").begins_with(f"analysis-{app_id}-"),
    )
    jobs = [j for j in items if j.get("app_id") == app_id and j.get("mode") == "code-analysis"]
    if run_id:
        exact = [j for j in jobs if j.get("atx_run_id") == run_id]
        if exact:
            return exact[0]
    done = [j for j in jobs if j.get("status") == "COMPLETED"]
    done.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return done[0] if done else {}


def _stamp_atx_provenance(app_id, result):
    """Overwrite ATX identifiers/timestamps from the persisted job + analysis
    records — the LLM has been seen inventing them (e.g. a completion date two
    weeks before the run). Returns consistency warnings."""
    warnings = []
    atx = _latest_atx_analysis(app_id)
    run_id = atx.get("run_id") or None
    job = _atx_job_for(app_id, run_id) if atx else {}
    actual = {
        "atx_run_id": run_id,
        "atx_job_id": job.get("job_id") or None,
        "analysis_completed_at": atx.get("timestamp") or atx.get("updated_at") or None,
    }
    for key, real in actual.items():
        claimed = result.get(key)
        if claimed not in (None, "") and str(claimed) != str(real or ""):
            warnings.append(f"{key}='{claimed}' did not match the recorded ATX job; replaced with '{real}'.")
        result[key] = real
    if atx.get("s3_prefix"):
        result["atx_s3_prefix"] = atx["s3_prefix"]
    if not atx:
        warnings.append("No ATX code-analysis record found for this app — ATX provenance fields cleared.")
    return warnings


def _looks_like_json(text):
    s = (text or "").strip()
    return (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))


def _md_value(value, depth=0):
    pad = "  " * depth
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}- **{k}**:")
                lines.extend(_md_value(v, depth + 1))
            else:
                lines.append(f"{pad}- **{k}**: {v if v not in (None, '') else '—'}")
        return lines
    if isinstance(value, list):
        lines = []
        for v in value:
            if isinstance(v, dict):
                head = ", ".join(f"{k}={_to_text(x)}" for k, x in v.items() if not isinstance(x, (dict, list)))
                lines.append(f"{pad}- {head or '(item)'}")
                nested = {k: x for k, x in v.items() if isinstance(x, (dict, list)) and x}
                if nested:
                    lines.extend(_md_value(nested, depth + 1))
            else:
                lines.append(f"{pad}- {v}")
        return lines or [f"{pad}- (none)"]
    return [f"{pad}{value}"]


def _render_report_md(app_id, analysis_type, result):
    """Compact deterministic markdown rendering of a structured analysis result."""
    title = analysis_type.replace("-", " ").title()
    lines = [f"# {title} — {result.get('app_name') or app_id}", ""]
    scalars = [(k, v) for k, v in result.items() if not isinstance(v, (dict, list)) and k != "report_md"]
    if scalars:
        lines += [f"- **{k}**: {v if v not in (None, '') else '—'}" for k, v in scalars]
        lines.append("")
    for k, v in result.items():
        if isinstance(v, (dict, list)):
            lines += [f"## {k.replace('_', ' ').title()}", ""] + _md_value(v) + [""]
    return "\n".join(lines).strip() + "\n"


_DB_TARGET_LABELS = {
    "rds_postgresql": "Amazon RDS for PostgreSQL", "rds_mysql": "Amazon RDS for MySQL",
    "rds_sqlserver": "Amazon RDS for SQL Server", "aurora_postgresql": "Amazon Aurora PostgreSQL",
    "aurora_mysql": "Amazon Aurora MySQL", "dynamodb": "Amazon DynamoDB", "documentdb": "Amazon DocumentDB",
}


def _db_family(engine) -> str:
    """dynamodb | postgresql | mysql | '' — engine family of a source engine or a target_database."""
    e = str(engine or "").strip().lower()
    if "dynamo" in e:
        return "dynamodb"
    if "postgres" in e:
        return "postgresql"
    if "mysql" in e or "maria" in e:
        return "mysql"
    return ""


def _stamp_execution_database(result, meta):
    """Stamp the confirmed target_database onto an execution-database report and
    flag where the agent's report disagrees with META / discovery data (it has
    been seen picking Aurora for rds_postgresql and inventing an 'Amazon RDS'
    source for an on-prem database). Flags, never rewrites the plan."""
    warnings = []
    td = (meta.get("target_database") or "").strip().lower()
    result["confirmed_target_database"] = td or None
    result["confirmed_target_service"] = _DB_TARGET_LABELS.get(td)
    report_target = _to_text(result.get("target_database") or result.get("target_service") or "")
    if td.startswith(("rds_", "aurora_")) and report_target:
        family, engine = td.split("_", 1)
        if family == "rds" and _mention(report_target, r"\baurora\b"):
            warnings.append(f"Report targets Aurora but the confirmed target_database is '{td}' ({_DB_TARGET_LABELS[td]}).")
        if family == "aurora" and not re.search(r"\baurora\b", report_target, re.I):
            warnings.append(f"Report target does not name Aurora but the confirmed target_database is '{td}'.")
        pat = _DB_ENGINE_PATTERNS.get(engine)
        if pat and not re.search(pat, report_target, re.I):
            warnings.append(f"Report target engine does not match the confirmed target_database '{td}'.")
    elif td in ("dynamodb", "documentdb") and report_target and not re.search(rf"\b{td}\b", report_target, re.I):
        warnings.append(f"Report target does not name {td} but the confirmed target_database is '{td}'.")

    src = meta.get("database") if isinstance(meta.get("database"), dict) else {}
    reported_src = result.get("source_database")
    if src:
        result["source_database_discovered"] = src
    if isinstance(reported_src, dict):
        for key in ("engine", "version", "size_gb"):
            if src.get(key) not in (None, "") and reported_src.get(key) not in (None, "") \
                    and str(src[key]).strip().lower() != str(reported_src[key]).strip().lower():
                warnings.append(
                    f"source_database.{key}='{reported_src[key]}' differs from discovery data ('{src[key]}')."
                )
        claimed_service = _to_text(reported_src.get("service") or reported_src.get("hosting") or "")
        known_hosting = " ".join(str(src.get(k) or "") for k in ("service", "hosting", "platform", "location"))
        if re.search(r"\b(?:rds|aurora|amazon|aws)\b", claimed_service, re.I) and not re.search(
            r"\b(?:rds|aurora|amazon|aws)\b", known_hosting, re.I
        ):
            warnings.append(
                f"source_database.service='{claimed_service}' is not supported by discovery data "
                "(no AWS hosting recorded for the source database) — verify the source."
            )
    # A managed target REPLACES the source datastore — "not_required" contradicts it
    # (APP-CLI: "source and target are both DynamoDB" while the report's own
    # data-access section rewrites MyBatis/MySQL repositories for DynamoDB).
    status = str(result.get("status") or "").strip().lower()
    hint = " (the report itself describes a MySQL/relational source)" if re.search(
        r"\bmysql\b|\bpostgres\w*\b|\bmybatis\b|\bjdbc\b|\boracle\b|\bsql\s*server\b",
        json.dumps(result, default=str), re.I) else ""
    # Same engine family on both sides (discovered DynamoDB → dynamodb, PostgreSQL →
    # rds/aurora_postgresql): nothing is replaced, so "not_required" is right
    # (APP-MUSIC / APP-MOVIES 2026-10-21) — unless the report itself describes a
    # relational source the discovery data doesn't (the APP-CLI case above).
    family = _db_family(src.get("engine"))
    report_text = json.dumps(result, default=str)
    other_engines = {"postgresql": r"\bmysql\b|\bmybatis\b|\boracle\b|\bsql\s*server\b",
                     "mysql": r"\bpostgres\w*\b|\boracle\b|\bsql\s*server\b",
                     "dynamodb": r"\bmysql\b|\bpostgres\w*\b|\bmybatis\b|\bjdbc\b|\boracle\b|\bsql\s*server\b"}
    same_family = bool(family) and family == _db_family(td) and not re.search(other_engines[family], report_text, re.I)
    if status == "not_required" and td and td not in ("none", "same_as_source") and not same_family:
        warnings.append(f"Report says no database migration is required, but target_database='{td}' replaces the "
                        f"source datastore{hint} — the data migration must be planned.")
    # History the record cannot have: dates before this report was assessed.
    assessed = str(result.get("assessed_at") or "")[:10]
    for key in sorted(result):
        val = result[key]
        if (key.endswith("_done_at") or key.startswith("previously_")) and isinstance(val, str) \
                and assessed and val[:10] < assessed:
            warnings.append(f"{key}='{val}' predates this report ({assessed}) — not from this run; verify or remove.")
    result["consistency_warnings"] = warnings
    return warnings


# ── Design-document consistency (checked at store time; content is in hand) ──
# The 12 sections design.md mandates (single list — the ASL Design prompt
# mirrors it). Matched against level-2 headings, tolerant of numbering/wording.
_DESIGN_SECTIONS = (
    ("Executive Summary", r"executive\s+summary"),
    ("Current State", r"current\s+state"),
    ("Target State Architecture", r"target\s+(?:state\s+)?architecture"),
    ("Migration Approach", r"migration\s+(?:approach|plan)"),
    ("Testing & Validation", r"testing|validation"),
    ("Operational Readiness", r"operational\s+readiness|operations"),
    ("Timeline & Milestones", r"timeline|milestones"),
    ("Risks & Mitigations", r"risks?"),
    ("Cost Analysis", r"cost"),
    ("Code Transformations", r"transformations?"),
    ("CI/CD Pipeline", r"ci\s*/\s*cd|pipeline"),
    ("Deployment Readiness Checklist", r"checklist|deployment\s+readiness"),
)
_DB_SAME_FORBIDDEN = (("RDS", r"\brds\b|\brelational\s+database\s+service\b"), ("Aurora", r"\baurora\b"),
                      ("DocumentDB", r"\bdocumentdb\b"))
_DMS_RE = r"\bdms\b|\bdatabase\s+migration\s+service\b"
_SUPERSEDED_RE = re.compile(r"\b(?:stale|superseded|not\s+used|not\s+applicable|n/a)\b", re.I)
# A cost-reconciliation line describes the ASSESSMENT's preliminary estimate, not
# what the design provisions. APP-TE design (2026-10-13): "the assessment-phase
# preliminary figure … included an Aurora PostgreSQL component … This design uses
# DynamoDB in place of that" failed the DynamoDB check twice.
# A line that offers an option / fallback rather than the plan. APP-TE design
# (2026-10-13, 2nd run) failed on a risks-table row: "…revert to a managed
# relational database path (lower risk, DMS-based migration) | Ravi Menon |".
_ALTERNATIVE_RE = re.compile(
    r"\b(?:alternatives?|fallback|fall\s+back|contingency|option(?:al|s)?|revert\w*|roll\s*back|"
    r"lower[- ]risk|if\s+[^|.]{0,60}\b(?:fails?|slips?|blocked|not\s+feasible))\b",
    re.I,
)
_ESTIMATE_RECON_RE = re.compile(
    r"\breconcil\w*|\b(?:assessment[- ]phase|preliminary|ballpark)\b[^\n]{0,80}?\b(?:estimate|figure|cost)\b",
    re.I,
)
_ACCOUNT_RE = re.compile(r"\b\d{12}\b")
_CIDR_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}\b")
_STATUS_RE = re.compile(r"(\*\*Status:?\*\*:?|(?<![\w*])Status:)(\s*)([^|\n]+)", re.I)


def _strip_md(s):
    return re.sub(r"[*_`]", "", s or "").strip()


def _table_rows(doc):
    """Markdown table rows as (lower-cased key, last cell). The last cell is the
    target value in both `| Param | Value |` and `| Param | Current | Target |` tables."""
    rows = []
    for line in doc.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.count("|") >= 3):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: "):
            continue
        rows.append((_strip_md(cells[0]).lower(), _strip_md(cells[-1])))
    return rows


def _target_part(stmt):
    """For 'X → Y' statements keep only the target side."""
    return re.split(r"→|->|⇒", stmt)[-1]


def _statements(doc, row_key_re, label_re, target_only=True):
    """Target statements for one dimension: matching parameter-table rows plus
    explicit 'Label: value' lines and 'Compute Strategy — X' style headings."""
    part = _target_part if target_only else (lambda x: x)
    out = [part(v) for k, v in _table_rows(doc) if re.fullmatch(row_key_re, k)]
    for line in doc.splitlines():
        s = _strip_md(line).lstrip("#").strip()
        m = re.match(rf"(?:[\d.]+\s*)?(?:{label_re})\s*(?::|—|–| - )\s*(.+)$", s, re.I)
        if m:
            out.append(part(m.group(1)))
    return [s for s in out if s.strip()]


def _compute_section(doc):
    """Body of the design's compute section(s) (e.g. `### 3.3 Compute Strategy`),
    up to the next heading of the same or a higher level. '' when absent."""
    out, level = [], 0
    for line in (doc or "").splitlines():
        s = _strip_md(line)
        h = re.match(r"^(#{1,6})\s", s)
        if level and h and len(h.group(1)) <= level:
            level = 0
        m = _COMPUTE_SECTION_HEADING_RE.match(s)
        if m:
            level = len(m.group(1))
            continue
        if level:
            out.append(line)
    return "\n".join(out)


def _sentences(text):
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", text or "") if x.strip()]


def _normalize_design_status(content):
    """A design is never approved before the design_review gate. Rewrite an
    'Approved' status stamp in the header to Pending Review. Returns
    (content, original_status_or_None)."""
    head, sep, rest = content.partition("\n## ")
    original = None

    def _sub(m):
        nonlocal original
        value = m.group(3).strip()
        if re.search(r"\bapproved\b", value, re.I) and not re.search(r"pending|awaiting|not\s+approved|unapproved", value, re.I):
            original = value
            return f"{m.group(1)}{m.group(2)}Pending Review — awaiting design_review approval "
        return m.group(0)

    head = _STATUS_RE.sub(_sub, head)
    return head + sep + rest, original


# Close-out documents (retire / retain / repurchase — ResolveStrategy's close_out
# branch) are not AWS designs: they are checked against the app's RECORDED FACTS.
# The audit (APP-HE / APP-TE) found close-outs written blind — 34 servers for a
# 6-machine app, "no owners / RPO / dependencies on record" when all were set, a
# 23/100 risk score for a 57/100 app, re-evaluation dates already in the past and a
# plan that ignored the hard deadline.
_CLOSE_OUT_BRANCH = {"retire", "retain", "repurchase"}
_CLAIMS_MISSING_RE = re.compile(
    r"\b(?:not\s+(?:on\s+(?:file|record)|captured|confirmed|declared|defined|provided|recorded|known|set)|"
    r"unknown|missing|incomplete|undefined|undeclared|absent|if\s+applicable)\b|\bno\s+(?:\w+\s+){0,2}on\s+record\b",
    re.I,
)
_DATE_RE = re.compile(r"\b(20\d{2}-[01]\d-[0-3]\d)\b")
_SERVER_COUNT_RE = re.compile(
    r"\b(\d{1,4})[-\s]+(?:(?:legacy|on-?prem(?:ises)?|vmware|vsphere|source|physical|virtual)\s+){0,3}"
    r"(?:servers?|machines?|vms?|hosts?)\b",
    re.I)
_FLEET_RE = re.compile(r"\b(?:total|cluster|inventory|fleet|estate|infrastructure)\b", re.I)
_SCHEDULE_ROW_RE = re.compile(r"^\s*(?:\||20\d{2}-[01]\d-[0-3]\d\b)")
_REFERENCE_DATE_RE = re.compile(r"\b(?:lease|eosl|end[- ]of[- ](?:support|life)|deadline)\b", re.I)
_UPCOMING_RE = re.compile(r"⬜|\b(?:scheduled|pending|planned|trigger|next\s+\w+\s+review|re-?evaluat\w*)\b", re.I)
_DONE_RE = re.compile(r"✅|\b(?:complete[d]?|done)\b", re.I)
_CUTOVER_RE = re.compile(r"\b(?:cutover|decommission\w*|go[- ]live|switch[- ]?over)\b", re.I)


def _closeout_facts(meta, owners):
    """(label, line-regex, recorded value) for each fact the app record HAS."""
    deps = meta.get("dependencies") if isinstance(meta.get("dependencies"), dict) else {}
    up = meta.get("dependencies_upstream") or deps.get("upstream") or []
    down = meta.get("dependencies_downstream") or deps.get("downstream") or []
    dr = meta.get("dr_availability") if isinstance(meta.get("dr_availability"), dict) else {}
    cost = meta.get("current_cost") if isinstance(meta.get("current_cost"), dict) else {}
    db = meta.get("database") if isinstance(meta.get("database"), dict) else {}
    cons = meta.get("constraints") if isinstance(meta.get("constraints"), dict) else {}
    compliance = meta.get("compliance") or []
    if isinstance(compliance, str):
        compliance = [compliance]
    facts = [
        ("owners", r"\bowners?\b", ", ".join(f"{o.get('name') or o.get('email')} ({o.get('role', '')})"
                                             for o in owners if isinstance(o, dict))),
        ("RPO/RTO", r"\brpo\b|\brto\b", " / ".join(str(v) for v in (meta.get("rpo") or dr.get("rpo"),
                                                                   meta.get("rto") or dr.get("rto")) if v)),
        ("dependencies", r"\bdependenc(?:y|ies)\b|\bupstream\b|\bdownstream\b",
         ", ".join([f"upstream {', '.join(map(str, up))}"] * bool(up) + [f"downstream {', '.join(map(str, down))}"] * bool(down))),
        ("current cost", r"\bcost\b|\btco\b",
         f"monthly_infra={cost.get('monthly_infra')}" if cost.get("monthly_infra") not in (None, "", "0", 0) else ""),
        ("database", r"\b(?:database|db)\b", " ".join(str(db.get(k)) for k in ("engine", "version") if db.get(k))),
        ("EOSL", r"\beosl\b|\bend[- ]of[- ](?:support|life)\b", str(cons.get("eosl_date") or "")),
        ("integrations", r"\bintegrations?\b", ", ".join(map(str, meta.get("integrations") or []))),
        ("compliance", r"\bcompliance\b|" + "|".join(rf"\b{re.escape(str(c))}\b" for c in compliance) if compliance
         else r"(?!x)x", ", ".join(map(str, compliance))),
    ]
    return [f for f in facts if f[2]]


def _check_closeout_document(content, meta, owners=None, today=None):
    """Deterministic, explainable check of a close-out document against the app's
    recorded facts. Same return shape as _check_design_document."""
    strategy = (meta.get("migration_strategy") or "").strip().lower()
    doc = content or ""
    lines = doc.splitlines()
    today = today or time.strftime("%Y-%m-%d", time.gmtime())
    owners = owners or []
    contradictions, warnings = [], []

    # Strategy stated / claimed history
    for stmt in _statements(doc, r"(?:migration\s+)?strategy", r"(?:confirmed\s+)?(?:migration\s+)?strategy",
                            target_only=False):
        named = next((r for r in _SEVEN_RS if re.search(rf"\b{r}\b", stmt, re.I)), None)
        if named and named != strategy and not re.search(rf"\b{strategy}\b", stmt, re.I):
            contradictions.append(f"migration_strategy: document states strategy '{named}' but the approved strategy is '{strategy}'.")
            break
    m = re.search(r"human[- ]confirmed[^.\n]{0,40}?\b(" + "|".join(_SEVEN_RS) + r")\b", doc, re.I)
    if m and m.group(1).lower() != strategy:
        contradictions.append(f"strategy history: document claims '{m.group(1).lower()}' was human-confirmed; the only "
                              f"confirmed strategy on record is '{strategy}'.")
    if strategy and not re.search(rf"\b{strategy}\b", doc, re.I):
        contradictions.append(f"migration_strategy: document never names the approved strategy '{strategy}'.")

    # Machine count
    try:
        mc = int(meta.get("machine_count") or len(meta.get("machine_ids") or []))
    except (TypeError, ValueError):
        mc = 0
    counts = {int(n) for n in _SERVER_COUNT_RE.findall(doc)}
    # Wrong when the right count never appears, or a different count is given as
    # the total / cluster / inventory ("6 servers …, 34-server cluster total").
    fleet = {int(n) for ln in lines if _FLEET_RE.search(ln) for n in _SERVER_COUNT_RE.findall(ln)} - {mc}
    if mc and counts and (mc not in counts or fleet):
        wrong = sorted(fleet or counts)
        contradictions.append(f"machine_count: document names {', '.join(map(str, wrong))} server(s) but the app "
                              f"has {mc} machine(s) ({', '.join(map(str, (meta.get('machine_ids') or [])[:10]))}).")

    # Risk score
    rs = str(meta.get("risk_score") or "").strip()
    if rs:
        scores = {n for line in lines if re.search(r"\brisk\b", line, re.I) for n in re.findall(r"\b(\d{1,3})\s*/\s*100\b", line)}
        if scores and rs not in scores:
            contradictions.append(f"risk_score: document states {', '.join(sorted(scores))}/100 but the recorded risk score is "
                                  f"{rs}/100 ({meta.get('risk_tier', '')}).")

    # Facts the record HAS that the document claims are missing / unknown
    # The missing-claim must be about the fact itself — same clause / table cell,
    # within a few words ("Missing critical fields: RPO, RTO, owners", "Monthly
    # infrastructure cost: not on file") — not a risk row that merely mentions it
    # ("…dependency … if decoupling plan not confirmed by 2026-10-30").
    clauses = [c for ln in lines for c in re.split(r"[|.;]|\s—\s", ln)]
    for label, pat, value in _closeout_facts(meta, owners):
        near = re.compile(rf"(?:{pat})[^,\n]{{0,25}}?(?:{_CLAIMS_MISSING_RE.pattern})|"
                          rf"\bmissing\b[^\n]{{0,40}}?(?:{pat})", re.I)
        hit = next((c.strip() for c in clauses if near.search(c)), None)
        if hit:
            contradictions.append(f"{label}: document says it is missing/unknown (\"{hit[:120]}\") but the app record has: {value}.")

    # Architecture / deployment method
    arch = (meta.get("architecture") or "").strip().lower()
    for mine, other in (("microservices", "monolith"), ("monolith", "microservices")):
        if arch == mine and re.search(rf"\b{other}\b", doc, re.I) and not re.search(rf"\b{mine}\b", doc, re.I):
            contradictions.append(f"architecture: document describes a {other} but the app architecture is '{arch}'.")
    deploy = str(meta.get("deployment_method") or "").strip()
    if deploy and "manual" not in deploy.lower() and re.search(r"\bmanual\s+deploy\w*|deployment[^|\n]{0,20}\bmanual\b", doc, re.I):
        contradictions.append(f"deployment_method: document says deployment is manual but the app uses '{deploy}'.")

    # Hard deadline + schedule sanity
    cons = meta.get("constraints") if isinstance(meta.get("constraints"), dict) else {}
    hd = str(meta.get("hard_deadline") or cons.get("hard_deadline") or "").strip()
    if hd:
        if hd not in doc:
            contradictions.append(f"hard_deadline: the app's hard deadline {hd} is not addressed in the document.")
        if re.search(r"\bno\s+(?:active\s+|hard\s+)?(?:migration\s+)?deadline\b", doc, re.I):
            contradictions.append(f"hard_deadline: document claims there is no deadline but the app's hard deadline is {hd}.")
        # Schedule rows only (tables / timeline lines), not prose or rows that
        # merely cite a reference date (DC lease end, EOSL).
        late = next((ln.strip() for ln in lines if _SCHEDULE_ROW_RE.match(ln) and _CUTOVER_RE.search(ln)
                     and not _DONE_RE.search(ln) and not _REFERENCE_DATE_RE.search(ln)
                     and any(d > hd for d in _DATE_RE.findall(ln))), None)
        if late:
            contradictions.append(f"hard_deadline: cutover/decommission is scheduled after the hard deadline {hd} (\"{late[:120]}\").")
    past = next((ln.strip() for ln in lines if _UPCOMING_RE.search(ln) and not _DONE_RE.search(ln)
                 and any(d < today for d in _DATE_RE.findall(ln))), None)
    if past:
        contradictions.append(f"schedule: an upcoming milestone/trigger is dated before today {today} (\"{past[:120]}\").")

    # Softer findings
    for w in meta.get("change_freeze_windows") or []:
        start = _DATE_RE.search(str(w))
        if start and start.group(1) not in doc:
            warnings.append(f"change freeze window {w} is not reflected in the document.")
    for label, value in (("RPO", meta.get("rpo")), ("RTO", meta.get("rto"))):
        if value and str(value) not in doc:
            warnings.append(f"{label} is recorded as {value} but the value does not appear in the document.")
    headings = [_strip_md(h).lower() for h in re.findall(r"(?m)^##(?!#)\s*(.+)$", doc)]
    missing = [name for name, pat in (("Decision Rationale", r"rationale|decision"),
                                      ("Close-out Plan", r"plan|decommission|replacement|retain"))
               if not any(re.search(pat, h, re.I) for h in headings)]
    if missing:
        warnings.append("Missing close-out section(s): " + ", ".join(missing) + ".")

    return {
        "contradictions": contradictions,
        "warnings": warnings,
        "missing_sections": missing,
        "checked_against": {"migration_strategy": strategy or None, "close_out": True},
    }


def _check_design_document(content, meta):
    """Deterministic, explainable check of a design doc against the APPROVED
    selections on META. Returns {contradictions, warnings, missing_sections,
    checked_against}. Contradictions are hard (the validate_phase post-check
    fails on them); warnings are surfaced at the design_review gate."""
    strategy = (meta.get("migration_strategy") or "").strip().lower()
    tc = _expected_compute(strategy, meta)
    td = (meta.get("target_database") or "").strip().lower()
    cicd = (meta.get("cicd_target") or "").strip().lower()
    acct = str(meta.get("target_account") or "").strip()
    cidr = (meta.get("vpc_cidr") or "").strip()
    doc = content or ""
    contradictions, warnings = [], []

    # Compute
    compute_stmts = _statements(doc, r"(?:target\s+)?compute(?:\s+(?:platform|target|service|strategy))?",
                                r"target\s+compute|compute\s+strategy|compute\s+platform")
    if tc in _COMPUTE_FORBIDDEN:
        section = _compute_section(doc)
        required = _COMPUTE_PATTERNS[tc]
        # Compute-section prose that PROPOSES another compute ("…deploy on ECS
        # Fargate instead") — a sentence that also names the approved compute is
        # describing a move onto it ("source Lambdas move to EC2"), not a proposal.
        prose = [x for x in _sentences(section)
                 if _COMPUTE_PROPOSAL_RE.search(x) and not _mention(x, required)]
        flagged = set()
        for stmt in compute_stmts + prose:
            for other in _COMPUTE_FORBIDDEN[tc]:
                if other in flagged:
                    continue
                hit = _mention(stmt, _COMPUTE_PATTERNS[other])
                if hit:
                    flagged.add(other)
                    contradictions.append(
                        f"target_compute: design proposes {_COMPUTE_LABELS[other]} (\"{hit}\") but the approved "
                        f"target_compute is '{tc}'."
                    )
                    break
        # The approved compute must be named where the design states its compute —
        # a passing mention elsewhere ("Fargate was considered") doesn't count.
        # Without any compute statement/section, fall back to the whole document.
        scope = "\n".join(compute_stmts) + "\n" + section if (compute_stmts or section.strip()) else doc
        if not _mention(scope, required):
            contradictions.append(f"target_compute: design never names {_COMPUTE_LABELS[tc]} (approved target_compute='{tc}').")
        if not compute_stmts:
            warnings.append("target_compute: no explicit target-compute statement (parameters table / §3.3) found.")

    # Database
    db_stmts = _statements(
        doc,
        r"(?:target\s+)?(?:database|db)(?:\s+(?:platform|engine|target|service))?|target\s+engine|engine|primary\s+database",
        r"target\s+database|primary\s+database|database\s+strategy",
    )
    if td == "same_as_source":
        src = meta.get("database") if isinstance(meta.get("database"), dict) else {}
        src_text = " ".join(str(v) for v in (src or {}).values()).lower()
        moved = next(
            ((label, hit) for stmt in db_stmts for label, pat in _DB_SAME_FORBIDDEN
             if (hit := _mention(stmt, pat)) and not re.search(pat, src_text, re.I)),
            None,
        )
        if moved:
            contradictions.append(
                f"target_database: design moves the database to {moved[0]} (\"{moved[1]}\") but the approved "
                "target_database is 'same_as_source' (database stays as-is)."
            )
        for line in doc.splitlines():
            hit = _mention(line, _DMS_RE)
            if hit:
                contradictions.append(
                    f"target_database: design plans a DMS migration (\"{hit}\") but target_database is 'same_as_source'."
                )
                break
    elif td.startswith(("rds_", "aurora_")):
        family, engine = td.split("_", 1)
        for stmt in db_stmts:
            if family == "rds":
                hit = _mention(stmt, r"\baurora\b")
                if hit:
                    contradictions.append(
                        f"target_database: design targets Aurora (\"{hit}\") but the approved target_database is '{td}'."
                    )
                    break
            others = [e for e in _DB_ENGINE_PATTERNS if e != engine]
            if engine in _DB_ENGINE_PATTERNS and not re.search(_DB_ENGINE_PATTERNS[engine], stmt, re.I):
                hit = next((h for e in others if (h := _mention(stmt, _DB_ENGINE_PATTERNS[e]))), None)
                if hit:
                    contradictions.append(f"target_database: design names engine \"{hit}\" but target_database is '{td}'.")
                    break
        if family == "aurora" and db_stmts and not any(re.search(r"\baurora\b", s, re.I) for s in db_stmts):
            contradictions.append(f"target_database: no target-database statement names Aurora (approved '{td}').")
    elif td in ("dynamodb", "documentdb"):
        if db_stmts and not any(re.search(rf"\b{td}\b", s, re.I) for s in db_stmts):
            contradictions.append(f"target_database: no target-database statement names {td} (approved '{td}').")
        # Naming the target is not enough: APP-CLI's design kept the monolith's data
        # on Aurora MySQL via DMS while approved target_database='dynamodb'. Any
        # relational target / DMS plan outside the Current State description
        # contradicts it (negated / "stale" / "superseded" mentions are ignored).
        section = ""
        found = {}
        for line in doc.splitlines():
            if re.match(r"^##(?!#)", line):
                section = _strip_md(line.lstrip("#")).lower()
                continue
            if (re.search(r"current\s+state|risks?", section) or _SUPERSEDED_RE.search(line)
                    or _ESTIMATE_RECON_RE.search(line) or _ALTERNATIVE_RE.search(line)):
                continue
            for label, pat in (("Aurora/RDS", r"\baurora\b|\brds\b|\brelational\s+database\s+service\b"),
                               ("DMS", _DMS_RE)):
                if label not in found and (hit := _mention(line, pat)):
                    found[label] = hit
        if "Aurora/RDS" in found:
            contradictions.append(
                f"target_database: design provisions Aurora/RDS (\"{found['Aurora/RDS']}\") but the approved "
                f"target_database is '{td}' — model the data in {td} instead."
            )
        if "DMS" in found:
            contradictions.append(
                f"target_database: design plans a DMS migration (\"{found['DMS']}\") into a relational target; the "
                f"approved target_database is '{td}' (migrate the data into {td})."
            )
    if td and not db_stmts:
        warnings.append("target_database: no explicit target-database statement (parameters table / §3.4) found.")

    # CI/CD
    if cicd in _CICD_PATTERNS:
        cicd_stmts = _statements(
            doc,
            r"(?:ci\s*/\s*cd|cicd)(?:\s+(?:target|provider|pipeline|platform|tool))?|pipeline(?:\s+provider)?",
            r"ci\s*/\s*cd\s+(?:target|provider|pipeline)|pipeline\s+provider",
        )
        for stmt in cicd_stmts:
            if re.search(_CICD_PATTERNS[cicd], stmt, re.I):
                continue
            hit = next((h for o, p in _CICD_PATTERNS.items() if o != cicd and (h := _mention(stmt, p))), None)
            if hit:
                contradictions.append(
                    f"cicd_target: design uses {hit!r} as the pipeline but the approved cicd_target is "
                    f"'{cicd}' ({_CICD_LABELS[cicd]})."
                )
                break
        if not re.search(_CICD_PATTERNS[cicd], doc, re.I):
            contradictions.append(f"cicd_target: design never names {_CICD_LABELS[cicd]} (approved cicd_target='{cicd}').")

    # AWS account + VPC CIDR
    rows = _table_rows(doc)
    if acct:
        for k, v in rows:
            if re.fullmatch(r"(?:target\s+)?(?:aws\s+)?account(?:\s+id)?", k):
                found = _ACCOUNT_RE.findall(v)
                if found and acct not in found:
                    contradictions.append(f"target_account: design lists AWS Account ID {found[0]} but the approved account is {acct}.")
                    break
        if acct not in doc:
            warnings.append(f"target_account: approved AWS Account ID {acct} does not appear in the design.")
    if cidr:
        for k, v in rows:
            if re.fullmatch(r"vpc(?:\s+cidr)?(?:\s+(?:range|block))?", k):
                found = _CIDR_RE.findall(v)
                if found and cidr not in found:
                    contradictions.append(f"vpc_cidr: design lists VPC CIDR {found[0]} but the approved vpc_cidr is {cidr}.")
                    break
        if cidr not in doc:
            warnings.append(f"vpc_cidr: approved VPC CIDR {cidr} does not appear in the design.")

    # Strategy
    if strategy:
        for stmt in _statements(doc, r"(?:migration\s+)?strategy", r"(?:confirmed\s+)?(?:migration\s+)?strategy",
                                target_only=False):
            named = next((r for r in _SEVEN_RS if re.search(rf"\b{r}\b", stmt, re.I)), None)
            if named and named != strategy and not re.search(rf"\b{strategy}\b", stmt, re.I):
                contradictions.append(f"migration_strategy: design states strategy '{named}' but the approved strategy is '{strategy}'.")
                break
        if not re.search(rf"\b{strategy}\b", doc, re.I):
            warnings.append(f"migration_strategy: design never names the approved strategy '{strategy}'.")

    # Approved selections dismissed as "stale" / "superseded" (APP-CLI design,
    # 2026-10-12: "the META field `dynamodb` is a stale template default"), or the
    # database stated as same_as_source when another target was approved.
    approved = {k: v for k, v in (("target_compute", tc), ("target_database", td), ("cicd_target", cicd)) if v}
    for line in doc.splitlines():
        # The approved value must be the SUBJECT ("`dynamodb` is a stale default"),
        # not merely on a line that calls another value superseded.
        hit = next((f for f, v in approved.items() if re.search(
            rf"{re.escape(v)}`?[^.|\n]{{0,40}}?\b(?:is|was)\s+(?:an?\s+|the\s+)?(?:stale|superseded|outdated|"
            rf"not\s+reflective|incorrect|wrong)\b", line, re.I)), None)
        if hit:
            contradictions.append(f"{hit}: design dismisses the approved {hit} '{approved[hit]}' as stale/superseded "
                                  f"(\"{_strip_md(line)[:120]}\") — the approved value is authoritative.")
            break
    if td and td != "same_as_source" and any(re.search(r"\bsame_as_source\b", st, re.I) for st in db_stmts):
        contradictions.append(f"target_database: design states 'same_as_source' but the approved target_database is '{td}'.")

    # Approved code transformations must be planned (design §10).
    steps = meta.get("transformation_steps") or []
    if isinstance(steps, str):
        steps = [x.strip() for x in steps.split(",")]
    missing_tds = [str(t) for t in steps if str(t).strip() and str(t) not in doc]
    if missing_tds:
        contradictions.append("transformation_steps: design never plans the approved transformation(s) "
                              + ", ".join(missing_tds) + ".")
    # …and only those. APP-CLI 2026-10-19: approved with NO transformations, yet §10
    # listed the assessment's three recommendations as "Confirmed (from assessment
    # approval)". Checked only when the gate recorded a list (an empty one included);
    # a TD the design itself marks as not approved / deferred / skipped is fine.
    if "transformation_steps" in meta:
        sec = re.search(r"(?ims)^##(?!#)\s*(?:\d+\.\s*)?code\s+transformations?\b.*?(?=^##(?!#)|\Z)", doc)
        approved_tds = {str(t).strip() for t in steps}
        unapproved = []
        for line in (sec.group(0) if sec else "").splitlines():
            if re.search(r"not\s+(?:approved|selected)|deferred|skipped|excluded|declined|out\s+of\s+scope", line, re.I):
                continue
            for name in re.findall(r"\bAWS/[A-Za-z0-9][\w.-]*", line):
                if name not in approved_tds and name not in unapproved:
                    unapproved.append(name)
        if unapproved:
            contradictions.append("transformation_steps: design plans transformation(s) that were not approved at the "
                                  "assessment gate: " + ", ".join(unapproved)
                                  + f" (approved: {', '.join(sorted(approved_tds)) or 'none'}).")

    # Network exposure: AMF never deploys internet-open ingress (exposure policy + module
    # validation). APP-TE 2026-10-23: the design put `0.0.0.0/0:443, 0.0.0.0/0:80` on the ALB
    # SG and passed the gate. Egress / route-table rows legitimately name 0.0.0.0/0.
    for line in doc.splitlines():
        if not re.search(r"0\.0\.0\.0/0|::/0", line) or not re.search(
                r"ingress|inbound|\bsg\b|security\s+group|alb|listener|:(?:80|443)\b|\bport\b", line, re.I):
            continue
        # Statements that nothing is open ("No rule opens 0.0.0.0/0 …", APP-CLI 2026-10-27) are fine.
        if re.search(r"egress|outbound|route|\bnat\b|never|\bnot\b|n't\b|must\s+not|forbid|reject|blocked|"
                     r"no\s+0\.0|\bno\s+(?:rule|ingress|inbound|security\s+group|sg|listener|port)s?\b|\bnothing\b|\bnone\b",
                     line, re.I):
            continue
        contradictions.append("network exposure: design opens ingress to the internet (\""
                              + " ".join(line.split())[:140] + "\") — AMF never deploys 0.0.0.0/0 or ::/0 ingress; "
                              "a public ALB accepts only allowed_ingress_cidrs + the VPC CIDR.")
        break
    # TLS: ACM can't issue a certificate for an AWS-generated ALB DNS name.
    acm = re.search(r"(?i)acm[^.\n|]{0,80}(?:alb|load\s+balancer)\s+dns|(?:alb|load\s+balancer)\s+dns[^.\n|]{0,80}acm", doc)
    if acm and not (meta.get("domain_name") or meta.get("certificate_arn")):
        warnings.append("TLS: design issues an ACM certificate for the ALB DNS name — not possible; AMF builds an HTTP:80 "
                        "listener unless the app has a domain_name / certificate_arn (\"" + " ".join(acm.group(0).split())[:100] + "\").")

    # Required sections (design.md's 12)
    headings = [_strip_md(h).lower() for h in re.findall(r"(?m)^##(?!#)\s*(.+)$", doc)]
    missing = [name for name, pat in _DESIGN_SECTIONS if not any(re.search(pat, h, re.I) for h in headings)]
    if missing:
        warnings.append("Missing design.md section(s): " + ", ".join(missing) + ".")

    # DR inputs captured on META must be reflected.
    for field in ("rpo", "rto"):
        if meta.get(field) and not re.search(rf"\b{field}\b", doc, re.I):
            warnings.append(f"{field.upper()} is set on the app ({meta.get(field)}) but not addressed in the design.")

    return {
        "contradictions": contradictions,
        "warnings": warnings,
        "missing_sections": missing,
        "checked_against": {
            "migration_strategy": strategy or None, "target_compute": tc or None,
            "target_database": td or None, "cicd_target": cicd or None,
            "target_account": acct or None, "vpc_cidr": cidr or None,
        },
    }


@registry.tool(
    "store_design_document",
    description="Persist a versioned design document (metadata in DynamoDB, body in S3).",
    schema={
        "app_id": {"type": "string", "required": True},
        "version": {"type": "string", "description": "Document version (default v1)"},
        "content": {"type": "string", "required": True},
    },
    infer=lambda a: "content" in a and "version" in a,
)
def store_design_document(args):
    app_id, version, content = args["app_id"], (args.get("version") or "v1").strip(), args["content"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    s3_key = f"applications/{app_id}/design/{version}.md"

    # A design is generated BEFORE the design_review gate — it can never be
    # "Approved" yet. Normalize a premature approval stamp in the header.
    content, premature_status = _normalize_design_status(content)

    # Deterministic consistency check against the APPROVED selections. Done here
    # because the content is in hand (the validate_phase post-check reads the
    # persisted result — it has no S3 access).
    try:
        meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    except Exception:  # noqa: BLE001 — never block storing the document
        meta = {}
    if (meta.get("migration_strategy") or "").strip().lower() in _CLOSE_OUT_BRANCH:
        try:
            owners = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("OWNER#"),
            ).get("Items", [])
        except Exception:  # noqa: BLE001
            owners = []
        check = _check_closeout_document(content, meta, owners)
    else:
        check = _check_design_document(content, meta)
    check["checked_at"] = now
    if premature_status:
        check["warnings"].append(
            f"Design header claimed Status '{premature_status}' before the design_review gate; normalized to Pending Review."
        )
        check["status_normalized_from"] = premature_status

    table.put_item(Item={
        "PK": f"APP#{app_id}", "SK": f"DESIGN#{version}",
        "GSI1PK": "TYPE#DESIGN", "GSI1SK": f"{app_id}#{version}",
        "app_id": app_id, "version": version, "s3_key": s3_key,
        "updated_at": now, "content_length": len(content),
        "status": "pending_review", "consistency_check": check,
    })
    # Mirror the version actually stored onto META so the post-check, the gate's
    # decision_summary and the UI all read the same (latest) version.
    try:
        table.update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression=(
                "SET design_version = :v, design_status = :s, design_generated_at = :t, "
                "design_warnings = :w, design_contradictions = :c"
            ),
            ExpressionAttributeValues={
                ":v": version, ":s": "pending_review", ":t": now,
                ":w": check["warnings"], ":c": check["contradictions"],
            },
        )
    except Exception as _err:  # noqa: BLE001 — the DESIGN# record is the source of truth
        logger.debug("best-effort step ignored: %s", _err)
    if BUCKET:
        s3.put_object(Bucket=BUCKET, Key=s3_key, Body=content.encode(), ContentType="text/markdown")
    return json_safe({
        "success": True, "app_id": app_id, "version": version, "s3_key": s3_key,
        "design_status": "pending_review",
        # Returned so the agent can self-correct in the same session; the
        # validate_phase post-check enforces the contradictions regardless.
        "contradictions": check["contradictions"], "warnings": check["warnings"],
    })


@registry.tool(
    "get_design_document",
    description="Return the latest (or a specific) design document, loading the body from S3.",
    schema={"app_id": {"type": "string", "required": True}, "version": {"type": "string"}},
    # Design-get is the fallback: app present, but none of the store/analysis
    # discriminators (content / result / analysis_type) are set.
    infer=lambda a: "app_id" in a and "content" not in a and "result" not in a and "analysis_type" not in a,
)
def get_design_document(args):
    app_id, version = args["app_id"], args.get("version", "")
    if not version:
        # Latest = the version store_design_document last wrote (lexical SK order
        # would rank v10 below v2).
        try:
            meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
            version = (meta.get("design_version") or "").strip()
        except Exception:  # noqa: BLE001
            version = ""
    items = []
    if version:
        item = table.get_item(Key={"PK": f"APP#{app_id}", "SK": f"DESIGN#{version}"}).get("Item")
        items = [item] if item else []
    if not items and not args.get("version"):
        resp = table.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("DESIGN#"),
            ScanIndexForward=False, Limit=1,
        )
        items = resp.get("Items", [])
    if not items:
        return {"app_id": app_id, "document": None, "message": "No design document found"}

    latest = items[0]
    content = ""
    s3_key = latest.get("s3_key", "")
    if BUCKET and s3_key:
        try:
            content = s3.get_object(Bucket=BUCKET, Key=s3_key)["Body"].read().decode()
        except Exception:
            content = ""
    return json_safe({
        "app_id": app_id, "version": latest.get("version"),
        "content": content[:DESIGN_CONTENT_MAX_CHARS] if content else "(stored in S3)",
        "s3_key": s3_key, "updated_at": latest.get("updated_at"),
    })


@registry.tool(
    "store_analysis_result",
    description="Persist an analysis result run (metadata in DynamoDB, JSON in S3).",
    schema={
        "app_id": {"type": "string", "required": True},
        "analysis_type": {"type": "string", "required": True},
        "result": {"type": "object", "description": "Analysis payload"},
    },
    infer=lambda a: "result" in a and "analysis_type" in a,
)
def store_analysis_result(args):
    app_id, analysis_type, result = args["app_id"], args["analysis_type"], args.get("result", {})

    # Deterministic strategy gate for assessment summaries. Code transformation
    # applies ONLY to code-changing strategies (replatform/refactor/rearchitect).
    # For rehost/relocate/close-out the app migrates AS-IS, so the assessment must
    # carry NO transformations or gaps — and rehost/relocate run on EC2. The LLM
    # cannot be relied on to zero these out (it lists modernization findings it
    # sees in the code regardless of the prompt gate), so enforce it here in code.
    consistency_warnings = None
    contradictions = None
    meta_warning_attr = None
    if analysis_type == "assessment-summary" and isinstance(result, dict):
        consistency_warnings = []
        contradictions = []
        meta_warning_attr = "assessment_warnings"
        try:
            meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
            contradictions = _assessment_contradictions(result, meta)
            consistency_warnings += _enforce_assessment_consistency(app_id, result, meta)
        except Exception as _err:  # noqa: BLE001 — never block storing the assessment
            logger.debug("best-effort step ignored: %s", _err)
        result["consistency_contradictions"] = contradictions
        try:
            consistency_warnings += _stamp_atx_provenance(app_id, result)
        except Exception as _err:  # noqa: BLE001
            logger.debug("best-effort step ignored: %s", _err)
        result["consistency_warnings"] = consistency_warnings

    # Deterministic DB-migration gate. The agent has been seen inventing full
    # DMS/Aurora plans for apps whose target_database is `none` or `same_as_source`
    # (or whose strategy is non-code-changing). Replace the result with a
    # `not_required` determination so the UI's structured renderer shows the
    # correct "No database workload" block.
    if analysis_type == "execution-database" and isinstance(result, dict):
        try:
            meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
            strategy = (meta.get("migration_strategy") or "").strip().lower()
            target_db = (meta.get("target_database") or "").strip().lower()
            non_migrating_db = {"", "none", "same_as_source"}
            non_code_strategies = {"rehost", "relocate", "retire", "retain", "repurchase"}
            if target_db in non_migrating_db or strategy in non_code_strategies:
                db_engine = ((meta.get("database") or {}).get("engine") if isinstance(meta.get("database"), dict) else "")
                result = {
                    "status": "not_required",
                    "determination": (
                        f"No database migration required for {app_id}. "
                        + (f"Strategy='{strategy}' is lift-and-shift / close-out — database stays as-is or is retired. "
                           if strategy in non_code_strategies else "")
                        + (f"target_database='{target_db}'." if target_db else "target_database is not set.")
                    ).strip(),
                    "migration_method": None, "target_service": None,
                    "dms_plan": None, "schema_conversion": None,
                    "assessed_by": "AMF deterministic gate (store_analysis_result)",
                    "assessed_at": time.strftime("%Y-%m-%d", time.gmtime()),
                    "evidence": {
                        "migration_strategy": strategy or None,
                        "target_database": target_db or None,
                        "source_database_engine": db_engine or None,
                        "rationale": "DB migration applies only when migration_strategy is "
                                     "replatform/refactor AND target_database is a managed service "
                                     "distinct from the source.",
                    },
                }
                consistency_warnings, meta_warning_attr = [], "execution_database_warnings"
            else:
                consistency_warnings = _stamp_execution_database(result, meta)
                meta_warning_attr = "execution_database_warnings"
        except Exception as _err:  # noqa: BLE001 — never block storing the record
            logger.debug("best-effort step ignored: %s", _err)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if analysis_type == "assessment-summary" and isinstance(result, dict):
        # report_md is rendered as markdown; an agent-supplied markdown report wins,
        # a JSON dump (or none) is replaced by a deterministic rendering.
        supplied = result.pop("report_md", None)
        if isinstance(supplied, str) and supplied.strip() and not _looks_like_json(supplied):
            report_md = supplied
        else:
            report_md = _render_report_md(app_id, analysis_type, result)
    else:
        # Other types keep the JSON string: the UI's StructuredJsonReport parses it
        # (e.g. the execution-database "No database workload" block).
        report_md = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
    item = {
        "PK": f"APP#{app_id}", "SK": f"ANALYSIS#{analysis_type}#{now}",
        "GSI1PK": "TYPE#ANALYSIS", "GSI1SK": f"{app_id}#{analysis_type}",
        "app_id": app_id, "analysis_type": analysis_type,
        "result": result, "data": {"report_md": report_md}, "updated_at": now,
    }
    if consistency_warnings is not None:
        item["consistency_warnings"] = consistency_warnings
    if contradictions is not None:
        item["consistency_contradictions"] = contradictions
    table.put_item(Item=item)
    if meta_warning_attr and consistency_warnings is not None:
        # Mirror onto META (always — an empty list clears stale warnings) so the
        # approval gate's decision_summary surfaces them.
        expr, values = f"SET {meta_warning_attr} = :w", {":w": consistency_warnings}
        if contradictions is not None:
            expr += ", assessment_contradictions = :c"
            values[":c"] = contradictions
        try:
            table.update_item(
                Key={"PK": f"APP#{app_id}", "SK": "META"},
                UpdateExpression=expr,
                ExpressionAttributeValues=values,
            )
        except Exception as _err:  # noqa: BLE001
            logger.debug("best-effort step ignored: %s", _err)
    if BUCKET:
        s3.put_object(
            Bucket=BUCKET, Key=f"applications/{app_id}/analysis/{analysis_type}.json",
            Body=json.dumps(result, default=str), ContentType="application/json",
        )
    out = {"success": True, "app_id": app_id, "analysis_type": analysis_type}
    if contradictions:
        # Returned so the agent can self-correct in the same session; the
        # validate_phase post-check enforces them regardless.
        out["contradictions"] = contradictions
    return out


@registry.tool(
    "compute_risk_score",
    description=(
        "Deterministically score an application's migration risk (0-100, higher = worse) "
        "across Risk/EOSL, Complexity, Team Readiness and Business Readiness. Reads the app's "
        "discovery data + readiness questionnaire, persists a risk-score analysis record, and "
        "mirrors the overall score/tier onto the app. Call once during Discovery after readiness inputs exist."
    ),
    schema={"app_id": {"type": "string", "required": True}},
    # Invoked by explicit name from the Gateway; keep the shape-infer specific so it
    # never steals get_design_document's bare-{app_id} fallback.
    infer=lambda a: a.get("action") == "risk_score",
)
def compute_risk_score(args):
    app_id = args["app_id"]
    meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
    if not meta:
        return {"success": False, "app_id": app_id, "message": "Application not found"}

    # Enrich with repo count from REPO# child rows when META doesn't carry it.
    app_data = {k: v for k, v in meta.items() if k not in _INTERNAL}
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
    # Mirror the headline numbers onto META so list views / pre-hooks can read them cheaply.
    table.update_item(
        Key={"PK": f"APP#{app_id}", "SK": "META"},
        UpdateExpression="SET risk_score = :s, risk_tier = :t, risk_scored_at = :n",
        ExpressionAttributeValues={":s": result["overall"], ":t": result["tier"], ":n": now},
    )
    if BUCKET:
        s3.put_object(
            Bucket=BUCKET, Key=f"applications/{app_id}/analysis/risk-score.json",
            Body=json.dumps(result, default=str), ContentType="application/json",
        )
    return json_safe({"success": True, "app_id": app_id, **result})


@registry.tool(
    "get_analysis_result",
    description="Return the latest analysis result of a type (or all types) for an app.",
    schema={"app_id": {"type": "string", "required": True}, "analysis_type": {"type": "string"}},
    infer=lambda a: "analysis_type" in a and "result" not in a,
)
def get_analysis_result(args):
    app_id, analysis_type = args["app_id"], args.get("analysis_type", "")
    # Records are ANALYSIS#<type>#<ts>: query with begins_with, newest first.
    prefix = f"ANALYSIS#{analysis_type}#" if analysis_type else "ANALYSIS#"
    # A typed read needs only the newest record — don't page every past run.
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(prefix),
        ScanIndexForward=False, **({"Limit": 1} if analysis_type else {}),
    )
    items = resp.get("Items", [])
    if analysis_type:
        items = items[:1]
    results = [{k: v for k, v in i.items() if k not in _INTERNAL} for i in items]
    return json_safe({"app_id": app_id, "results": results, "count": len(results)})
