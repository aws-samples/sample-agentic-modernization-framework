"""
DynamoDB operations for the ATX worker (workbench transformation + analysis records).
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.src.worker.config import (
    APP_ID,
    PLAN_ID,
    ddb_table,
    logger,
    to_dynamo,
)

# ── Workbench DynamoDB ops ─────────────────────────────────────────────


def update_plan_step_status(step_num: int, status: str, message: str = "", s3_report: str = ""):
    """Update a single step's status (and optional S3 report key) in the plan record."""
    try:
        resp = ddb_table.get_item(Key={"PK": f"APP#{APP_ID}", "SK": f"PLAN#{PLAN_ID}"})
        item = resp.get("Item")
        if not item:
            return
        from backend.src.db.helpers import from_dynamo

        plan = from_dynamo(item)
        steps = plan.get("steps", [])
        for s in steps:
            if s.get("step_number") == step_num:
                s["status"] = status
                s["message"] = message
                s["updated_at"] = datetime.now(UTC).isoformat()
                if s3_report:
                    s["s3_report"] = s3_report
                break

        ddb_table.update_item(
            Key={"PK": f"APP#{APP_ID}", "SK": f"PLAN#{PLAN_ID}"},
            UpdateExpression="SET steps = :s, updated_at = :ua",
            ExpressionAttributeValues=to_dynamo({":s": steps, ":ua": datetime.now(UTC).isoformat()}),
        )
    except Exception as exc:
        logger.warning("Failed to update step %d status: %s", step_num, exc)


def update_plan_status(status: str, **kwargs):
    """Update the plan's overall status."""
    now = datetime.now(UTC).isoformat()
    update_expr = "SET #st = :s, updated_at = :ua"
    expr_names = {"#st": "status"}
    expr_vals: dict = {":s": status, ":ua": now}

    for key, attr in [
        ("failed_step", ":fs"),
        ("completed_at", ":ca"),
        ("synthesis", ":syn"),
        ("error_message", ":em"),
        ("s3_prefix", ":sp"),
        ("outcome_summary", ":os"),
    ]:
        if key in kwargs:
            update_expr += f", {key} = {attr}"
            expr_vals[attr] = kwargs[key]

    ddb_table.update_item(
        Key={"PK": f"APP#{APP_ID}", "SK": f"PLAN#{PLAN_ID}"},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=to_dynamo(expr_vals),
    )


# ── Analysis (ATX) record ──────────────────────────────────────────────


def store_analysis_record(app_id: str, atx_output: dict):
    """Create an ANALYSIS# record in DynamoDB for one ATX run.

    Single source of truth for turning ATX output (or the .md files it wrote to
    S3) into the ANALYSIS# record the Assessment Harness / frontend read. Used by
    the code-analysis worker mode.
    """
    from backend.src.worker.config import ATX_S3_OUTPUT_BUCKET, ATX_TD_NAME, s3_client

    td_name = ATX_TD_NAME
    analysis_type = td_name.replace("AWS/", "")
    run_id = atx_output.get("atx_run_id", "")
    s3_prefix = atx_output.get("s3_prefix", "")

    if not run_id:
        return

    # Extract the relevant data based on analysis type
    data: dict = {}
    if "modernization_report" in atx_output:
        data["report_md"] = atx_output["modernization_report"]
    if "modernization_report_json" in atx_output:
        data["report_json"] = atx_output["modernization_report_json"]
    if "agentic_report" in atx_output:
        data["report_md"] = atx_output["agentic_report"]
    if "agentic_report_json" in atx_output:
        data["report_json"] = atx_output["agentic_report_json"]

    # For comprehensive, store key docs
    comprehensive_keys = [
        "project_overview",
        "technical_debt_report",
        "architecture_overview",
        "architecture_components",
        "diagrams_architecture",
        "diagrams_behavioral",
        "security_analysis",
        "code_metrics",
        "complexity_analysis",
        "migration_order",
        "remediation_plan",
        "behavior_business_logic",
    ]
    for k in comprehensive_keys:
        if k in atx_output:
            data[k] = atx_output[k]

    # If data is still empty, read .md files from S3 and store content
    if not data and s3_prefix:
        try:
            bucket_prefix = (
                s3_prefix.replace(f"s3://{ATX_S3_OUTPUT_BUCKET}/", "")
                if s3_prefix.startswith("s3://")
                else f"applications/{app_id}/analysis/{analysis_type}/{run_id}/"
            )
            resp_s3 = s3_client.list_objects_v2(Bucket=ATX_S3_OUTPUT_BUCKET, Prefix=bucket_prefix, MaxKeys=50)
            md_content = []
            for obj in sorted(resp_s3.get("Contents", []), key=lambda x: x["Key"]):
                if obj["Key"].endswith(".md"):
                    md_obj = s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=obj["Key"])
                    md_content.append(md_obj["Body"].read().decode("utf-8"))
            if md_content:
                combined = "\n\n---\n\n".join(md_content)
                data["report_md"] = combined[:400000]
                logger.info("Loaded %d .md files from S3 into ANALYSIS record (%d chars)", len(md_content), len(combined))
        except Exception as exc:
            logger.warning("Failed to read .md files from S3 for ANALYSIS record: %s", exc)

    # Last resort: read known ATXDocumentation files from the canonical S3 path
    if not data and run_id:
        try:
            bucket_prefix = f"applications/{app_id}/analysis/{analysis_type}/{run_id}/"
            resp_s3 = s3_client.list_objects_v2(Bucket=ATX_S3_OUTPUT_BUCKET, Prefix=bucket_prefix, MaxKeys=100)
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
            for obj in resp_s3.get("Contents", []):
                key = obj["Key"]
                for suffix, field in file_map.items():
                    if key.endswith(suffix):
                        data[field] = (
                            s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=key)["Body"].read().decode("utf-8")[:8000]
                        )
                        break
                if key.endswith("-report.md") and "ATXDocumentation" not in key:
                    data["report_md"] = (
                        s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=key)["Body"].read().decode("utf-8")[:50000]
                    )
                if key.endswith("-report.json") and "ATXDocumentation" not in key:
                    data["report_json"] = (
                        s3_client.get_object(Bucket=ATX_S3_OUTPUT_BUCKET, Key=key)["Body"].read().decode("utf-8")[:50000]
                    )
            if data:
                logger.info("Last-resort S3 read: loaded %d fields for ANALYSIS record", len(data))
        except Exception as exc:
            logger.warning("Last-resort S3 read failed: %s", exc)

    if not data:
        return

    from backend.src.db import keys

    now = datetime.now(UTC).isoformat()
    key = keys.analysis(app_id, analysis_type, now)
    ddb_table.put_item(
        Item={
            **key.as_key(),
            **key.gsi1_attrs(),
            "app_id": app_id,
            "analysis_type": analysis_type,
            "run_id": run_id,
            "timestamp": now,
            "status": "completed",
            "s3_prefix": s3_prefix,
            "file_count": atx_output.get("file_count", 0),
            "data": data,
        }
    )
    logger.info("ANALYSIS# record created: %s/%s", analysis_type, run_id)


# ── Deterministic result records + META fields (specs 13/14) ───────────


def put_result_record(app_id: str, analysis_type: str, result: dict, status: str = "completed") -> str:
    """Write ANALYSIS#<type>#<ts> with a `result` map (ci-readiness, ci-fix). Returns the SK.

    Item shape is read by invoke_runtime's gate summaries: PK/SK, GSI1 (TYPE#ANALYSIS,
    <app_id>#<type>), app_id, analysis_type, status, result, updated_at.
    """
    from backend.src.db import keys

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = keys.analysis(app_id, analysis_type, now)
    ddb_table.put_item(Item=to_dynamo({
        **key.as_key(), **key.gsi1_attrs(),
        "app_id": app_id, "analysis_type": analysis_type, "status": status,
        "result": result, "updated_at": now,
    }))
    logger.info("ANALYSIS#%s record written for %s", analysis_type, app_id)
    return key.as_key()["SK"]


def update_meta_fields(app_id: str, set_fields: dict, remove_fields: list | tuple = (),
                       set_if_absent: dict | None = None) -> None:
    """SET / REMOVE META attributes; `set_if_absent` never overwrites an existing value."""
    parts_set, parts_remove, names, values = [], [], {}, {}
    i = 0
    for k, v in (set_fields or {}).items():
        if v is None:
            continue
        names[f"#f{i}"], values[f":v{i}"] = k, v
        parts_set.append(f"#f{i} = :v{i}")
        i += 1
    for k, v in (set_if_absent or {}).items():
        names[f"#f{i}"], values[f":v{i}"] = k, v
        parts_set.append(f"#f{i} = if_not_exists(#f{i}, :v{i})")
        i += 1
    for k in remove_fields or ():
        if k in (set_fields or {}):
            continue
        names[f"#f{i}"] = k
        parts_remove.append(f"#f{i}")
        i += 1
    if not parts_set and not parts_remove:
        return
    expr = ("SET " + ", ".join(parts_set) if parts_set else "") + (
        (" REMOVE " if parts_set else "REMOVE ") + ", ".join(parts_remove) if parts_remove else "")
    kwargs = {"Key": {"PK": f"APP#{app_id}", "SK": "META"}, "UpdateExpression": expr,
              "ExpressionAttributeNames": names}
    if values:
        kwargs["ExpressionAttributeValues"] = to_dynamo(values)
    ddb_table.update_item(**kwargs)
