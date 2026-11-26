"""
Analysis API — manages per-app analysis records (comprehensive, modernization, agentic, etc.)

DynamoDB Schema:
  PK: APP#{app_id}
  SK: ANALYSIS#{analysis_type}#{timestamp}

Each record stores the ATX output for one analysis run.
The assessment report view queries ALL analysis records for an app and groups by type.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from datetime import UTC, datetime
from html.parser import HTMLParser

from boto3.dynamodb.conditions import Key
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from backend.src.auth.rbac import Role, require_role
from backend.src.config import settings
from backend.src.core import aws
from backend.src.core.models import ApiResponse, error_response, generic_error_message, internal_error
from backend.src.core.risk_scoring import calculate_risk_score, render_report_md
from backend.src.db import accessors, keys
from backend.src.db.helpers import from_dynamo, get_table
from backend.src.db.repository import read_full_app

logger = logging.getLogger(__name__)
router = APIRouter()

s3 = aws.client("s3")
def _bucket() -> str:
    """Resolved on use, not at import (no STS call when the module loads)."""
    return settings.get_s3_bucket()

# ── S3 Backfill Helper ─────────────────────────────────────────────────


def _backfill_from_s3(app_id: str, analysis_type: str, table) -> list:
    """Check S3 for analysis data and auto-create DynamoDB record if found.

    This handles the case where the assessment worker uploaded to S3
    but failed to create the DynamoDB ANALYSIS# record.
    """
    from datetime import datetime

    try:
        prefix = f"applications/{app_id}/analysis/{analysis_type}/"
        resp = s3.list_objects_v2(Bucket=_bucket(), Prefix=prefix, Delimiter="/")
        run_prefixes = resp.get("CommonPrefixes", [])

        if not run_prefixes:
            return []

        items = []
        for run_prefix_obj in run_prefixes:
            run_path = run_prefix_obj["Prefix"]
            run_id = run_path.rstrip("/").split("/")[-1]

            # Read key files
            data = {}
            file_map = {
                "ATXDocumentation/technical-debt-report.md": "technical_debt_report",
                "ATXDocumentation/project-overview.md": "project_overview",
                "ATXDocumentation/analysis/security-patterns.md": "security_analysis",
                "ATXDocumentation/analysis/dependency-analysis.md": "dependency_analysis",
                "ATXDocumentation/analysis/complexity-analysis.md": "complexity_analysis",
                "ATXDocumentation/analysis/code-metrics.md": "code_metrics",
                "ATXDocumentation/analysis/tech-debt.md": "tech_debt_analysis",
                "ATXDocumentation/architecture/system-overview.md": "architecture_overview",
                "ATXDocumentation/architecture/components.md": "architecture_components",
                "ATXDocumentation/behavior/business-logic.md": "behavior_business_logic",
                "ATXDocumentation/migration/component-order.md": "migration_order",
            }

            for s3_key, field_name in file_map.items():
                try:
                    obj = s3.get_object(Bucket=_bucket(), Key=run_path + s3_key)
                    data[field_name] = obj["Body"].read().decode("utf-8")[:settings.atx_doc_max_chars]
                except Exception as _err:
                    logger.debug("best-effort step ignored: %s", _err)

            # Check for report.md / report.json
            try:
                objs = s3.list_objects_v2(Bucket=_bucket(), Prefix=run_path, MaxKeys=settings.atx_run_list_max_keys)
                for obj_info in objs.get("Contents", []):
                    key = obj_info["Key"]
                    if (key.endswith("-report.md") or key.endswith("/report.md")) and "ATXDocumentation" not in key:
                        data["report_md"] = (
                            s3.get_object(Bucket=_bucket(), Key=key)["Body"].read().decode("utf-8")
                            [:settings.atx_report_view_max_chars]
                        )
                    if (key.endswith("-report.json") or key.endswith("/report.json")) and "ATXDocumentation" not in key:
                        data["report_json"] = (
                            s3.get_object(Bucket=_bucket(), Key=key)["Body"].read().decode("utf-8")
                            [:settings.atx_report_view_max_chars]
                        )
            except Exception as _err:
                logger.debug("best-effort step ignored: %s", _err)

            if not data:
                continue

            # Create the DynamoDB record
            now = datetime.now(UTC).isoformat()
            key = keys.analysis(app_id, analysis_type, now)
            item = {
                **key.as_key(),
                **key.gsi1_attrs(),
                "app_id": app_id,
                "analysis_type": analysis_type,
                "run_id": run_id,
                "timestamp": now,
                "s3_prefix": f"s3://{_bucket()}/{run_path}",
                "status": "completed",
                "data": data,
                "backfilled": True,
            }
            table.put_item(Item=item)
            items.append(item)
            logger.info("Auto-backfilled ANALYSIS#%s for %s (%d fields)", analysis_type, app_id, len(data))

        return items
    except Exception as exc:
        logger.warning("S3 backfill failed for %s/%s: %s", app_id, analysis_type, exc)
        return []


@router.get("/transformation-gaps")
def get_transformation_gaps():
    """Aggregate 'missing ATX TD' transformation gaps across every app's assessment.

    A gap is a code-change requirement the assessment found that has NO matching
    ATX custom transformation (canonical schema: result.transformations.gaps[]).
    Gaps with the same requirement are grouped so apps sharing a missing custom
    transformation are linked. Single GSI1 query on TYPE#ANALYSIS — never a scan.

    Response: {total_unknown, gaps: [{requirement, reason, severity, scope_samples:[...],
    first_seen, apps:[{app_id, app_name, business_unit, wave_number, language}]}]}
    sorted by severity (high>medium>low) then by affected-app count. `first_seen` is the
    earliest assessment timestamp that reported the gap; the per-app attributes drive the
    UI filters (application / wave / BU / language).
    """
    try:
        # Newest assessment-summary per app (assessment records carry no status).
        records = accessors.list_latest_analysis_by_type("assessment-summary")
        # app_id -> attributes the UI filters on (application / wave / BU / language).
        meta_by_id = {
            a.get("app_id", ""): {
                "app_name": a.get("app_name") or a.get("app_id", ""),
                "business_unit": a.get("business_unit") or "",
                "wave_number": a.get("wave_number"),
                "language": a.get("language") or "",
            }
            for a in accessors.list_all_applications()
        }
        sev_rank = {"high": 0, "medium": 1, "low": 2}

        grouped: dict[str, dict] = {}
        for rec in records:
            app_id = rec.get("app_id", "")
            if not app_id:
                continue
            # When this assessment ran — used to date when the gap was first seen.
            seen_at = rec.get("updated_at") or (rec.get("SK", "").split("#")[-1] if "#" in rec.get("SK", "") else "")
            tx = (rec.get("result") or {}).get("transformations") or {}
            for gap in tx.get("gaps") or []:
                if not isinstance(gap, dict):
                    continue
                req = (gap.get("requirement") or "").strip()
                if not req:
                    continue
                key = req.lower()
                g = grouped.get(key)
                if not g:
                    g = grouped[key] = {
                        "requirement": req,
                        "reason": gap.get("reason", ""),
                        "severity": (gap.get("severity") or "medium").lower(),
                        "scope_samples": [],
                        "apps": [],
                        "first_seen": seen_at or None,
                        "_ids": set(),
                        "_scopes": set(),
                    }
                sev = (gap.get("severity") or "").lower()
                if sev_rank.get(sev, 3) < sev_rank.get(g["severity"], 3):
                    g["severity"] = sev
                scope = (gap.get("scope") or "").strip()
                if scope and scope not in g["_scopes"]:
                    g["_scopes"].add(scope)
                    g["scope_samples"].append(scope)
                # Earliest assessment timestamp wins as "first seen" (ISO sorts lexically).
                if seen_at and (not g["first_seen"] or seen_at < g["first_seen"]):
                    g["first_seen"] = seen_at
                if app_id not in g["_ids"]:
                    g["_ids"].add(app_id)
                    m = meta_by_id.get(app_id, {})
                    g["apps"].append({
                        "app_id": app_id,
                        "app_name": m.get("app_name") or app_id,
                        "business_unit": m.get("business_unit", ""),
                        "wave_number": m.get("wave_number"),
                        "language": m.get("language", ""),
                    })

        gaps = []
        for g in grouped.values():
            g.pop("_ids", None)
            g.pop("_scopes", None)
            gaps.append(g)
        gaps.sort(key=lambda g: (sev_rank.get(g["severity"], 3), -len(g["apps"])))

        return ApiResponse(data={"total_unknown": len(gaps), "gaps": gaps})
    except Exception as exc:
        logger.error("transformation-gaps aggregation failed: %s", exc)
        return internal_error(exc, code="GAPS_FAILED")


@router.post("/{app_id}/risk-score", dependencies=[require_role(Role.ENGINEER)])
def compute_risk_score(app_id: str):
    """Deterministically (re)score an app's migration risk from its current discovery
    data + readiness questionnaires (0-100, higher = worse). Persists an
    `ANALYSIS#risk-score` record and mirrors `risk_score`/`risk_tier` onto META so the
    Discovery step always has a fresh Category/Score/Reason table. Same
    `backend.src.core.risk_scoring` module the Gateway tool + post-hook use.
    """
    try:
        app = read_full_app(get_table(), app_id)
        if not app:
            return error_response("Application not found", code="NOT_FOUND")

        result = calculate_risk_score(app, settings.risk_eosl_horizon_year)
        report_md = render_report_md(app.get("app_name", app_id), result)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        accessors.put(
            keys.analysis(app_id, "risk-score", ts),
            {"app_id": app_id, "analysis_type": "risk-score",
             "result": result, "data": {"report_md": report_md}, "updated_at": ts},
        )
        get_table().update_item(
            Key={"PK": f"APP#{app_id}", "SK": "META"},
            UpdateExpression="SET risk_score = :s, risk_tier = :t, risk_scored_at = :n",
            ExpressionAttributeValues={":s": result["overall"], ":t": result["tier"], ":n": ts},
        )
        return ApiResponse(data=result)
    except Exception as exc:
        logger.error("risk-score computation failed for %s: %s", app_id, exc)
        return internal_error(exc, code="RISK_SCORE_FAILED")


@router.get("/{app_id}/{analysis_type}")
def get_analysis_by_type(app_id: str, analysis_type: str):
    """Get all runs of a specific analysis type for an app.

    Falls back to S3 if no DynamoDB records exist (auto-backfill).
    """
    try:
        table = get_table()
        resp = table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(f"ANALYSIS#{analysis_type}#")
            ),
            ScanIndexForward=False,
        )
        items = from_dynamo(resp.get("Items", []))

        # If no DynamoDB records, check S3 and auto-backfill
        if not items:
            items = _backfill_from_s3(app_id, analysis_type, table)

        return ApiResponse(data=items)
    except Exception as exc:
        return internal_error(exc, code="QUERY_FAILED")


# Report HTML comes from repo-derived content (ATX output, agent markdown): it must
# never run script or load anything. Inline styles only.
# `sandbox` + base-uri/form-action also stop the document navigating or submitting anywhere.
REPORT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'; sandbox"
)


_REPORT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class _NavigationTagFinder(HTMLParser):
    """Collects the source spans of tags that navigate without a click (CSP can't block
    `<meta http-equiv=refresh>`) or retarget relative links (`<base>`)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.spans: list[tuple[tuple[int, int], int]] = []

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "base" or (
            tag == "meta" and any(k == "http-equiv" and (v or "").strip().lower() == "refresh" for k, v in attrs)
        ):
            self.spans.append((self.getpos(), len(self.get_starttag_text() or "")))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)


def _strip_navigation(content: str) -> str:
    """Remove meta-refresh and <base> tags from untrusted report HTML (no open redirect)."""
    finder = _NavigationTagFinder()
    try:
        finder.feed(content)
        finder.close()
    except Exception:  # malformed markup: refuse to serve it raw
        return f"<pre>{html_lib.escape(content)}</pre>"
    if not finder.spans:
        return content
    line_starts = [0]
    for line in content.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    out: list[str] = []
    pos = 0
    for (lineno, col), length in finder.spans:
        start = line_starts[lineno - 1] + col
        out.append(content[pos:start])
        pos = start + length
    out.append(content[pos:])
    return "".join(out)


def _html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content=_strip_navigation(content),
        status_code=status_code,
        headers={
            "Content-Security-Policy": REPORT_CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/{app_id}/{analysis_type}/html")
def get_analysis_html_report(app_id: str, analysis_type: str):
    """Get the report for the latest analysis run.

    Finds markdown files in S3, converts to simple HTML, and returns. Every response
    carries REPORT_CSP (no scripts, no network loads): an ATX-produced .html object is
    served as-is but inert, and markdown is HTML-escaped by _md_to_html.
    """
    # Both values build DynamoDB keys and the S3 prefix below — refuse anything but a plain id.
    if not (_REPORT_ID_RE.fullmatch(app_id) and _REPORT_ID_RE.fullmatch(analysis_type)):
        return _html("<p>Invalid app id or analysis type.</p>", status_code=400)
    try:
        # Find the latest run
        table = get_table()
        resp = table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with(f"ANALYSIS#{analysis_type}#")
            ),
            ScanIndexForward=False,
            Limit=1,
        )
        items = from_dynamo(resp.get("Items", []))
        if not items:
            return _html("<p>No analysis found. Run an assessment first.</p>", status_code=404)

        item = items[0]
        s3_prefix = item.get("s3_prefix", "")
        run_id = item.get("run_id", "")

        # Fast path: check if report_md is in the ANALYSIS record data
        data = item.get("data", {})
        if isinstance(data, dict) and data.get("report_md"):
            html = _md_to_html(data["report_md"])
            return _html(html)

        # Also check top-level report_md (older records)
        if item.get("report_md"):
            html = _md_to_html(item["report_md"])
            return _html(html)

        if not s3_prefix and not run_id:
            return _html("<p>No S3 data available for this run.</p>", status_code=404)

        # Determine S3 prefix
        if s3_prefix and s3_prefix.startswith("s3://"):
            bucket_prefix = s3_prefix.replace(f"s3://{_bucket()}/", "")
        else:
            bucket_prefix = f"applications/{app_id}/analysis/{analysis_type}/{run_id}/"

        # List objects — find .html first, then .md files
        resp_s3 = s3.list_objects_v2(Bucket=_bucket(), Prefix=bucket_prefix, MaxKeys=100)
        objects = resp_s3.get("Contents", [])

        # Try HTML first
        for obj in objects:
            if obj["Key"].endswith(".html"):
                html_obj = s3.get_object(Bucket=_bucket(), Key=obj["Key"])
                return _html(html_obj["Body"].read().decode("utf-8"))

        # Fall back to markdown — concatenate all .md files and render as HTML
        md_contents = []
        for obj in sorted(objects, key=lambda x: x["Key"]):
            if obj["Key"].endswith(".md"):
                md_obj = s3.get_object(Bucket=_bucket(), Key=obj["Key"])
                md_text = md_obj["Body"].read().decode("utf-8")
                md_contents.append(md_text)

        if not md_contents:
            return _html("<p>No report files found in S3.</p>", status_code=404)

        # Simple markdown to HTML conversion (no external lib needed)
        combined_md = "\n\n---\n\n".join(md_contents)
        html = _md_to_html(combined_md)
        return _html(html)

    except Exception as exc:
        logger.error("Failed to get report for %s/%s: %s", app_id, analysis_type, exc)
        msg = html_lib.escape(generic_error_message(exc, "REPORT_RENDER_FAILED", f"{app_id}/{analysis_type}"))
        return _html(f"<p>{msg}</p>", status_code=500)


_CODE_OPEN = '<code style="background:#f0f0f0;padding:1px 4px;border-radius:2px;">'


def _wrap_spans(line: str, marker: str, open_tag: str, close_tag: str) -> str:
    """Wrap each closed, non-empty `marker`…`marker` span in tags — a linear str scan, no regex.

    Unpaired markers stay literal. Content containing the marker's character is left as-is
    (matches the old `[^*]+` / `[^`]+` semantics).
    """
    out: list[str] = []
    pos = 0
    n = len(marker)
    while True:
        start = line.find(marker, pos)
        if start < 0:
            break
        end = line.find(marker, start + n)
        if end < 0:
            break
        inner = line[start + n:end]
        if inner and marker[0] not in inner:
            out.append(line[pos:start])
            out.append(f"{open_tag}{inner}{close_tag}")
            pos = end + n
        else:
            out.append(line[pos:start + 1])
            pos = start + 1
    out.append(line[pos:])
    return "".join(out)


def _md_to_html(md: str) -> str:
    """Convert markdown to styled HTML (simple conversion, no external deps).

    Every line of input text is HTML-escaped before markup is added (R6): the markdown is repo/agent-derived, so raw HTML in it renders as text.
    """
    lines = md.split("\n")
    html_lines = []
    in_code = False
    in_list = False

    for line in lines:
        # Code blocks
        if line.strip().startswith("```"):
            if in_code:
                html_lines.append("</pre>")
                in_code = False
            else:
                html_lines.append(
                    "<pre style='background:#f4f4f4;padding:12px;border-radius:4px;overflow-x:auto;font-size:12px;'>"
                )
                in_code = True
            continue
        # Escape first; the markdown markers below (#, -, *, `) are not escaped.
        line = html_lib.escape(line)
        if in_code:
            html_lines.append(line)
            continue

        # Headers
        if line.startswith("#### "):
            html_lines.append(f"<h4>{line[5:]}</h4>")
        elif line.startswith("### "):
            html_lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            html_lines.append(f"<h2 style='border-bottom:1px solid #eee;padding-bottom:4px;'>{line[3:]}</h2>")
        elif line.startswith("# "):
            html_lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("---"):
            html_lines.append("<hr style='border:none;border-top:1px solid #ddd;margin:16px 0;'>")
        elif line.startswith("- ") or line.startswith("* "):
            if not in_list:
                html_lines.append("<ul style='margin:4px 0;'>")
                in_list = True
            html_lines.append(f"<li>{line[2:]}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if line.strip():
                # Bold and inline code
                line = _wrap_spans(line, "**", "<strong>", "</strong>")
                line = _wrap_spans(line, "`", _CODE_OPEN, "</code>")
                html_lines.append(f"<p style='margin:4px 0;'>{line}</p>")
            else:
                html_lines.append("")

    if in_list:
        html_lines.append("</ul>")

    body = "\n".join(html_lines)
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:14px;line-height:1.6;padding:16px;max-width:900px;">{body}</div>"""

