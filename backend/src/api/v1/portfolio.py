"""
Portfolio API — Application-level CRUD.
The application is the aggregate root. All sub-entities (owners, machines,
software, repos, notes) are nested inside the application object.
The backend handles DynamoDB item splitting internally.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from backend.src.api.v1.workflow import RUN_DERIVED_META_FIELDS
from backend.src.auth.rbac import Role, ensure_role, require_role
from backend.src.config import settings
from backend.src.core.models import ApiResponse, error_response, generic_error_message
from backend.src.db.helpers import (
    call_with_credential_refresh,
    from_dynamo,
    get_table,
    is_credential_error,
    to_dynamo,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic Models (Application = aggregate root) ─────────────────────


class OwnerModel(BaseModel):
    model_config = {"extra": "ignore"}
    name: str
    email: str
    role: str = "app_owner"


# A linked repo must be clonable: ATX `git clone`s the url as-is, so a bare repo
# name ("serverless-java-frameworks-samples") or one with stray whitespace fails
# the clone and Assessment used to proceed with nothing analysed.
_CLONE_URL_RE = re.compile(r"^https://git-codecommit\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/v1/repos/[A-Za-z0-9._-]{1,100}$|^codecommit::[a-z0-9-]+://[A-Za-z0-9._@-]{1,200}$")


def repo_url_error(url: Any) -> str | None:
    """Return why `url` is not a clonable repo URL (None if ok or empty)."""
    u = str(url or "").strip()
    if not u or _CLONE_URL_RE.match(u):
        return None
    return (f"Repository URL '{u}' is not an AWS CodeCommit clone URL (AMF sources CodeCommit only) — use "
            "https://git-codecommit.<region>.amazonaws.com/v1/repos/<name> or codecommit::<region>://<name>.")


class RepoModel(BaseModel):
    model_config = {"extra": "ignore"}
    name: str = ""
    url: str = ""
    type: str = "application"
    language: str = ""
    branch: str = "main"

    @field_validator("name", "url", "branch", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("url")
    @classmethod
    def _clonable(cls, v: str) -> str:
        err = repo_url_error(v)
        if err:
            raise ValueError(err)
        return v


class SoftwareModel(BaseModel):
    model_config = {"extra": "ignore"}
    product: str = ""
    vendor: str = ""
    version: str = ""
    full_version: str = ""
    edition: str = ""
    category: str = "other"
    is_licensable: bool = False
    license_type: str = "open_source"
    license_count: int = 0
    eosl_date: str = ""
    eosl_status: str = "unknown"
    notes: str = ""


class MachineModel(BaseModel):
    model_config = {"extra": "ignore"}
    machine_id: str = ""
    hostname: str = ""
    ip_address: str = ""
    environment: str = "prod"
    device_category: str = "Server - VMware"
    host_server: str = ""
    cpu_cores: int = 0
    cpu_model: str = ""
    memory_gb: float = 0
    storage_gb: float = 0
    storage_type: str = "ssd"
    virtualization: str = "vmware"
    datacenter: str = ""
    location: str = ""
    network_zone: str = "internal"
    avg_cpu_utilization: float = 0
    avg_memory_utilization: float = 0
    status: str = "active"
    migration_target: str = ""
    notes: str = ""
    os: SoftwareModel | dict | None = None
    software: list[SoftwareModel] = Field(default_factory=list)


class NoteModel(BaseModel):
    text: str
    author: str = ""
    created_at: str = ""


class DocumentModel(BaseModel):
    model_config = {"extra": "ignore"}
    title: str
    url: str
    type: str = "other"


class ApplicationModel(BaseModel):
    """Full application object — the aggregate root for all portfolio data."""

    model_config = {"extra": "ignore"}

    app_id: str = ""
    app_name: str
    description: str = ""
    business_unit: str = ""
    tier: str = "tier_2"
    language: str = ""
    framework: str = ""
    architecture: str = "monolith"
    deployment_method: str = "manual"
    last_release_date: str = ""
    data_volume_gb: float = 0
    user_count: int = 0
    compliance: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    migration_strategy: str = ""
    wave_number: str | None = ""
    status: str = "discovered"
    notes: str = ""
    additional_notes: list[NoteModel] = Field(default_factory=list)
    owners: list[OwnerModel] = Field(default_factory=list)
    repos: list[RepoModel] = Field(default_factory=list)
    machines: list[MachineModel] = Field(default_factory=list)
    machine_ids: list[str] = Field(default_factory=list)
    machine_count: int = 0
    documents: list[DocumentModel] = Field(default_factory=list)
    # Requirements
    rpo: str = ""
    rto: str = ""
    uptime_sla: str = ""
    multi_region: str = "no"
    target_region: str = ""
    target_account: str = ""
    data_residency: str = ""
    blackout_periods: str = ""
    hard_deadline: str = ""
    budget_constraint: str = ""
    data_classification: str = ""
    encryption_required: str = "yes"


# ── Internal: Write full app to DynamoDB ───────────────────────────────


def _write_appto_dynamo(table, app_id: str, app: ApplicationModel, now: str) -> None:
    """Write the full application object as multiple DynamoDB items."""

    # Collect machine_ids — from machines list OR from direct machine_ids field
    machine_ids = []
    if app.machines:
        for m in app.machines:
            mid = m.machine_id or f"M-{uuid.uuid4().hex[:8].upper()}"
            machine_ids.append(mid)
    elif app.machine_ids:
        machine_ids = app.machine_ids

    # 1. App META
    table.put_item(
        Item=to_dynamo(
            {
                "PK": f"APP#{app_id}",
                "SK": "META",
                "GSI1PK": "TYPE#APP",
                "GSI1SK": app_id,
                "app_id": app_id,
                "app_name": app.app_name,
                "description": app.description,
                "business_unit": app.business_unit,
                "tier": app.tier,
                "language": app.language,
                "framework": app.framework,
                "architecture": app.architecture,
                "deployment_method": app.deployment_method,
                "last_release_date": app.last_release_date,
                "data_volume_gb": app.data_volume_gb,
                "user_count": app.user_count,
                "compliance": app.compliance,
                "integrations": app.integrations,
                "machine_ids": machine_ids,
                "migration_strategy": app.migration_strategy,
                "wave_number": app.wave_number or "",
                "status": app.status,
                "notes": app.notes,
                "documents": [d.model_dump() for d in app.documents],
                "rpo": app.rpo,
                "rto": app.rto,
                "uptime_sla": app.uptime_sla,
                "multi_region": app.multi_region,
                "target_region": app.target_region,
                "target_account": app.target_account,
                "data_residency": app.data_residency,
                "blackout_periods": app.blackout_periods,
                "hard_deadline": app.hard_deadline,
                "budget_constraint": app.budget_constraint,
                "data_classification": app.data_classification,
                "encryption_required": app.encryption_required,
                "owner_count": len(app.owners),
                "repo_count": len(app.repos),
                "machine_count": len(machine_ids),
                "updated_at": now,
            }
        ),
        ConditionExpression="attribute_not_exists(PK)",
    )

    # 2. Owners
    for owner in app.owners:
        key = owner.email.split("@")[0].replace(".", "-")
        table.put_item(
            Item={
                "PK": f"APP#{app_id}",
                "SK": f"OWNER#{key}",
                "name": owner.name,
                "email": owner.email,
                "role": owner.role,
            }
        )

    # 3. Repos
    for repo in app.repos:
        table.put_item(
            Item={
                "PK": f"APP#{app_id}",
                "SK": f"REPO#{repo.name}",
                "name": repo.name,
                "url": repo.url,
                "type": repo.type,
                "language": repo.language,
                "branch": repo.branch,
            }
        )

    # 4. Additional notes
    for note in app.additional_notes:
        ts = note.created_at or now
        table.put_item(
            Item={
                "PK": f"APP#{app_id}",
                "SK": f"NOTE#{ts}",
                "text": note.text,
                "author": note.author,
                "created_at": ts,
            }
        )

    # 5. Machines + software
    for i, m in enumerate(app.machines):
        mid = machine_ids[i]
        table.put_item(
            Item=to_dynamo(
                {
                    "PK": f"MACHINE#{mid}",
                    "SK": "META",
                    "GSI1PK": "TYPE#MACHINE",
                    "GSI1SK": mid,
                    "machine_id": mid,
                    "hostname": m.hostname,
                    "ip_address": m.ip_address,
                    "environment": m.environment,
                    "device_category": m.device_category,
                    "host_server": m.host_server,
                    "app_ids": [app_id],
                    "cpu_cores": m.cpu_cores,
                    "cpu_model": m.cpu_model,
                    "memory_gb": m.memory_gb,
                    "storage_gb": m.storage_gb,
                    "storage_type": m.storage_type,
                    "virtualization": m.virtualization,
                    "datacenter": m.datacenter,
                    "location": m.location,
                    "network_zone": m.network_zone,
                    "avg_cpu_utilization": m.avg_cpu_utilization,
                    "avg_memory_utilization": m.avg_memory_utilization,
                    "status": m.status,
                    "migration_target": m.migration_target,
                    "notes": m.notes,
                    "discovery_date": now,
                }
            )
        )
        if m.os:
            table.put_item(
                Item=to_dynamo(
                    {
                        "PK": f"MACHINE#{mid}",
                        "SK": "SW#os",
                        **m.os.model_dump(),
                    }
                )
            )
        for sw in m.software:
            sw_key = sw.product.lower().replace(" ", "-")[:40]
            table.put_item(
                Item=to_dynamo(
                    {
                        "PK": f"MACHINE#{mid}",
                        "SK": f"SW#{sw_key}",
                        **sw.model_dump(),
                    }
                )
            )


# Single source lives in the db layer (so the worker can reuse it too).
from backend.src.db.repository import read_full_app as _read_full_app  # noqa: E402,F401

# ── API Endpoints ──────────────────────────────────────────────────────


@router.get("/apps")
def list_apps(limit: int = Query(200, ge=1, le=500)):
    """List applications, retrying once after local AWS credential rotation."""

    def _load_apps():
        table = get_table()
        all_items = []
        query_kwargs = {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("GSI1PK").eq("TYPE#APP"),
        }
        while len(all_items) < limit:
            query_kwargs["Limit"] = min(limit - len(all_items), 500)
            resp = table.query(**query_kwargs)
            all_items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return from_dynamo(all_items)

    try:
        items = call_with_credential_refresh(_load_apps)
        return ApiResponse(data=items, meta={"count": len(items)})
    except Exception as exc:
        logger.error("list_apps: %s", exc)
        if is_credential_error(exc):
            return error_response(
                "AWS credentials expired. Refresh the configured AWS session and retry.",
                code="AWS_CREDENTIALS_EXPIRED",
                status_code=503,
            )
        return error_response(
            "Portfolio applications are temporarily unavailable.",
            code="PORTFOLIO_UNAVAILABLE",
            status_code=503,
        )


@router.get("/apps/{app_id}")
def get_app(app_id: str):
    """Get full application with owners, repos, notes, machines, software, and completeness."""
    try:
        table = get_table()
        app = _read_full_app(table, app_id)
        if not app:
            return ApiResponse(status="error", data={"message": f"App {app_id} not found"})

        from backend.src.core.completeness import calculate_completeness

        app["completeness_detail"] = calculate_completeness(app)
        app["completeness"] = app["completeness_detail"]["percentage"]

        return ApiResponse(data=app)
    except Exception as exc:
        logger.error("get_app: %s", exc)
        return ApiResponse(status="error", data={"message": generic_error_message(exc)})


@router.post("/apps", dependencies=[require_role(Role.ENGINEER)])
def create_app(app: ApplicationModel):
    """Create a new application with all sub-entities."""
    try:
        table = get_table()
        now = datetime.now(UTC).isoformat()
        app_id = app.app_id or f"APP-{uuid.uuid4().hex[:8].upper()}"
        try:
            _write_appto_dynamo(table, app_id, app, now)
        except ClientError as exc:
            # POST creates only: an existing app (and its run state) is never overwritten.
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return error_response(f"App {app_id} already exists", code="CONFLICT")
            raise
        return ApiResponse(data={"app_id": app_id, "created_at": now})
    except Exception as exc:
        logger.error("create_app: %s", exc)
        return ApiResponse(status="error", data={"message": generic_error_message(exc)})


# ── Privileged META fields ─────────────────────────
# The assessment-gate inputs are human inputs (the assessment approval PUTs them;
# validate_assessment_inputs checks them below), so ENGINEER may set them even
# though a new run resets them. Deploying them is gated separately: the deploy-gate
# approval needs ADMIN and the worker only deploys into DEPLOY_ALLOWED_ACCOUNTS.
ASSESSMENT_GATE_INPUTS = frozenset({
    "target_compute", "target_database", "cicd_target", "target_account", "vpc_cidr",
    "ami_id", "db_instance_class",
})
# Written only by the server (workflow, gates, workers, tools). A client value is
# refused with 400 naming the field. completeness_detail is not here: GET derives it
# and the edit modal echoes it back, so it is dropped silently (_EXCLUDED_KEYS).
SERVER_ONLY_FIELDS = frozenset(
    {"PK", "SK", "GSI1PK", "GSI1SK", "status", "current_step", "current_phase",
     "deploy_approved_by", "deploy_approved_commit", "deploy_approved_at", "exec_head_commit"}
    | (set(RUN_DERIVED_META_FIELDS) - ASSESSMENT_GATE_INPUTS - {"completeness_detail"})
)
# Reviewed per-app opt-ins with no server writer: a human sets them, but only an
# Admin — expose_publicly waives the network-exposure policy (core/exposure_policy),
# validation_write_tests lets the validation probe write to the deployed app.
ADMIN_ONLY_FIELDS = frozenset({"expose_publicly", "validation_write_tests"})


def _request_user(request: Request | None):
    """The authenticated user (None for a direct, non-HTTP call — fails any Admin check)."""
    return getattr(getattr(request, "state", None), "user", None)


def _same_value(a: Any, b: Any) -> bool:
    import json as _json

    try:
        return _json.dumps(a, sort_keys=True, default=str) == _json.dumps(b, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return a == b


def privileged_field_violation(fields: dict, existing: dict, user) -> str | None:
    """Why `fields` may not be written by `user` (None if they may), and drop no-op echoes.

    A privileged field whose value equals the stored one is a no-op echo (the edit
    modal sends back the whole GET record) and is removed from `fields`; a changed
    server-only field is refused; a changed Admin-only field needs ADMIN (raises 403).
    """
    for key in list(fields):
        is_server = key in SERVER_ONLY_FIELDS or str(key).startswith("GSI")
        if not is_server and key not in ADMIN_ONLY_FIELDS:
            continue
        if key in existing and _same_value(fields[key], existing[key]):
            fields.pop(key)
            continue
        if is_server:
            return f"Field '{key}' is server-managed and can't be set by a client"
        ensure_role(user, Role.ADMIN)
    return None



COVERAGE_REASON_MAX_CHARS = 500


def _normalise_coverage_gate(body: dict) -> str | None:
    """Validate/normalise coverage_threshold (int 0-100, "" / None = platform default) and its reason."""
    if "coverage_threshold" in body:
        raw = body["coverage_threshold"]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            body["coverage_threshold"] = None
        else:
            try:
                value = int(str(raw).strip())
            except ValueError:
                return "coverage_threshold must be a whole number from 0 to 100"
            if not 0 <= value <= 100:
                return "coverage_threshold must be a whole number from 0 to 100"
            body["coverage_threshold"] = value
    if "coverage_threshold_reason" in body:
        reason = str(body.get("coverage_threshold_reason") or "").strip()
        if len(reason) > COVERAGE_REASON_MAX_CHARS:
            return f"coverage_threshold_reason must be at most {COVERAGE_REASON_MAX_CHARS} characters"
        body["coverage_threshold_reason"] = reason
    if body.get("coverage_threshold") == 0 and not body.get("coverage_threshold_reason"):
        return "a coverage_threshold of 0 disables the gate and needs coverage_threshold_reason"
    return None


_DEP_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _validate_depends_on(table, app_id: str, value) -> tuple[list[str], str]:
    """Normalise META depends_on: app ids this app calls/needs.

    Returns (deduplicated list, error). Every target must be a different, existing app."""
    from backend.src.config import settings

    if value is None:
        return [], ""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return [], "depends_on must be a list of app ids"
    deps: list[str] = []
    for raw in value:
        dep = raw.strip()
        if not _DEP_APP_ID_RE.fullmatch(dep):
            return [], f"invalid app id in depends_on: {dep[:64]!r}"
        if dep == app_id:
            return [], "an app cannot depend on itself"
        if dep not in deps:
            deps.append(dep)
    if len(deps) > settings.app_dependencies_max:
        return [], f"depends_on allows at most {settings.app_dependencies_max} apps"
    missing = [d for d in deps if not table.get_item(Key={"PK": f"APP#{d}", "SK": "META"},
                                                     ProjectionExpression="PK").get("Item")]
    if missing:
        return [], "unknown app id(s) in depends_on: " + ", ".join(missing)
    return deps, ""


@router.put("/apps/{app_id}", dependencies=[require_role(Role.ENGINEER)])
def update_app(app_id: str, body: dict, request: Request = None):
    """Update application — merges the given fields into the existing DynamoDB META record.

    Never creates an app (404 when META is missing). Server-managed fields are refused
    (400 naming the field) and Admin-only fields need ADMIN (403), see
    privileged_field_violation.
    """
    table = get_table()
    existing_meta = from_dynamo(table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item") or {})
    if not existing_meta:
        return error_response(f"App {app_id} not found", code="NOT_FOUND")
    body = dict(body)
    violation = privileged_field_violation(body, existing_meta, _request_user(request))
    if violation:
        raise HTTPException(status_code=400, detail=violation)
    try:
        now = datetime.now(UTC).isoformat()

        # Strategy-driven input validation. Reject writes that contradict the 7R
        # strategy (e.g. target_compute=ecs_fargate while strategy=rehost, or a
        # non-12-digit AWS account ID). Single source of truth is
        # backend.src.core.strategy_options; the frontend hides invalid dropdown
        # options but we never trust the client.
        from backend.src.core.strategy_options import (
            cicd_options_for,
            compute_options_for,
            database_options_for,
            validate_assessment_inputs,
        )
        checked_fields = {
            "target_compute", "target_database", "cicd_target",
            "target_account", "vpc_cidr", "transformation_steps",
            "ami_id", "db_instance_class",
        }
        repo_errs = [e for r in (body.get("repos") or []) if isinstance(r, dict)
                     if (e := repo_url_error(r.get("url")))]
        if repo_errs:
            return error_response("Validation failed: " + "; ".join(repo_errs), code="INVALID_REPO_URL",
                                  details={"errors": repo_errs})

        # CI coverage gate (per app; empty = the platform default COVERAGE_THRESHOLD_DEFAULT).
        cov_err = _normalise_coverage_gate(body)
        if cov_err:
            return error_response(f"Validation failed: {cov_err}", code="INVALID_COVERAGE_GATE")

        if "depends_on" in body:
            deps, dep_err = _validate_depends_on(table, app_id, body.get("depends_on"))
            if dep_err:
                return error_response(f"Validation failed: {dep_err}", code="INVALID_DEPENDENCIES")
            body["depends_on"] = deps

        if any(k in body for k in checked_fields):
            existing = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
            merged = {**{k: existing.get(k) for k in checked_fields}, **{k: v for k, v in body.items() if k in checked_fields}}
            strategy = body.get("migration_strategy") or existing.get("migration_strategy") or ""
            # The assessment approval always sends the sizing keys; other edits
            # only get format checks (Execution's pre-hook enforces presence).
            require_sizing = "ami_id" in body or "db_instance_class" in body
            errs = validate_assessment_inputs(strategy, merged, require_sizing=require_sizing)
            if errs:
                return error_response(
                    "Validation failed: " + "; ".join(e["message"] for e in errs),
                    code="INVALID_STRATEGY_INPUTS",
                    details={"errors": errs, "strategy": strategy},
                )

        # A strategy change alone (e.g. the Discovery override) makes earlier
        # selections invalid for the new strategy: clear those so Design/Execution
        # never build a compute/DB/pipeline the strategy forbids — the assessment
        # gate collects them again.
        stale: list[str] = []
        new_strategy = str(body.get("migration_strategy") or "").lower()
        if new_strategy and not any(k in body for k in ("target_compute", "target_database", "cicd_target")):
            existing = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"}).get("Item", {}) or {}
            for field, options_for in (("target_compute", compute_options_for),
                                       ("target_database", database_options_for),
                                       ("cicd_target", cicd_options_for)):
                value = existing.get(field)
                if value and value not in options_for(new_strategy):
                    stale.append(field)

        # Remove temp form fields, internal keys, and non-META data
        _EXCLUDED_KEYS = {
            "PK",
            "SK",
            "GSI1PK",
            "GSI1SK",
            "machines",
            # Aggregates fetched by read_full_app (OWNER# / REPO# / NOTE# sibling
            # items). Writing them back to META pollutes META with duplicate rows
            # that still carry their own PK/SK. These must only be written as their
            # own sibling items via the enrich path, never on META.
            "owners",
            "repos",
            "additional_notes",
            "notes_history",
            "workflow",
            "pending",
            "isEnriched",
            "completeness",
            # Derived by GET /apps/{id}; the edit modal sends it back. Persisting it
            # on META once let the strategy scan match its own derived text.
            "completeness_detail",
            "ready_for_assessment",
        }
        clean_data = {k: v for k, v in body.items() if not k.startswith("_") and k not in _EXCLUDED_KEYS}
        clean_data["updated_at"] = now
        clean_data["app_id"] = app_id

        # Ensure machine_count stays in sync
        if "machine_ids" in clean_data:
            clean_data["machine_count"] = len(clean_data["machine_ids"])

        # Build update expression from all provided fields
        expr_parts = []
        expr_values = {}
        expr_names = {}
        for i, (k, v) in enumerate(clean_data.items()):
            safe_key = f"#k{i}"
            expr_parts.append(f"{safe_key} = :v{i}")
            expr_values[f":v{i}"] = v
            expr_names[safe_key] = k

        update_expr = "SET " + ", ".join(expr_parts)
        if stale:
            for j, f in enumerate(stale):
                expr_names[f"#s{j}"] = f
            update_expr += " REMOVE " + ", ".join(f"#s{j}" for j in range(len(stale)))
        try:
            # PUT updates, never creates: the app may have been deleted since the read.
            table.update_item(
                Key={"PK": f"APP#{app_id}", "SK": "META"},
                UpdateExpression=update_expr,
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=to_dynamo(expr_values),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return error_response(f"App {app_id} not found", code="NOT_FOUND")
            raise

        # When the body carries owner/repo aggregates (edit modal sends the whole
        # record), persist them as their own OWNER#/REPO# sibling items — not on
        # META. This preserves the editability of those lists while keeping META
        # free of PK/SK pollution from read_full_app aggregates.
        _INFRA = {"PK", "SK", "GSI1PK", "GSI1SK"}
        owners_in = body.get("owners")
        if isinstance(owners_in, list):
            for owner in owners_in:
                if not isinstance(owner, dict):
                    continue
                email = str(owner.get("email") or "").strip()
                if not email:
                    continue
                clean = {k: v for k, v in owner.items() if k not in _INFRA}
                clean.update({"PK": f"APP#{app_id}", "SK": f"OWNER#{email}"})
                table.put_item(Item=to_dynamo(clean))

        repos_in = body.get("repos")
        if isinstance(repos_in, list):
            kept = set()
            for repo in repos_in:
                if not isinstance(repo, dict):
                    continue
                rid = str(repo.get("name") or (repo.get("url") or "").strip().rstrip("/").split("/")[-1]).strip()
                if not rid:
                    continue
                clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in repo.items() if k not in _INFRA}
                clean.update({"PK": f"APP#{app_id}", "SK": f"REPO#{rid}", "name": rid})
                table.put_item(Item=to_dynamo(clean))
                kept.add(f"REPO#{rid}")
            # The list is the full set (the edit modal sends every linked repo):
            # a repo removed in the UI used to stay linked and ATX still cloned it.
            existing = table.query(
                KeyConditionExpression=Key("PK").eq(f"APP#{app_id}") & Key("SK").begins_with("REPO#"),
                ProjectionExpression="SK",
            ).get("Items", [])
            for item in existing:
                if item["SK"] not in kept:
                    table.delete_item(Key={"PK": f"APP#{app_id}", "SK": item["SK"]})
            table.update_item(
                Key={"PK": f"APP#{app_id}", "SK": "META"},
                UpdateExpression="SET repo_count = :c",
                ExpressionAttributeValues={":c": len(kept)},
            )

        return ApiResponse(data={"app_id": app_id, "updated_at": now})
    except Exception as exc:
        logger.error("update_app: %s", exc)
        return ApiResponse(status="error", data={"message": generic_error_message(exc)})


# ── Bulk Enrichment ────────────────────────────────────────────────────


class EnrichAppItem(BaseModel):
    """Enrichment data (an L2 record) for a single app. Only provided fields are updated.

    Every field of the L2 record is persisted onto META. The known L2 fields are
    declared below with types so obviously malformed uploads are rejected, but
    ``extra="allow"`` keeps any field this model does not list — an earlier
    version used ``extra="ignore"`` and silently dropped ~20 L2 fields (rpo, rto,
    target_region, current_compute, dr_availability, …) that completeness, the
    strategy rules and the assessment all read. Types are deliberately broad
    (``str | list`` etc.): one over-strict field would 422 the whole upload.
    """

    model_config = {"extra": "allow"}
    app_id: str
    # Identity / business
    app_name: str | None = None
    description: str | None = None
    business_unit: str | None = None
    tier: str | None = None
    user_count: int | None = None
    data_volume_gb: float | None = None
    owner_id: str | None = None
    tech_lead_id: str | None = None
    # Technology
    language: str | None = None
    framework: str | None = None
    architecture: str | None = None
    deployment_method: str | None = None
    last_release_date: str | None = None
    runtime: dict | None = None
    database: dict | None = None
    # Current environment (as-is) and target preferences
    current_compute: str | None = None
    current_region: str | None = None
    containerization: str | None = None
    # An L2 cicd_target is a PREFERENCE (e.g. "AWS CodePipeline") — confirmed at
    # assessment. Persisted verbatim as cicd_target_preference; see bulk_enrich.
    cicd_target: str | None = None
    target_region: str | None = None
    target_db_engine: str | None = None
    db_migration_method: str | None = None
    licensing: str | None = None
    # Compliance / requirements / DR
    compliance: list[str] | None = None
    data_classification: str | None = None
    rpo: str | None = None
    rto: str | None = None
    uptime_sla: str | None = None
    multi_region: str | bool | None = None
    dr_regions: list[str] | None = None
    dr_availability: dict | None = None
    # Dependencies / operations
    integrations: list[str] | None = None
    dependencies: dict | None = None
    dependencies_upstream: list[str] | None = None
    dependencies_downstream: list[str] | None = None
    startup_shutdown_procedures: str | None = None
    change_freeze_windows: list[str] | str | None = None
    blackout_periods: list[str] | str | None = None
    hard_deadline: str | None = None
    constraints: dict | None = None
    change_history: dict | None = None
    performance: dict | None = None
    current_cost: dict | None = None
    # Machines — when supplied, the L2 list REPLACES META.machine_ids.
    machine_ids: list[str] | None = None
    machine_count: int | None = None
    # Strategy / readiness / free text
    migration_strategy: str | None = None
    notes: str | None = None
    risks: list[str] | None = None
    business_readiness: dict | None = None
    team_readiness: dict | None = None
    # Aggregates written as their own OWNER#/REPO# sibling items, never on META.
    owners: list[OwnerModel] = Field(default_factory=list)
    repos: list[RepoModel] = Field(default_factory=list)


# Keys an L2 record must never write onto META: the table's key/index attributes,
# the aggregates that live as sibling items (or MACHINE# items), and values this
# service derives itself (writing a stale copy back would poison later reads —
# completeness_detail is what once steered the strategy scan to 'repurchase').
_ENRICH_EXCLUDED_KEYS = {
    "PK", "SK", "GSI1PK", "GSI1SK", "app_id",
    "owners", "repos", "machines", "additional_notes", "notes_history",
    "machine_count",  # derived from machine_ids below
    "completeness", "completeness_detail", "ready_for_assessment", "isEnriched",
    "workflow", "pending",
}

# Run-scoped META fields an L2 record may carry a PREFERENCE for. A new run
# clears these (workflow._reset_app_for_new_run) because the assessment gate
# re-confirms them; the L2 preference is kept in META.l2_preferences so the reset
# can restore it instead of losing the uploaded value.
# ami_id / db_instance_class too: EC2 / RDS sizing from L2 must survive a new run, not be re-entered.
L2_PREFERENCE_FIELDS = ("cicd_target", "target_compute", "target_database", "target_account", "vpc_cidr",
                        "ami_id", "db_instance_class")

# L2 free-text CI/CD preference → the strategy_options enum. Anything unrecognised
# is kept only as the verbatim preference (never written as an invalid enum, which
# would make every later PUT /apps/{id} fail validate_assessment_inputs).
_CICD_ALIASES = {
    "codepipeline": "codepipeline", "aws codepipeline": "codepipeline", "aws code pipeline": "codepipeline",
}

# dr_availability is an object in L2; its members back the flat requirement
# fields completeness and the assessment read. Fill the flat field from the
# object only when the record did not supply the flat field itself.
_DR_FLAT_FIELDS = ("rpo", "rto", "uptime_sla", "multi_region", "dr_regions")


def _is_provided(value: Any) -> bool:
    """'Only provided fields are updated': None / blank / empty collection = not provided."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


def _enrich_meta_fields(item: EnrichAppItem) -> dict[str, Any]:
    """Every provided L2 field destined for META (declared + extra), normalised."""
    raw = item.model_dump(exclude={"owners", "repos"})
    raw.update(item.model_extra or {})
    fields = {
        k: v for k, v in raw.items()
        if k not in _ENRICH_EXCLUDED_KEYS and not str(k).startswith("_") and _is_provided(v)
    }

    dr = fields.get("dr_availability")
    if isinstance(dr, dict):
        for f in _DR_FLAT_FIELDS:
            if f not in fields and _is_provided(dr.get(f)):
                fields[f] = dr[f]

    # L2 machine list wins over the ADS auto-grouping: replace, keep the count in sync.
    if "machine_ids" in fields:
        ids = list(dict.fromkeys(str(m).strip() for m in fields["machine_ids"] if str(m).strip()))
        fields["machine_ids"] = ids
        fields["machine_count"] = len(ids)

    if "cicd_target" in fields:
        pref = str(fields.pop("cicd_target")).strip()
        fields["cicd_target_preference"] = pref
        normalised = _CICD_ALIASES.get(pref.lower())
        if normalised:
            fields["cicd_target"] = normalised

    prefs = {f: fields[f] for f in L2_PREFERENCE_FIELDS if f in fields}
    if prefs:
        fields["l2_preferences"] = prefs
    return fields


@router.post("/apps/enrich", dependencies=[require_role(Role.ENGINEER)])
def bulk_enrich(items: list[EnrichAppItem], request: Request = None):
    """Bulk enrich existing applications with owners, repos, business unit, tier.

    Use this before starting the workflow. Upload a JSON array:
    [
      {
        "app_id": "APP-AL2023",
        "business_unit": "media",
        "tier": "tier_1",
        "owners": [{"name": "John Doe", "email": "john@example.com", "role": "app_owner"}],
        "repos": [{"name": "my-app", "url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/my-app", "branch": "main"}]
      }
    ]
    """
    if len(items) > settings.bulk_enrich_max:
        return ApiResponse(status="error", data={"message": f"Max {settings.bulk_enrich_max} apps per request"})
    try:
        table = get_table()
        now = datetime.now(UTC).isoformat()
        results = {"updated": 0, "not_found": [], "errors": []}

        for item in items:
            # Check app exists — if not, create it
            resp = table.get_item(Key={"PK": f"APP#{item.app_id}", "SK": "META"})
            app_exists = "Item" in resp

            if not app_exists:
                # Skip — only enrich apps that were created by L1 discovery
                results["not_found"].append(item.app_id)
                logger.info("Skipped enrichment for %s — not found in L1 discovery", item.app_id)
                continue

            try:
                # Update META fields — every provided L2 field (see _enrich_meta_fields).
                # All attribute names go through ExpressionAttributeNames, so DynamoDB
                # reserved words (language, database, constraints, …) need no special case.
                fields = _enrich_meta_fields(item)
                # Same privileged-field guard as PUT /apps/{id}.
                violation = privileged_field_violation(
                    fields, from_dynamo(resp["Item"]), _request_user(request))
                if violation:
                    results["errors"].append({"app_id": item.app_id, "error": violation})
                    continue
                if fields:
                    fields["enriched_at"] = now
                    names = {f"#k{i}": k for i, k in enumerate(fields)}
                    values = {f":v{i}": v for i, v in enumerate(fields.values())}
                    table.update_item(
                        Key={"PK": f"APP#{item.app_id}", "SK": "META"},
                        UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(fields))),
                        ExpressionAttributeNames=names,
                        ExpressionAttributeValues=to_dynamo(values),
                    )

                # Add owners + repos in batches (25 items per BatchWriteItem). Keyed by
                # SK, so a repeated owner/repo in one item keeps the last one (as put_item did).
                children: dict[str, dict] = {}
                for owner in item.owners:
                    children[f"OWNER#{owner.email}"] = {
                        "name": owner.name,
                        "email": owner.email,
                        "role": owner.role,
                        "added_at": now,
                    }
                for repo in item.repos:
                    repo_id = repo.name or repo.url.rstrip("/").split("/")[-1]
                    children[f"REPO#{repo_id}"] = {
                        "name": repo_id,
                        "url": repo.url,
                        "branch": repo.branch,
                        "language": repo.language,
                        "type": repo.type,
                        "added_at": now,
                    }
                if children:
                    with table.batch_writer() as batch:
                        for sk, attrs in children.items():
                            batch.put_item(Item=to_dynamo({"PK": f"APP#{item.app_id}", "SK": sk, **attrs}))

                # Update repo count on META
                if item.repos:
                    repos_resp = table.query(
                        KeyConditionExpression=Key("PK").eq(f"APP#{item.app_id}") & Key("SK").begins_with("REPO#"),
                        Select="COUNT",
                    )
                    table.update_item(
                        Key={"PK": f"APP#{item.app_id}", "SK": "META"},
                        UpdateExpression="SET repo_count = :rc",
                        ExpressionAttributeValues={":rc": repos_resp.get("Count", 0)},
                    )

                results["updated"] += 1

            except HTTPException as e:  # an Admin-only field from a non-Admin
                results["errors"].append({"app_id": item.app_id, "error": e.detail})
            except Exception as e:
                results["errors"].append(
                    {"app_id": item.app_id, "error": generic_error_message(e, "ENRICH_FAILED", item.app_id)})

        return ApiResponse(data=results)

    except Exception as exc:
        logger.error("bulk_enrich: %s", exc)
        return ApiResponse(status="error", data={"message": generic_error_message(exc)})


@router.get("/machines")
def list_machines(limit: int = Query(500, ge=1, le=5000)):
    """List machines, retrying once after local AWS credential rotation."""

    def _load_machines():
        table = get_table()
        all_items = []
        query_kwargs = {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("GSI1PK").eq("TYPE#MACHINE"),
        }
        while len(all_items) < limit:
            query_kwargs["Limit"] = min(limit - len(all_items), 500)
            resp = table.query(**query_kwargs)
            all_items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return from_dynamo(all_items)

    try:
        items = call_with_credential_refresh(_load_machines)
        return ApiResponse(data=items, meta={"count": len(items)})
    except Exception as exc:
        logger.error("list_machines: %s", exc)
        if is_credential_error(exc):
            return error_response(
                "AWS credentials expired. Refresh the configured AWS session and retry.",
                code="AWS_CREDENTIALS_EXPIRED",
                status_code=503,
            )
        return error_response(
            "Portfolio machines are temporarily unavailable.",
            code="PORTFOLIO_UNAVAILABLE",
            status_code=503,
        )
