"""
Design Document API.
Generates and manages migration/modernization design documents per application.

Flow:
  1. POST /generate/{app_id} → Gathers all data, calls Bedrock, stores in S3
  2. GET /{app_id} → Returns the latest design document

Data sources for generation:
  - Portfolio: app metadata, machines, repos, owners
  - Assessment: strategy, confidence, tool results (code, component, dependency)
  - Wave Plan: wave assignment, dependencies
  - User Notes: DR requirements, target region, architecture preferences
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter

from backend.src.config import settings
from backend.src.core.models import ApiResponse, error_response, internal_error
from backend.src.db.helpers import call_with_credential_refresh, get_table

logger = logging.getLogger(__name__)
router = APIRouter()

# Bucket is resolved per-request via settings.get_s3_bucket() rather than
# captured at import time: an import-time STS hiccup once yielded a wrong
# (account-less) bucket that persisted for the whole process.
DESIGN_PREFIX = "applications"


def _schedule_risk_banner(meta: dict) -> str:
    """Return a markdown "Schedule Risk" callout when EOSL + hard-deadline are tight.

    The LLM has repeatedly failed to surface a 1-month buffer between an EOSL and a
    hard-cutover deadline as a top-level risk. Compute it deterministically on read
    and prepend a banner so it's always visible — never swallowed by prose.
    Thresholds: `warn` < settings.schedule_risk_warn_days (90) of buffer, `critical` when
    the hard deadline lands on/after EOSL.
    Returns "" when the data is incomplete or the buffer is comfortable.
    """
    from datetime import date
    cons = meta.get("constraints") or {}
    eosl_raw = meta.get("eosl_date") or cons.get("eosl_date")
    hd_raw = meta.get("hard_deadline") or cons.get("hard_deadline")

    def _parse(v):
        if not v:
            return None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(str(v)[:10], fmt).date()
            except ValueError:
                continue
        return None

    eosl, hd = _parse(eosl_raw), _parse(hd_raw)
    if not eosl or not hd:
        return ""
    # Safe when the migration lands BEFORE EOSL (gap positive). Running past EOSL
    # means operating unsupported — treat as critical regardless of buffer size.
    gap_days = (eosl - hd).days
    today = date.today()
    if gap_days >= settings.schedule_risk_warn_days:
        return ""  # comfortable buffer
    if gap_days <= 0:
        severity = "🔴 CRITICAL"
        detail = (
            f"Hard deadline **{hd.isoformat()}** lands "
            + ("on or " if gap_days == 0 else "")
            + f"**after EOSL {eosl.isoformat()}** — the app will run unsupported "
              f"for {abs(gap_days)} day(s) before migration completes."
        )
    else:
        severity = "🟡 WARNING"
        detail = (
            f"Only **{gap_days} day(s)** between hard deadline ({hd.isoformat()}) "
            f"and EOSL ({eosl.isoformat()}) — any slip in transforms puts the app "
            "into unsupported operation."
        )
    days_to_eosl = (eosl - today).days
    return (
        "> ### " + severity + " — Schedule Risk\n"
        "> " + detail + f" _({days_to_eosl} days to EOSL from today)_\n"
        "> This banner is injected deterministically from app constraints; cannot be hidden by the design-doc author.\n\n"
    )


# label-fragment (case-insensitive, substring) -> META field name. Constant, precompiled patterns.
_CONFIRMED_FIELDS = [
    (re.compile(r"aws\s*account\s*id|account\s*id|target\s*account"), "target_account"),
    (re.compile(r"vpc\s*cidr|cidr\s*(?:range|block)?"), "vpc_cidr"),
    (re.compile(r"target\s*region|aws\s*region"), "target_region"),
    (re.compile(r"target\s*compute|compute\s*service"), "target_compute"),
]


def _sanitize_design_doc(content: str, meta: dict) -> str:
    """Deterministically replace `[REQUIRES INPUT]` placeholders for values that
    are already CONFIRMED on the app (account, VPC CIDR, region, compute).

    The design prompt tells the model not to emit `[REQUIRES INPUT]` for these,
    but the LLM has been observed to slip and leave the placeholder in a
    Required Inputs table even when the account was honored elsewhere. Fixing it
    on read is cheap, idempotent, and never regresses — anything genuinely
    missing on META is left as `[REQUIRES INPUT]` for the human to fill.
    """
    if not content:
        return content

    # Deterministic Schedule Risk banner (EOSL vs hard-deadline tightness). Idempotent:
    # the marker in the banner prevents double-prepend on repeated reads/sanitizes.
    SR_MARKER = "Schedule Risk\n> "
    banner = _schedule_risk_banner(meta)
    if banner and SR_MARKER not in content:
        content = banner + content

    out_lines = []
    for line in content.splitlines(keepends=True):
        if "[REQUIRES INPUT]" in line:
            lower = line.lower()
            for pattern, field in _CONFIRMED_FIELDS:
                if pattern.search(lower):
                    val = meta.get(field)
                    if val:
                        line = line.replace("[REQUIRES INPUT]", str(val))
                        break
        out_lines.append(line)
    return "".join(out_lines)


def _s3_call(method: str, **kwargs):
    """Call S3 with one credential refresh and a newly constructed client."""
    return call_with_credential_refresh(
        lambda: getattr(boto3.client("s3", region_name=settings.aws_region), method)(**kwargs)
    )


def _is_no_such_key(exc: ClientError) -> bool:
    code = (exc.response.get("Error") or {}).get("Code", "")
    return code in {"NoSuchKey", "404"}


# ── GET /{app_id} — Get design document ───────────────────────────────


@router.get("/{app_id}")
def get_design_document(app_id: str):
    """Get the latest design document for an application."""
    try:
        table = get_table()
        meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {})
        current_status = meta.get("design_status", "")

        # Try versioned files first (from AgentCore Harness: v1.md, v2.md, etc.)
        # Find the latest version
        from boto3.dynamodb.conditions import Key as DKey
        design_records = table.query(
            KeyConditionExpression=DKey("PK").eq(f"APP#{app_id}") & DKey("SK").begins_with("DESIGN#"),
            ScanIndexForward=False,
            Limit=1,
        ).get("Items", [])

        if design_records:
            s3_key = design_records[0].get("s3_key", "")
            if s3_key:
                try:
                    resp = _s3_call("get_object", Bucket=settings.get_s3_bucket(), Key=s3_key)
                    content = _sanitize_design_doc(resp["Body"].read().decode("utf-8"), meta)
                    return ApiResponse(data={
                        "app_id": app_id,
                        "content": content,
                        "version": design_records[0].get("version", ""),
                        "last_modified": resp["LastModified"].isoformat(),
                        "size": resp["ContentLength"],
                        "status": "generated",
                    })
                except Exception as _err:
                    logger.debug("best-effort step ignored: %s", _err)

        # Fallback: legacy path (design-document.md)
        key = f"{DESIGN_PREFIX}/{app_id}/design/design-document.md"
        resp = _s3_call("get_object", Bucket=settings.get_s3_bucket(), Key=key)
        content = _sanitize_design_doc(resp["Body"].read().decode("utf-8"), meta)
        return ApiResponse(
            data={
                "app_id": app_id,
                "content": content,
                "last_modified": resp["LastModified"].isoformat(),
                "size": resp["ContentLength"],
                "status": current_status if current_status == "generating" else "generated",
            }
        )
    except ClientError as exc:
        if not _is_no_such_key(exc):
            logger.error("Failed to get design doc for %s: %s", app_id, exc)
            return internal_error(exc, code="S3_ERROR")
        # Check if generation is in progress
        table = get_table()
        meta = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {})
        design_status = meta.get("design_status", "")
        if design_status == "generating":
            return ApiResponse(data={"app_id": app_id, "status": "generating", "content": None})
        if design_status == "error":
            return ApiResponse(
                data={"app_id": app_id, "status": "error", "error": meta.get("design_error", ""), "content": None}
            )
        return error_response(f"No design document found for {app_id}. Generate one first.", code="NOT_FOUND")
    except Exception as exc:
        logger.error("Failed to get design doc for %s: %s", app_id, exc)
        return internal_error(exc, code="S3_ERROR")


# Note: design documents are generated by the AgentCore Harness in the
# Design Step Functions state (single architecture) — there is no
# standalone ECS "design" generation endpoint; GET /{app_id} is a read view
# over the design docs the Harness persists to S3/DynamoDB.
