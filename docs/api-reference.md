# API Reference

FastAPI control plane (`backend/src/main.py`). Every router is mounted under `/api/v1`.

- Base URL (local dev): `http://localhost:8000`
- Interactive docs: `/docs` (Swagger UI), `/redoc`, `/openapi.json`

This page lists every route. Request and response schemas are authoritative in `/docs`.

## Authentication

`AuthMiddleware` (`backend/src/auth/middleware.py`) runs on every request.

| Mode | Behaviour |
|---|---|
| `DEV_AUTH=true` (local dev) | Every request runs as a dev user in the Admin, Engineer and Viewer groups. `GET /api/v1/auth/dev-login` and `GET /api/v1/auth/dev-user` are mounted. |
| `DEV_AUTH=false` (hosted / UAT) | Requires `Authorization: Bearer <Cognito JWT>`. The token is validated against the pool's JWKS (`COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`). A missing or invalid token returns `401`. |

These paths skip authentication: `/health`, `/docs`, `/redoc`, `/openapi.json` and the two dev-auth routes. `/health/ready` does **not** skip it.

**RBAC.** Cognito groups map to roles `Viewer < Engineer < Admin` (`auth/rbac.py`). Only the routes marked with a role in the tables below enforce one through `require_role`. Every other route needs authentication only.

## Response envelope

Most routes return `ApiResponse` (`core/models.py`):

```json
{ "status": "success", "data": { }, "meta": { }, "error": null }
```

Failures go through `error_response()`, which returns a real non-2xx HTTP status:

```json
{ "status": "error", "data": null, "meta": {}, "error": { "code": "NOT_FOUND", "message": "…", "details": { } } }
```

How codes map to HTTP status:
- `NOT_FOUND` / `*_NOT_FOUND` → 404
- `VALIDATION_ERROR`, `BAD_REQUEST` or any code containing `INVALID` → 400
- `UNAUTHORIZED` / `*EXPIRED*` / `*AUTH*` → 401
- `FORBIDDEN` → 403
- `CONFLICT`, `WORKFLOW_ALREADY_RUNNING` → 409
- `*DISABLED*` / `*UNAVAILABLE*` → 503
- anything else → 500

Some older handlers still return `ApiResponse(status="error", data={"message": …})` with HTTP 200. An example is the discovery upload. Clients should check `status` as well as the HTTP code. The frontend client (`frontend/src/api/client.ts`) does this.

There is no rate limiting. In the hosted deployment, CloudFront and the ALB sit in front of the API.

---

## Health and dev auth (`main.py`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/health` | Liveness | `{"status":"ok"}`, unauthenticated |
| GET | `/health/ready` | Readiness: DynamoDB `describe_table` + the journey state machine | 200 `{"status":"ready","dependencies":{…}}`; 503 with `AWS_CREDENTIALS_EXPIRED` or `AWS_DEPENDENCY_UNAVAILABLE` |
| GET | `/api/v1/auth/dev-login` | Fake unsigned admin JWT | Only when `DEV_AUTH=true`; `expires_in` = `DEV_TOKEN_TTL_SECONDS` (86400) |
| GET | `/api/v1/auth/dev-user` | Dev user profile | Only when `DEV_AUTH=true` |

## System — `/api/v1/system`

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/capabilities` | Feature flags for the SPA | Not wrapped in the envelope. Returns `environment`, `deployment_mode` (`"agentcore"`), `graph_backend`, `wave_planning_enabled`, `aws_region`, `agentcore_harness_id`, `bedrock_model_id`, `sfn_state_machine_name` |

## Infrastructure — `/api/v1/infrastructure`

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/` | Configured AgentCore deployment (names and ids from settings; no live AWS calls) | `data` holds `region`, `project_prefix`, `environment`, `dynamodb_table`, `s3_bucket`, `graph_backend`, `wave_planning_enabled`, `harness{id,arn,model}`, `gateway{id,endpoint}`, `workflow{state_machine,invoke_lambda}`, `ecs{cluster,task_definition,log_group}`. Used by Settings → Infrastructure. |

## Portfolio — `/api/v1/portfolio`

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/apps` | List applications (summary) | `limit` default 200, max 500 |
| GET | `/apps/{app_id}` | Full app: owners, repos, notes, machines, software, completeness | |
| POST | `/apps` | Create an application with sub-entities | Body: `ApplicationModel` |
| PUT | `/apps/{app_id}` | Merge arbitrary fields into the app `META` record | Drops `_`-prefixed temporary fields. Validates strategy-dependent fields (`target_compute`, `target_database`, `cicd_target` against `core/strategy_options.py`) and repo URLs. No schema change is needed for new fields. |
| POST | `/apps/enrich` | L2 enrichment: owners, repos, business unit, tier and so on | Body: `EnrichAppItem[]`. Unknown fields are kept (`extra="allow"`). L2 `machine_ids` replace the ADS auto-grouping. |
| GET | `/machines` | List machines | `limit` default 500, max 5000 |

### Portfolio program view (same prefix, `portfolio_program.py`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/dashboard` | Every app with phase, status, strategy and wave | |
| GET | `/waves` | Per-wave counts by status and completion ratio | |
| GET | `/at-risk` | Apps that are failed, blocked, or waiting on an approval for too long | `aging_hours` default `APPROVAL_AGING_HOURS` (48), range 1–720 |
| GET | `/metrics` | Completion, burndown and velocity | `weeks` default 8, range 1–52 |

## Discovery — `/api/v1/discovery`

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/upload` | Upload AWS ADS discovery exports and ingest them in one call | Multipart `files[]`; servers are auto-grouped into apps |

## Workflow (HITL hub) — `/api/v1/workflow`

Step Functions (`{prefix}-agentcore-workflow`) holds the live status. DynamoDB is a cache.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/start` | Start the journey for an app | Body `{app_id}`. Resets the run-derived META fields. Returns 409 `WORKFLOW_ALREADY_RUNNING` if a run is already active. |
| POST | `/approve` | Resolve the pending gate (`SendTaskSuccess` / `SendTaskFailure`) | Body `{app_id, step, approved=true, comment="", inputs?}`. Writes `APPROVAL_AUDIT#` with the execution ARN and the gate inputs. A rejection comment becomes REVIEWER FEEDBACK on the re-run. |
| POST | `/cancel` | Stop the running execution | Body `{app_id, reason}` |
| GET | `/active` | All workflows, with live Step Functions status | |
| GET | `/pending/{app_id}` | Pending approvals for the **current** execution | Includes the gate `decision_summary` |
| GET | `/execution/{app_id}` | Combined Execution-screen view (transformation, infrastructure, CI/CD) | |
| GET | `/failure/{app_id}` | Why the latest run failed: failed step, kind (`task_error`, `precondition`, `post_check`, `rejected`, `aborted`, `timeout`), plain-language reasons, warnings, raw error. Rebuilt from the Step Functions history. | `failed:false` when the run hasn't failed |
| GET | `/tool-calls/{app_id}` | Captured MCP tool-call traces | `phase` optional |
| GET | `/report/{app_id}/{phase}` | Phase report artifacts from S3 (presigned) | `expires` default 3600 |
| GET | `/migration-plan/{app_id}` | Stored migration plan | |
| GET | `/migration-plan/{app_id}/defaults` | Suggested engineer inputs from the app's data | |
| POST | `/migration-plan/{app_id}/submit` | Store validated inputs | Body `MigrationPlanInputs` |
| GET | `/handover/{app_id}` | Handover package (runbook + decommission plan) | |
| POST | `/wave-plan/generate` | Portfolio assessment plus wave assignment | Body `WavePlanConfig` |
| POST | `/wave-plan/agent` | Wave plan built deterministically from the dependency graph | |
| POST | `/wave-plan/complete` | Save the reviewed wave assignments on each app | Body `{app_ids[], wave_assignments{}}`. Returns `assigned`; `resumed` is always 0 (no Step Functions state waits on a wave plan). |
| GET | `/wave-plan/portfolio-report` | Latest portfolio (modernization-readiness) report from S3 | |

Gate step names: `discovery_review`, `assessment_review`, `design_review`, `execution_review`, `validation_review`, `cutover_review`, `handover_review`.

## Analysis — `/api/v1/analysis`

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/transformation-gaps` | Combined "missing ATX TD" gaps across all assessments | |
| POST | `/{app_id}/risk-score` | Recompute the deterministic migration risk score (0–100) | Uses `core/risk_scoring.py` |
| GET | `/{app_id}/{analysis_type}` | All runs of one analysis type | |
| GET | `/{app_id}/{analysis_type}/html` | Latest run's report as HTML | Used in an iframe |

## Design — `/api/v1/design`

The Harness generates design documents in the `Design` state. This route reads them.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/{app_id}` | Latest design document | A deterministic "Schedule Risk" banner is prepended when the EOSL buffer is under `SCHEDULE_RISK_WARN_DAYS` (90) |

## Waves — `/api/v1/waves`

Wave plans are generated by `POST /api/v1/workflow/wave-plan/generate` (or `/wave-plan/agent`).

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/plan` | Latest wave plan | |
| PUT | `/assign` | Move an app to another wave | Query `app_id`, `wave_number` |

## Graph — `/api/v1/graph`

Data comes from the DynamoDB-derived graph (`db/graph_dynamo.py`, the default) or from Amazon Neptune when `GRAPH_BACKEND=neptune` (`POST /api/v1/graph/sync` re-projects the table into Neptune; Engineer+).

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/topology` | App-to-app topology for visualization | |
| GET | `/wave-insights` | Shared DBs, complexity scores, recommended order | `pilot` / `early` / `later` cut-offs from `WAVE_PILOT_MAX_SCORE` (3) / `WAVE_EARLY_MAX_SCORE` (8) |

## Workbench (ATX catalog) — `/api/v1/workbench`

Transformation plans (`PLAN#`) are created by the `tool_execution` Lambda when the Execution phase launches the
workbench worker; on startup the API marks plans whose ECS task is gone as failed (`recover_stuck_plans`).

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/transformations` | Available ATX transformation definitions | |

## Chat — `/api/v1/chat`

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/message` | Migration advisor backed by the same AgentCore Harness (through the invoke-runtime Lambda) | Body `{app_id, message, session_id?}`. The transcript is kept in S3 `chat-sessions/` (last `CHAT_HISTORY_MAX_MESSAGES` = 40). |

## Skills — `/api/v1/skills`

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/` | List skill playbooks | |
| GET | `/{name}` | Skill content | |
| POST | `/upload` | Write `.md` skills locally and stage them to S3 | The Harness only uses them after `terraform apply` |

## Governance — `/api/v1/governance`

Built from Step Functions execution history plus DynamoDB.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/summary` | Estate-wide rollup of runs, gates and governed apps | |
| GET | `/{app_id}` | One app's audit trail: executions, gates, artifacts, ops events | |
| GET | `/{app_id}/gates` | Gate list with outcomes | Outcomes: `approved`, `rejected`, `cancelled` (aborted run), `pending` |
