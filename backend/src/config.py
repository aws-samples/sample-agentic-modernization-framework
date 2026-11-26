"""
Application configuration via pydantic-settings.
All values can be overridden by environment variables or .env file.
"""

import contextlib

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    aws_region: str = "us-east-1"
    project_prefix: str = "amf"  # All resource names derive from this
    dynamodb_table: str = ""  # Auto: {prefix}_portfolio
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-6"
    s3_bucket: str = ""  # Auto: {prefix}-data-{account_id}-{region}
    log_level: str = "INFO"
    dev_auth: bool = False
    environment: str = "dev"
    cors_origins: str = "http://localhost:3000"
    atx_shell_timeout: int = 36000
    atx_code_analysis_td_name: str = "AWS/comprehensive-codebase-analysis"
    # GET /workbench/transformations runs `atx custom def list` (~900 entries, slow): bound
    # the call and cache the result.
    atx_list_timeout_seconds: int = 120
    atx_list_cache_ttl_seconds: int = 3600
    atx_ecs_cluster: str = ""  # Auto: {prefix}-cluster
    assessment_ecs_task_definition: str = ""  # Auto: {prefix}-worker-task
    git_user_email: str = "atx-runner@migration-framework.local"
    git_user_name: str = "ATX Runner"
    sfn_state_machine_name: str = ""  # Auto: {prefix}-agentcore-workflow (see model_post_init)
    sfn_state_machine_arn: str = ""  # Auto: built from account + name on first use
    ecs_log_group: str = ""  # Auto: /ecs/{prefix}-migration-worker

    # ── AgentCore (the single agent architecture) ──────────────────────────
    agentcore_gateway_url: str = ""  # MCP Gateway endpoint
    agentcore_gateway_id: str = ""  # Gateway ID for tool registration
    # Harness ARN (Terraform output harness_arn). AGENTCORE_RUNTIME_ARN is the
    # legacy env name setup.sh used to write.
    agentcore_harness_arn: str = Field(
        default="", validation_alias=AliasChoices("agentcore_harness_arn", "agentcore_runtime_arn")
    )
    # Lambda bridge that invokes the AgentCore Harness (SFN + chat share it).
    # Auto-derived from prefix if unset: {prefix}-invoke-runtime
    agentcore_invoke_lambda: str = ""
    # Execution tool Lambda — the API calls it only for the explicit "Tear down"
    # action. Auto-derived if unset: {prefix}-tool-execution
    tool_execution_lambda: str = ""

    # ── Auth ───────────────────────────────────────────────────────────────
    # Hosted mode: ENABLE_PUBLIC_HOSTING=true, or MODE=api (set
    # only on the hosted ECS API task, infra/modules/worker-task). In hosted mode the
    # API refuses to start with DEV_AUTH on or without COGNITO_USER_POOL_ID +
    # COGNITO_APP_CLIENT_ID (auth/startup_guard.py).
    enable_public_hosting: bool = False
    # DEPLOY_ENABLED (Terraform enable_deploy, default off): approving execution_review terraform-applies the
    # reviewed commit. Off, the journey stops at a reviewed migration branch and AMF applies nothing.
    deploy_enabled: bool = False
    run_mode: str = Field(default="", validation_alias=AliasChoices("mode", "run_mode"))
    hosted_run_modes: str = "api"  # comma-separated MODE values that mean "hosted API task"
    dev_token_ttl_seconds: int = 86400
    jwks_cache_ttl_seconds: int = 3600
    http_timeout_seconds: int = 10

    # ── Bedrock (worker synthesis) ─────────────────────────────────────────
    bedrock_read_timeout: int = 600
    bedrock_connect_timeout: int = 10
    bedrock_max_attempts: int = 3
    synthesis_max_tokens: int = 4096
    synthesis_temperature: float = 0.3

    # ── Worker ─────────────────────────────────────────────────────────────
    terraform_cmd_timeout_seconds: int = 900

    # ── Business tunables ──────────────────────────────────────────────────
    # Below this a strategy recommendation is "no evidence" (mirrors
    # tool_strategy.CONFIDENCE_FLOOR in the strategy Lambda).
    strategy_confidence_floor: int = 40
    approval_aging_hours: int = 48  # program view flags approvals older than this
    schedule_risk_warn_days: int = 90  # design: buffer to hard deadline ≤ this → warning
    chat_history_max_messages: int = 40
    chat_invoke_timeout_seconds: int = 120
    failure_history_events: int = 200  # SFN history window scanned for a failure reason  # boto read timeout on the invoke-runtime Lambda call
    bulk_enrich_max: int = 1000  # POST /portfolio/apps/enrich cap (one L2 upload)
    # Wave grouping by dependency score: ≤ pilot → Wave 1, ≤ early → Wave 2, else later.
    wave_pilot_max_score: int = 3
    wave_early_max_score: int = 8
    # Wave planner (core/wave_planner, planning.md). The tool_planning Lambda gets the
    # same values via tool_planning_tunables in lambdas.tf.
    wave_pilot_size: int = 3
    wave_band_limits: str = "3,8,12,10"   # max apps in Pilot, Foundation, Core, Customer-Facing
    wave_spill_limit: int = 10            # max apps in a spill-over wave above the bands
    wave_tier1_max_share: float = 0.30    # share of tier_1 apps allowed in one wave
    wave_max_cycle_cowave: int = 3        # dependency cycles up to this size share a wave

    def wave_planner_config(self):
        from backend.src.core.wave_planner import PlannerConfig
        limits = [int(x) for x in self.wave_band_limits.split(",") if x.strip()]
        return PlannerConfig(pilot_size=self.wave_pilot_size, band_limits=dict(enumerate(limits)),
                             spill_limit=self.wave_spill_limit, tier1_max_share=self.wave_tier1_max_share,
                             max_cycle_cowave=self.wave_max_cycle_cowave)
    graph_max_machines: int = 2000  # DynamoDB graph derivation read cap

    # ── Frontend polling + target regions (exposed via GET /system/capabilities) ──
    # Regions offered as migration targets; the migration-plan default is aws_region.
    supported_target_regions: str = "us-east-1,us-west-2,eu-west-1,ap-southeast-1"
    validation_poll_ms: int = 15000
    validation_poll_max: int = 60
    workflow_poll_ms: int = 10000

    # ── AWS clients (api/ cached client factory, core/aws.py) ──────────────
    aws_connect_timeout_seconds: int = 5
    aws_read_timeout_seconds: int = 30
    aws_max_attempts: int = 3

    # ── API caches / throttles ─────────────────────────────────────────────
    active_workflows_cache_seconds: int = 10  # GET /workflow/active server-side cache
    active_workflows_max_workers: int = 8  # bounded parallel DescribeExecution
    parallel_query_workers: int = 8  # bounded parallel per-machine CONN# queries
    jwks_min_refresh_seconds: int = 60  # unknown kid → JWKS refetch at most this often
    atx_list_min_refresh_seconds: int = 60  # GET /workbench/transformations?refresh=true throttle
    governance_cache_seconds: int = 15  # GET /governance/* execution list cache
    governance_max_executions: int = 500  # list_executions pagination cap
    governance_history_page_size: int = 1000  # get_execution_history maxResults

    # ── API limits / truncation ────────────────────────────────────────────
    # Presigned URL lifetime: a client-supplied `expires` is
    # clamped to [presign_min_seconds, presign_max_seconds] server-side.
    presign_ttl_seconds: int = 900  # default presigned URL lifetime (15 min)
    presign_max_seconds: int = 900  # presigned URL lifetime upper bound (15 min)
    presign_min_seconds: int = 60  # presigned URL lifetime lower bound
    skill_upload_max_bytes: int = 262144  # POST /skills/upload per-file cap (256 KiB)
    report_list_max_keys: int = 500  # S3 listing cap for a phase's report artifacts
    atx_doc_max_chars: int = 8000  # per ATXDocumentation file in GET /analysis
    atx_report_view_max_chars: int = 50000  # report.md / report.json in GET /analysis
    atx_run_list_max_keys: int = 100  # S3 listing cap for one ATX run's outputs
    list_apps_max: int = 500  # accessors.list_all_applications default cap
    list_machines_max: int = 5000  # accessors.list_all_machines default cap
    list_analysis_max: int = 2000  # accessors.list_*_analysis_by_type default cap

    # ── Business thresholds ────────────────────────────────────────────────
    # Risk scoring: EOSL before this year = mid risk. The Lambdas read the same
    # value from the RISK_EOSL_HORIZON_YEAR env var (core/risk_scoring.py).
    risk_eosl_horizon_year: int = 2032
    # Discovery ingestion: ADS grouping confidence by server count.
    ingest_group_high_confidence_servers: int = 10
    ingest_group_medium_confidence_servers: int = 3
    # Program metrics window (GET /portfolio/program/metrics?weeks=).
    program_metrics_default_weeks: int = 8
    program_metrics_max_weeks: int = 52
    # AWS best-practice validation (core/validation/aws_best_practices.py).
    bp_lambda_max_data_volume_gb: int = 100  # Lambda above this → error
    bp_lambda_max_peak_tps: int = 1000  # Lambda above this → warning
    bp_dynamodb_max_data_volume_gb: int = 500  # DynamoDB above this → cost warning
    bp_tier1_min_backup_days: int = 14
    bp_compliance_min_backup_days: int = 30
    bp_container_min_peak_tps: int = 5000  # defaults: above this → ecs_fargate
    bp_lambda_suggest_max_peak_tps: int = 50  # defaults: tier_3 below this → lambda
    bp_load_test_min_peak_tps: int = 500  # suggest load tests above this
    bp_backup_days_tier1: int = 30
    bp_backup_days_tier2: int = 14
    bp_backup_days_tier3: int = 7

    # ── Worker tunables ────────────────────────────────────────────────────
    deploy_cmd_timeout_seconds: int = 3600  # terraform apply/destroy floor
    deploy_role_session_seconds: int = 3600  # chained-role sessions are capped at 1 h
    deploy_pipeline_timeout_seconds: int = 3600
    deploy_pipeline_start_grace_seconds: int = 180
    deploy_pipeline_poll_seconds: int = 20
    # The app's Terraform state backend (must match backend.tf.tpl in the templates).
    app_tfstate_bucket_pattern: str = "{prefix}-tfstate-{account}-{region}"
    app_tfstate_lock_pattern: str = "{prefix}-tfstate-lock"
    app_tf_aws_provider_version: str = "~> 5.0"
    coverage_threshold_default: int = 70
    ci_tool_timeout_seconds: int = 600
    # repo-controlled build/scan code runs as this unprivileged OS user (via a fixed
    # sudo rule in the worker image). Without the user or sudo (a laptop, tests) it falls back to
    # sandbox_env() only.
    build_user: str = "builduser"
    build_isolation_enabled: bool = True
    # Sandbox timings: the one-off `sudo -n -u <build_user> true` probe, coreutils `timeout -k` grace, and the
    # outer backstop on top of the in-sandbox timeout.
    sandbox_probe_timeout_seconds: int = 30
    sandbox_kill_after_seconds: int = 10
    sandbox_timeout_grace_seconds: int = 30
    # Upper bound for one git subprocess in the worker (clone/push of a large repo included).
    git_cmd_timeout_seconds: int = 1800
    # Prefix for generated-Dockerfile base images (e.g. "123.dkr.ecr.us-east-1.amazonaws.com/mirror/");
    # empty → the public registries.
    dockerfile_base_registry: str = ""
    repo_scan_max_files: int = 3000
    repo_scan_max_bytes: int = 400_000
    atx_report_max_chars: int = 15000  # ATX report text stored on the analysis record

    # ── Graph backend / wave planning ──────────────────────────────────────
    # Wave planning needs dependency data. The connection graph is already in
    # the DynamoDB single table (written by discovery ingestion), so a
    # dedicated graph store is an optimisation, not a requirement.
    #   graph_backend = "dynamodb" → derive from the portfolio table (default)
    #   graph_backend = "neptune"  → uat/prod (neptune_endpoint)
    #   graph_backend = "none"     → wave planning DISABLED
    graph_backend: str = "dynamodb"  # dynamodb | neptune | none
    neptune_endpoint: str = ""   # Neptune cluster endpoint (used when graph_backend=neptune)
    neptune_port: int = 8182
    # SSRF guard: the Neptune endpoint must be a bare hostname under this DNS suffix.
    neptune_allowed_host_suffix: str = ".neptune.amazonaws.com"
    # The Neptune graph is a projection of the DynamoDB table (the source of truth);
    # reads re-sync it when it is older than this (POST /graph/sync forces one).
    neptune_sync_ttl_seconds: int = 300
    # Neptune Serverless at min capacity can take well over the generic http_timeout_seconds on a cold
    # cluster, and the TTL re-sync runs inside a request.
    neptune_query_timeout_seconds: int = 60
    # Neptune is a projection of the DynamoDB graph: on any Neptune failure, serve the same contract
    # from graph_dynamo instead of failing the request (logged as a warning).
    neptune_fallback_to_dynamodb: bool = True
    # Declared app→app dependencies per app (META depends_on).
    app_dependencies_max: int = 50

    @property
    def wave_planning_enabled(self) -> bool:
        """Wave planning works on any backend that can supply dependencies.

        Only an explicit graph_backend="none" disables it — the DynamoDB
        derivation needs no extra infrastructure.
        """
        return self.graph_backend.lower() in ("dynamodb", "neptune")

    @property
    def supported_target_regions_list(self) -> list[str]:
        return [r.strip() for r in self.supported_target_regions.split(",") if r.strip()]

    def model_post_init(self, __context):
        """Derive resource names from prefix if not explicitly set."""
        p = self.project_prefix
        if not self.dynamodb_table:
            # Same name Terraform creates: replace(project_prefix, "-", "_") + "_portfolio".
            object.__setattr__(self, "dynamodb_table", f"{p.replace('-', '_')}_portfolio")
        if not self.atx_ecs_cluster:
            object.__setattr__(self, "atx_ecs_cluster", f"{p}-cluster")
        if not self.assessment_ecs_task_definition:
            object.__setattr__(self, "assessment_ecs_task_definition", f"{p}-worker-task")
        if not self.sfn_state_machine_name:
            # The deployed journey state machine. Overridable via
            # SFN_STATE_MACHINE_NAME for other environments.
            object.__setattr__(self, "sfn_state_machine_name", f"{p}-agentcore-workflow")
        if not self.ecs_log_group:
            object.__setattr__(self, "ecs_log_group", f"/ecs/{p}-migration-worker")
        if not self.agentcore_invoke_lambda:
            object.__setattr__(self, "agentcore_invoke_lambda", f"{p}-invoke-runtime")
        if not self.tool_execution_lambda:
            object.__setattr__(self, "tool_execution_lambda", f"{p}-tool-execution")

    @property
    def agentcore_harness_id(self) -> str:
        """Harness id = the last segment of its ARN (…:harness/<id>)."""
        return self.agentcore_harness_arn.rsplit("/", 1)[-1] if self.agentcore_harness_arn else ""

    def get_sfn_state_machine_arn(self) -> str:
        """Journey state-machine ARN; built from the caller account if not set."""
        if not self.sfn_state_machine_arn:
            import boto3
            account_id = boto3.client("sts", region_name=self.aws_region).get_caller_identity()["Account"]
            object.__setattr__(
                self,
                "sfn_state_machine_arn",
                f"arn:aws:states:{self.aws_region}:{account_id}:stateMachine:{self.sfn_state_machine_name}",
            )
        return self.sfn_state_machine_arn

    _cached_bucket: str = ""

    def get_s3_bucket(self) -> str:
        """The data bucket: S3_BUCKET if set (setup.sh, Terraform), else derived from this account.

        The derived name needs the account ID. If STS can't supply it, this raises instead of
        guessing an account-less name, which could belong to another account.
        """
        if self.s3_bucket:
            return self.s3_bucket
        if self._cached_bucket:
            return self._cached_bucket
        try:
            import boto3
            account_id = boto3.client("sts", region_name=self.aws_region).get_caller_identity()["Account"]
        except Exception as err:
            raise RuntimeError(
                "S3_BUCKET is not set and the account ID could not be read from STS; "
                "set S3_BUCKET (scripts/setup.sh writes it to .env)") from err
        self._cached_bucket = f"{self.project_prefix}-data-{account_id}-{self.aws_region}"
        # Set on the instance so direct .s3_bucket access also works
        object.__setattr__(self, "s3_bucket", self._cached_bucket)
        return self._cached_bucket

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore", "populate_by_name": True}


settings = Settings()
# Resolve the S3 bucket at startup when possible; if STS isn't ready yet, the first S3 use retries
# (and fails clearly if it still can't).
with contextlib.suppress(RuntimeError):
    settings.get_s3_bucket()
