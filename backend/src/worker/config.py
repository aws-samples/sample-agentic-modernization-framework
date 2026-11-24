"""
Worker configuration — environment variables and shared AWS clients.

Uses the same pydantic Settings class as the API for consistency,
with additional worker-specific env vars layered on top.
"""

from __future__ import annotations

import logging
import os
import sys

import boto3
import botocore.config

from backend.src.config import settings

# ── Worker-specific env vars (injected by ECS task overrides) ──────────

MODE = os.environ.get("MODE", "assessment")
APP_ID = os.environ.get("APP_ID", "")
# Run correlation id for observability (R41): the ECS job id ties logs/metrics
# for one worker run together with the app id.
RUN_ID = os.environ.get("JOB_ID", "")
ASSESSMENT_TS = os.environ.get("ASSESSMENT_TS", "")
PLAN_ID = os.environ.get("PLAN_ID", "")
STEPS_JSON = os.environ.get("WORKBENCH_STEPS", "[]")
START_FROM_STEP = int(os.environ.get("START_FROM_STEP", "1"))
DESIGN_VERSION = os.environ.get("DESIGN_VERSION", "")
PHASE = os.environ.get("PHASE", "discovery")
# MODE=infra deploy / tear-down (spec 17): set by tool_execution for the Deploy step.
INFRA_ACTION = os.environ.get("INFRA_ACTION", "").strip().lower()  # "" (validate+plan) | apply | destroy
DEPLOY_COMMIT = os.environ.get("DEPLOY_COMMIT", "").strip()
DEPLOY_ROLE_ARN = os.environ.get("DEPLOY_ROLE_ARN", "").strip()
# ECS/SFN callback inputs (one place for every per-run env read).
JOB_ID = RUN_ID
SFN_CALLBACK = os.environ.get("SFN_CALLBACK", "0") == "1"


def sfn_task_token() -> str:
    """The SFN task token, read at call time (sfn_callback / code_analysis)."""
    return os.environ.get("SFN_TASK_TOKEN", "")


def job_id() -> str:
    """The ECS job id, read at call time (workbench task tracking)."""
    return os.environ.get("JOB_ID", "")


def selected_repos_env() -> str:
    """ATX_SELECTED_REPOS, read at call time (code_analysis)."""
    return os.environ.get("ATX_SELECTED_REPOS", "")

# ── Derived from shared settings (single source of truth) ─────────────

AWS_REGION = settings.aws_region
DYNAMODB_TABLE = settings.dynamodb_table
BEDROCK_MODEL_ID = settings.bedrock_model_id
ATX_S3_OUTPUT_BUCKET = settings.s3_bucket
ATX_TD_NAME = settings.atx_code_analysis_td_name
ATX_SHELL_TIMEOUT = settings.atx_shell_timeout
GIT_USER_EMAIL = settings.git_user_email
GIT_USER_NAME = settings.git_user_name
# Pipeline wait (Settings reads the same DEPLOY_PIPELINE_* env vars).
DEPLOY_PIPELINE_TIMEOUT_SECONDS = settings.deploy_pipeline_timeout_seconds
DEPLOY_PIPELINE_START_GRACE_SECONDS = settings.deploy_pipeline_start_grace_seconds
DEPLOY_PIPELINE_POLL_SECONDS = settings.deploy_pipeline_poll_seconds

# ── Logging ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format='{"time":"%(asctime)s","level":"%(levelname)s","app_id":"'
    + APP_ID
    + '","run_id":"'
    + RUN_ID
    + '","mode":"'
    + MODE
    + '","message":"%(message)s"}',
    stream=sys.stdout,
)
logger = logging.getLogger("migration_worker")
logger.info("Migration worker v2026-06-03 | mode=%s | app=%s", MODE, APP_ID)

# ── AWS Clients (shared, reused across worker modules) ─────────────────

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table = dynamodb.Table(DYNAMODB_TABLE)
s3_client = boto3.client("s3", region_name=AWS_REGION)
bedrock_client = boto3.client(
    "bedrock-runtime",
    region_name=AWS_REGION,
    config=botocore.config.Config(
        read_timeout=settings.bedrock_read_timeout,
        connect_timeout=settings.bedrock_connect_timeout,
        retries={"max_attempts": settings.bedrock_max_attempts},
    ),
)

# ── Shared helpers ─────────────────────────────────────────────────────

from backend.src.db.helpers import from_dynamo, to_dynamo  # noqa: F401 — re-exported for worker modules
from backend.src.db.repository import read_full_app as _read_full_app_from_table


def read_full_app(app_id: str) -> dict | None:
    """Read full application data from DynamoDB."""
    return _read_full_app_from_table(ddb_table, app_id)
