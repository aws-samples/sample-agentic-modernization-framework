# CLAUDE.md — backend/

Python 3.12 FastAPI control plane + ECS ATX worker. See the root `CLAUDE.md` for the overall
architecture. Run `pytest backend/tests -q` and `ruff check backend/src backend/tests` before finishing.

## Layout & responsibilities

- `src/main.py` — FastAPI app; mounts `api/v1/*` routers; health probes (`/health`, `/health/ready`);
  startup does bounded GSI1 recovery of stuck workflows and workbench plans (no scans). Dev-only `/api/v1/auth/dev-*` routes exist
  only when `settings.dev_auth`.
- `src/config.py` — pydantic-settings; **the single source of configuration** (all resource names derive from
  `PROJECT_PREFIX`). `model_post_init` derives `dynamodb_table`, `sfn_state_machine_name`
  (`{prefix}-agentcore-workflow`), `agentcore_invoke_lambda` (`{prefix}-invoke-runtime`), `atx_ecs_cluster`,
  `assessment_ecs_task_definition`, `ecs_log_group`. Helpers: `get_s3_bucket()`, `get_sfn_state_machine_arn()`
  (or set `SFN_STATE_MACHINE_ARN`), `agentcore_harness_id` (from `agentcore_harness_arn`; legacy env name
  `AGENTCORE_RUNTIME_ARN` still accepted). Tunables live here too:
  `dev_token_ttl_seconds`, `jwks_cache_ttl_seconds`, `http_timeout_seconds`,
  `bedrock_read_timeout`/`bedrock_connect_timeout`/`bedrock_max_attempts`, `synthesis_max_tokens`/`synthesis_temperature`,
  `terraform_cmd_timeout_seconds`, `strategy_confidence_floor`, `approval_aging_hours`, `schedule_risk_warn_days`,
  `chat_history_max_messages`, `wave_pilot_max_score`/`wave_early_max_score`/`wave_max_iterations`,
  `graph_max_machines`.
- `src/api/v1/` — one router per resource (portfolio + portfolio_program, workflow, discovery, analysis, design,
  waves, graph, workbench, chat, skills, system, infrastructure, operations, governance). `workflow.py` is the HITL hub (`/start`, `/approve`, `/cancel`, `/pending`,
  `/active`, `/execution`, migration-plan, wave-plan, `/validate`, `/teardown`, tool-calls). `system.py` `/capabilities` and
  `infrastructure.py` `/` feed the frontend its region, harness id, model and resource names.
  `operations.py`/`governance.py` are read-mostly views over `OPS_*` records and Step Functions history.
- `src/core/` — pure logic: `wave_planner.py`, `completeness.py`, `risk_scoring.py` (also zipped into
  the tool_documents + invoke_runtime Lambdas), `strategy_options.py` (valid targets per strategy),
  `exposure_policy.py`, `validation/`.
- `src/db/` — single-table access. **`keys.py` is the one place PK/SK/GSI1 layout lives.**
  `accessors.py` (typed reads + GSI1 lists), `repository.py` (`read_full_app`),
  `helpers.py` (`get_table`, `to_dynamo`/`from_dynamo`), `reports.py` (S3 artifacts + presigned URLs),
  `graph_dynamo.py` (default dependency graph derived from the table), `neptune.py` (`GRAPH_BACKEND=neptune`: SigV4 openCypher, a TTL-synced projection of the table,
  same response contracts as `graph_dynamo`). Wave planning is enabled unless `GRAPH_BACKEND=none`.
- `src/worker/` — ECS worker. `main.py` dispatches `code-analysis` + `workbench` (ATX), `infra`
  (`terraform_runner.py` — terraform validate+plan on generated IaC, output to `execution/infrastructure/`),
  `config.py` (worker env + shared clients, values from `Settings`),
  `atx.py` runs the ATX CLI, `code_analysis.py`, `workbench.py`, `synthesis.py` (Bedrock converse),
  `branches.py`, `dockerfile_gen.py`; `sfn_callback.py` does idempotent `SendTaskSuccess/Failure`;
  `dynamo.py` holds `store_analysis_record`.
- `resources/templates/` — committable IaC + CI/CD templates (`terraform/`, `codepipeline/` — CodePipeline is
  the only CI/CD target), each with a `templates.json` manifest; consumed by the `tool_execution` Lambda.
  `terraform/environments/template.tfvars` is a placeholder template (explicitly un-ignored in `.gitignore`).
- `src/auth/` — Cognito JWT validation (`cognito.py`) + RBAC (`rbac.py`, Admin > Engineer > Viewer via
  `require_role`) + dev bypass (`dev_user.py`, only when `DEV_AUTH=true`).
- `resources/skills/*.md` — Harness phase playbooks (composed into the prompt by Terraform, not read at runtime).

## Rules

- **No hardcoded tunables, resource names, ARNs or regions.** Add a `Settings` field and read `settings.<field>`;
  don't call `os.getenv` in `src/` (worker per-run inputs like `MODE`/`APP_ID` in `worker/config.py` are the exception).
- **No `table.scan`.** List via `GSI1` (`GSI1PK=TYPE#<entity>`). New writers of `WORKFLOW#ACTIVE` /
  `TASK_TOKEN#` must use `keys.workflow_active()` / `keys.task_token()` so GSI attrs are always present.
- **No worker → api imports.** Shared reads live in `db/repository.py` (e.g. `read_full_app`).
- Workflow status is derived from Step Functions live (source of truth); DynamoDB `status` is a cache.
- `PUT /portfolio/apps/{id}` merges arbitrary fields into the app META (strips `_`-prefixed temp fields) —
  adding new app fields needs no schema change. It validates strategy-dependent targets
  (`strategy_options.validate_assessment_inputs`) and repo URLs.
- Error style: routers return `ApiResponse` / `error_response` (`core/models.py`).

## Don't

- Don't re-add `src/agents/` or `src/agentcore/` (deleted; single Harness architecture).
- Don't add `strands` or agent-framework deps — tool logic lives in `infra/.../lambda-src/tool_*.py`.
- Don't reintroduce removed settings (`deployment_mode`, `enable_graph_db`, `api_base_url`,
  `atx_s3_output_bucket`/`s3_connector_bucket` aliases) — use `get_s3_bucket()` and `GRAPH_BACKEND`.
