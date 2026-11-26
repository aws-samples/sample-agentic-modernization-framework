"""Deterministic migration risk scoring (Discovery phase).

Pure, dependency-free (stdlib only) so the SAME module is the single source of
truth for both the FastAPI control plane *and* the `compute_risk_score` AgentCore
Gateway tool (Terraform vendors this file into the tool-Lambda zip; see
`infra/environments/agentcore/lambdas.tf`). Do NOT import project modules here.

Score model (confirmed with product): **risk score — higher = worse**. Four
categories each contribute 0..25 risk points; the aggregate is 0..100 where 100
is the highest risk / least ready to migrate. Every category returns a
`{category, score, max, reason}` row so the UI can render a Category/Score/Reason
table directly.

    result = calculate_risk_score(app_data)
    # {
    #   "overall": 58, "max": 100, "tier": "High",
    #   "categories": [ {category, score, max, reason}, ... ],
    #   "inputs_complete": bool,
    # }
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

CATEGORY_MAX = 25

# EOSL before this year scores mid risk. This module can't import the project
# Settings (it is vendored into Lambda zips), so the default is read from the
# RISK_EOSL_HORIZON_YEAR env var (Lambda env / API env, default 2032); the API
# passes `settings.risk_eosl_horizon_year` explicitly.
EOSL_HORIZON_YEAR_DEFAULT = int(os.environ.get("RISK_EOSL_HORIZON_YEAR", "2032") or "2032")
EOSL_BEFORE_HORIZON_POINTS = 13

# Complexity thresholds (risk points per band).
DEPS_LOW_MAX = 4        # deps <= this → 0 pts
DEPS_MEDIUM_MAX = 10    # deps <= this → DEPS_MEDIUM_POINTS, else DEPS_HIGH_POINTS
DEPS_MEDIUM_POINTS = 5
DEPS_HIGH_POINTS = 10
REPOS_LOW_MAX = 1       # repos <= this → 0 pts
REPOS_MEDIUM_MAX = 3    # repos <= this → REPOS_MEDIUM_POINTS, else REPOS_HIGH_POINTS
REPOS_MEDIUM_POINTS = 2
REPOS_HIGH_POINTS = 5
OVERALL_MAX = 4 * CATEGORY_MAX  # 100

# Higher tier = higher risk. Bands over the 0..100 aggregate.
TIER_BANDS = [(0, 33, "Low"), (34, 66, "Medium"), (67, 100, "High")]

# ── Readiness questionnaire (Discovery user input) ────────────────────────────
# The frontend renders one Select per question; the skill documents the same set;
# the tool reads the persisted answers. Keep the three in sync via these consts.
STATUS_OPTIONS = ["Completed", "In Progress", "Initiated", "Pending", "Not Started"]
# risk points per answer (higher = less ready = more risk). Unknown/blank → worst.
STATUS_POINTS = {
    "completed": 0,
    "in progress": 2,
    "initiated": 3,
    "pending": 4,
    "not started": 5,
}
_WORST_STATUS_POINTS = 5

BUSINESS_QUESTIONS = [
    ("stakeholder_approval", "Stakeholder approval"),
    ("cost_approval", "Cost / budget approval"),
    ("dr_signoff", "DR / business-continuity sign-off"),
    ("compliance_signoff", "Compliance / security sign-off"),
    ("migration_window", "Migration window agreed"),
]
TEAM_QUESTIONS = [
    ("team_availability", "Team availability for migration"),
    ("knowledge_transfer", "Knowledge transfer completed"),
    ("aws_skills", "AWS skills / training"),
    ("runbook_ownership", "Runbook & ownership defined"),
    ("tooling_access", "Tooling & access provisioned"),
]


# ── helpers ───────────────────────────────────────────────────────────────────
def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_year(value: Any) -> int | None:
    """Pull a 4-digit year out of an EOSL value (date, ISO string, or year)."""
    if value is None or value == "":
        return None
    if isinstance(value, (date, datetime)):
        return value.year
    s = str(value).strip()
    # try a full ISO date first, else the leading 4-digit year
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).year
        except ValueError:
            continue
    digits = "".join(ch for ch in s[:4] if ch.isdigit())
    yr = _to_int(digits)
    return yr if yr and 1990 <= yr <= 2100 else None


def _status_points(answer: Any) -> int:
    if answer is None:
        return _WORST_STATUS_POINTS
    return STATUS_POINTS.get(str(answer).strip().lower(), _WORST_STATUS_POINTS)


def _dependency_count(app: dict) -> int:
    """Best-effort dependency count from whatever the record carries."""
    explicit = _to_int(app.get("dependencies_count") or app.get("dependency_count"))
    if explicit is not None:
        return explicit
    total = 0
    # Discovery sometimes stores deps flat (dependencies_upstream / _downstream)
    # and sometimes nested under `dependencies.{upstream,downstream}` — count
    # both so no genuine dependency load is scored as 0.
    for key in ("dependencies_upstream", "dependencies_downstream", "dependencies"):
        v = app.get(key)
        if isinstance(v, (list, tuple, set)):
            total += len(v)
        elif isinstance(v, dict):
            for sub in ("upstream", "downstream"):
                sv = v.get(sub)
                if isinstance(sv, (list, tuple, set)):
                    total += len(sv)
                elif isinstance(sv, str) and sv.strip():
                    total += len([p for p in sv.replace("\n", ",").split(",") if p.strip()])
        elif isinstance(v, str) and v.strip():
            total += len([p for p in v.replace("\n", ",").split(",") if p.strip()])
    return total


def _repo_count(app: dict) -> int:
    explicit = _to_int(app.get("repo_count") or app.get("repository_count"))
    if explicit is not None:
        return explicit
    repos = app.get("repos") or app.get("repositories")
    return len(repos) if isinstance(repos, (list, tuple)) else 0


def _tests_present(app: dict) -> int:
    """Count of {unit, qa, functional} test types the app declares it has (0..3)."""
    present = 0
    flag_map = {
        "unit": ("unit_tests", "has_unit_tests", "unit_test"),
        "qa": ("qa_tests", "has_qa_tests", "qa_test"),
        "functional": ("functional_tests", "has_functional_tests", "functional_test"),
    }
    # 1) explicit boolean-ish flags on the record
    for keys in flag_map.values():
        if any(_is_truthy(app.get(k)) for k in keys):
            present += 1
            continue
    if present:
        return min(present, 3)
    # 2) fall back to a testing_strategy list/string (engineer inputs)
    strat = app.get("testing_strategy") or app.get("tests") or app.get("test_coverage")
    text = ""
    if isinstance(strat, (list, tuple, set)):
        text = " ".join(str(x) for x in strat).lower()
    elif isinstance(strat, str):
        text = strat.lower()
    for kw_group in (("unit",), ("qa", "quality"), ("functional", "e2e", "integration")):
        if any(kw in text for kw in kw_group):
            present += 1
    return min(present, 3)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "yes", "y", "1", "available", "present"}


def _tier(overall: int) -> str:
    for lo, hi, label in TIER_BANDS:
        if lo <= overall <= hi:
            return label
    return "High"


# ── category scorers (each returns a {category, score, max, reason} row) ───────
def _score_eosl_risk(app: dict, horizon_year: int | None = None) -> dict:
    # Discovery data stores the EOSL date under `constraints.eosl_date` on most
    # apps and some older ones at META top-level. Check both so we never silently
    # score "no EOSL" (0 points) for an app that actually has one.
    cons = app.get("constraints") or {}
    raw = (app.get("eosl_date") or app.get("eosl") or app.get("end_of_support")
           or (isinstance(cons, dict) and cons.get("eosl_date")))
    year = _parse_year(raw)
    today = date.today()
    if year is None:
        return _row("Risk (EOSL)", 0, "No EOSL recorded — treated as supported")
    if _is_past(raw, today):
        return _row("Risk (EOSL)", CATEGORY_MAX, f"EOSL expired ({year})")
    horizon = int(horizon_year) if horizon_year else EOSL_HORIZON_YEAR_DEFAULT
    if year < horizon:
        return _row("Risk (EOSL)", EOSL_BEFORE_HORIZON_POINTS, f"EOSL before {horizon} ({year})")
    return _row("Risk (EOSL)", 0, f"EOSL {year} (>= {horizon})")


def _is_past(raw: Any, today: date) -> bool:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(raw)[:10], fmt).date() < today
        except ValueError:
            continue
    yr = _parse_year(raw)
    return bool(yr and yr < today.year)


def _score_complexity(app: dict) -> dict:
    deps = _dependency_count(app)
    repos = _repo_count(app)
    tests = _tests_present(app)
    # Machine count — when discovery linked them. More hosts to cut over = more risk.
    machines = _to_int(app.get("machine_count"))
    if machines is None:
        machines = len(app.get("machine_ids") or []) if isinstance(app.get("machine_ids"), (list, tuple)) else 0

    dep_pts = 0 if deps <= DEPS_LOW_MAX else (DEPS_MEDIUM_POINTS if deps <= DEPS_MEDIUM_MAX else DEPS_HIGH_POINTS)
    repo_pts = (0 if repos <= REPOS_LOW_MAX
                else (REPOS_MEDIUM_POINTS if repos <= REPOS_MEDIUM_MAX else REPOS_HIGH_POINTS))
    # more test types present => less risk. 3 present = 0, 0 present = 5.
    test_pts = round((3 - tests) / 3 * 5)
    # Machines: 1 or none = 0, 2-3 = 2, 4-9 = 4, 10+ = 5.
    if machines <= 1:
        machine_pts = 0
    elif machines <= 3:
        machine_pts = 2
    elif machines <= 9:
        machine_pts = 4
    else:
        machine_pts = 5
    score = min(dep_pts + repo_pts + test_pts + machine_pts, CATEGORY_MAX)

    test_label = {3: "all test types", 2: "2 test types", 1: "1 test type", 0: "no tests"}[tests]
    reason = f"{deps} deps, {repos} repo(s), {machines} machine(s), {test_label}"
    return _row("Complexity", score, reason)


def _score_readiness(app: dict, questions: list[tuple[str, str]], meta_key: str, label: str) -> dict:
    answers = app.get(meta_key) or {}
    if not isinstance(answers, dict):
        answers = {}
    score = 0
    unanswered = []
    for key, _text in questions:
        ans = answers.get(key)
        if ans in (None, ""):
            unanswered.append(key)
        score += _status_points(ans)
    score = min(score, CATEGORY_MAX)
    if unanswered:
        reason = f"{len(questions) - len(unanswered)}/{len(questions)} answered; unanswered treated as not started"
    else:
        # summarise how many are complete
        done = sum(1 for k, _ in questions if str(answers.get(k, "")).strip().lower() == "completed")
        reason = f"{done}/{len(questions)} completed"
    return _row(label, score, reason)


def _row(category: str, score: int, reason: str) -> dict:
    return {"category": category, "score": int(score), "max": CATEGORY_MAX, "reason": reason}


# ── public API ─────────────────────────────────────────────────────────────────
def calculate_risk_score(app_data: dict, eosl_horizon_year: int | None = None) -> dict:
    """Score an application's migration risk (0..100, higher = worse).

    `app_data` is a plain dict as returned by `repository.read_full_app` (app META
    merged with `repos`/`owners`) or the app META item. All keys are optional;
    missing readiness answers are treated as the worst (Not Started).
    `eosl_horizon_year` overrides the EOSL horizon (default: RISK_EOSL_HORIZON_YEAR / 2032).
    """
    app = app_data or {}
    categories = [
        _score_eosl_risk(app, eosl_horizon_year),
        _score_complexity(app),
        _score_readiness(app, TEAM_QUESTIONS, "team_readiness", "Team Readiness"),
        _score_readiness(app, BUSINESS_QUESTIONS, "business_readiness", "Business Readiness"),
    ]
    overall = sum(c["score"] for c in categories)
    business = app.get("business_readiness") or {}
    team = app.get("team_readiness") or {}
    inputs_complete = bool(
        isinstance(business, dict)
        and isinstance(team, dict)
        and all(business.get(k) for k, _ in BUSINESS_QUESTIONS)
        and all(team.get(k) for k, _ in TEAM_QUESTIONS)
    )
    return {
        "overall": overall,
        "max": OVERALL_MAX,
        "tier": _tier(overall),
        "categories": categories,
        "inputs_complete": inputs_complete,
    }


def render_report_md(app_name: str, result: dict) -> str:
    """Human-readable markdown table (stored alongside the analysis record)."""
    lines = [
        f"# Migration Risk Score — {app_name}",
        "",
        f"**Overall: {result['overall']}/{result['max']} — {result['tier']} risk**  "
        "_(higher = more risk / less ready)_",
        "",
        "| Category | Score | Reason |",
        "|---|---|---|",
    ]
    for c in result["categories"]:
        lines.append(f"| {c['category']} | {c['score']}/{c['max']} | {c['reason']} |")
    if not result.get("inputs_complete"):
        lines += ["", "> ⚠️ Readiness questionnaire incomplete — unanswered items scored as highest risk."]
    return "\n".join(lines)
