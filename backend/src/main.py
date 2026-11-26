"""
FastAPI application entry point.
Run with: uvicorn backend.src.main:app --reload --host 127.0.0.1 --port 8000  (scripts/start.sh)
"""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from backend.src.auth.middleware import AuthMiddleware
from backend.src.auth.startup_guard import assert_auth_config_safe
from backend.src.config import settings

# ── Structured logging ─────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger("migration_framework")


# ── Lifespan ───────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources on startup, clean up on shutdown."""
    from backend.src.api.deps import get_db

    # Fail closed: never serve hosted mode with DEV_AUTH or without Cognito (raises).
    assert_auth_config_safe(settings)

    logger.info("Initializing DynamoDB client")
    get_db()

    # Recover any workbench plans stuck in "running" from a previous run
    try:
        from backend.src.api.v1.workbench import recover_stuck_plans

        logger.info("Running startup recovery for stuck plans")
        recover_stuck_plans()
    except Exception as exc:
        logger.error("Workbench startup recovery failed: %s", exc)

    # Recover workflows where SFN completed but DynamoDB still shows RUNNING
    try:
        logger.info("Running startup recovery for stuck workflows")
        _recover_stuck_workflows()
    except Exception as exc:
        logger.error("Workflow startup recovery failed: %s", exc)

    yield
    logger.info("Shutting down")


def _recover_stuck_workflows():
    """Refresh cached WORKFLOW#ACTIVE status against Step Functions.

    Best-effort cache warm-up on boot. SFN is the source of truth; get_active_workflows
    also reconciles lazily on read, so this is not required for correctness — it just
    avoids a first-read discrepancy. Uses a bounded GSI1 Query on the TYPE#WORKFLOW
    partition (never a full-table Scan).
    """
    import boto3
    from boto3.dynamodb.conditions import Key

    from backend.src.db.helpers import get_table
    from backend.src.db.keys import TYPE_WORKFLOW

    sfn = boto3.client("stepfunctions", region_name=settings.aws_region)
    table = get_table()

    fixed = 0
    query_kwargs = {
        "IndexName": "GSI1",
        "KeyConditionExpression": Key("GSI1PK").eq(TYPE_WORKFLOW),
        "FilterExpression": "#s = :running",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":running": "RUNNING"},
    }
    while True:
        resp = table.query(**query_kwargs)
        for item in resp.get("Items", []):
            exec_arn = item.get("execution_arn", "")
            if not exec_arn:
                continue
            try:
                sfn_status = sfn.describe_execution(executionArn=exec_arn)["status"]
                if sfn_status != "RUNNING":
                    new_status = "COMPLETED" if sfn_status == "SUCCEEDED" else "FAILED"
                    table.update_item(
                        Key={"PK": item["PK"], "SK": item["SK"]},
                        UpdateExpression="SET #s = :s",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":s": new_status},
                    )
                    fixed += 1
            except Exception as _err:
                logger.debug("best-effort step ignored: %s", _err)
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    if fixed:
        logger.info("Fixed %d stuck workflow(s)", fixed)


# ── App ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AMF — Agentic Modernization Framework",
    version="0.1.0",
    # API docs only for local dev (DEV_AUTH): hosted mode doesn't publish its
    # schema.
    docs_url="/docs" if settings.dev_auth else None,
    redoc_url="/redoc" if settings.dev_auth else None,
    openapi_url="/openapi.json" if settings.dev_auth else None,
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request ID middleware ──────────────────────────────────────────────


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response: Response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ── Security headers ───────────────────────────────────────────────────
# Every response: nosniff + no referrer. HTML responses also get
# a no-script CSP with frame-ancestors 'none' unless the route set its own (report
# HTML sets a stricter one); the dev-only Swagger/ReDoc pages load their JS from a
# CDN, so they are left alone.

_HTML_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})


async def security_headers_middleware(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html") and request.url.path not in _DOCS_PATHS:
        response.headers.setdefault("Content-Security-Policy", _HTML_CSP)
    return response


# ── Validation error logging ───────────────────────────────────────────

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Log only where and what kind of error: never the submitted values (exc.errors() carries the input).
    errors = [{"loc": list(e.get("loc", ())), "type": e.get("type", "")} for e in exc.errors()]
    logger.error("Validation error on %s %s: %s", request.method, request.url.path, errors)
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]})


# ── Auth middleware ────────────────────────────────────────────────────

app.add_middleware(AuthMiddleware)
# Registered after AuthMiddleware so it is outermost: 401s get the headers too.
app.middleware("http")(security_headers_middleware)


# ── Health ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Process liveness only. Use /health/ready for AWS dependency readiness."""
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    """Verify the AWS data plane and workflow system used by the UI."""
    import boto3

    from backend.src.db.helpers import call_with_credential_refresh, is_credential_error

    def _check_dependencies():
        dynamodb = boto3.client("dynamodb", region_name=settings.aws_region)
        table_status = dynamodb.describe_table(TableName=settings.dynamodb_table)["Table"]["TableStatus"]
        state_machine_arn = settings.get_sfn_state_machine_arn()
        state_machine_status = boto3.client(
            "stepfunctions", region_name=settings.aws_region
        ).describe_state_machine(stateMachineArn=state_machine_arn)["status"]
        return {
            "dynamodb": table_status,
            "step_functions": state_machine_status,
            "state_machine": settings.sfn_state_machine_name,
        }

    try:
        dependencies = call_with_credential_refresh(_check_dependencies)
        return {"status": "ready", "dependencies": dependencies}
    except Exception as exc:
        credential_error = is_credential_error(exc)
        logger.error("Readiness check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "error": {
                    "code": "AWS_CREDENTIALS_EXPIRED" if credential_error else "AWS_DEPENDENCY_UNAVAILABLE",
                    "message": (
                        "AWS credentials expired. Refresh the configured AWS session."
                        if credential_error
                        else "An AWS dependency required by AMF is unavailable."
                    ),
                },
            },
        )


# ── Router mounts ──────────────────────────────────────────────────────
# Every /api/v1 router needs an authenticated user with at least VIEWER; mutating
# routes add ENGINEER/ADMIN themselves (tests/unit/test_rbac_routes.py).

from backend.src.auth.rbac import Role, require_role

_AUTHENTICATED = [require_role(Role.VIEWER)]

from backend.src.api.v1 import (
    analysis,
    chat,
    design,
    discovery,
    portfolio,
    waves,
    workbench,
    workflow,
)

# Discovery & Enrichment
app.include_router(discovery.router, prefix="/api/v1/discovery", tags=["Discovery"], dependencies=_AUTHENTICATED)
app.include_router(portfolio.router, prefix="/api/v1/portfolio", tags=["Portfolio"], dependencies=_AUTHENTICATED)

# Analysis
app.include_router(analysis.router, prefix="/api/v1/analysis", tags=["Analysis"], dependencies=_AUTHENTICATED)

# Planning & Design
app.include_router(waves.router, prefix="/api/v1/waves", tags=["Waves"], dependencies=_AUTHENTICATED)
app.include_router(design.router, prefix="/api/v1/design", tags=["Design"], dependencies=_AUTHENTICATED)

# Execution
app.include_router(workbench.router, prefix="/api/v1/workbench", tags=["Workbench"], dependencies=_AUTHENTICATED)

# Workflow Orchestration (Step Functions)
app.include_router(workflow.router, prefix="/api/v1/workflow", tags=["Workflow"], dependencies=_AUTHENTICATED)

# Supporting
app.include_router(chat.router, prefix="/api/v1/chat", tags=["Chat"], dependencies=_AUTHENTICATED)

# System / capabilities (feature flags for the frontend)
from backend.src.api.v1 import system

app.include_router(system.router, prefix="/api/v1/system", tags=["System"], dependencies=_AUTHENTICATED)

# Portfolio / program-level views (dashboard, waves, at-risk, metrics)
from backend.src.api.v1 import portfolio_program

app.include_router(portfolio_program.router, prefix="/api/v1/portfolio", tags=["Portfolio Program"], dependencies=_AUTHENTICATED)

# Graph (DynamoDB-derived or Neptune)
from backend.src.api.v1 import graph

app.include_router(graph.router, prefix="/api/v1/graph", tags=["Graph"], dependencies=_AUTHENTICATED)

# Skills & Config Management
from backend.src.api.v1 import skills

app.include_router(skills.router, prefix="/api/v1/skills", tags=["Skills"], dependencies=_AUTHENTICATED)

from backend.src.api.v1 import infrastructure

app.include_router(infrastructure.router, prefix="/api/v1/infrastructure", tags=["Infrastructure"], dependencies=_AUTHENTICATED)

# Governance — the run + approval-gate audit trail, assembled from Step Functions history.
from backend.src.api.v1 import governance

app.include_router(governance.router, prefix="/api/v1/governance", tags=["Governance"], dependencies=_AUTHENTICATED)


# ── Dev auth endpoints — only available when DEV_AUTH=true ─────────────

if settings.dev_auth:
    from backend.src.auth.dev_user import (
        DEV_USER_EMAIL,
        DEV_USER_GROUPS,
        create_dev_token,
        get_dev_user,
    )

    @app.get("/api/v1/auth/dev-login")
    async def dev_login():
        """Dev-only: returns a fake JWT with full admin permissions."""
        return {
            "access_token": create_dev_token(),
            "id_token": create_dev_token(),
            "token_type": "Bearer",  # nosec B105 — OAuth token_type label, not a secret
            "expires_in": settings.dev_token_ttl_seconds,
            "user": {
                "email": DEV_USER_EMAIL,
                "username": get_dev_user().username,
                "groups": DEV_USER_GROUPS,
            },
        }

    @app.get("/api/v1/auth/dev-user")
    async def dev_user_info():
        """Dev-only: returns the dev user profile."""
        user = get_dev_user()
        return {
            "sub": user.sub,
            "email": user.email,
            "username": user.username,
            "groups": user.groups,
        }
