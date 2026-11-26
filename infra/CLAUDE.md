# CLAUDE.md — infra/

Terraform for the single AgentCore deployment. Everything is provisioned here **except** the
Terraform-state S3 bucket + `{prefix}_tf_lock` DynamoDB table (bootstrapped by `scripts/setup.sh`).

## Layout

- `environments/agentcore/` — the ONLY environment (root module). Provider AWS `>= 6.0` (needed for
  `aws_bedrockagentcore_*`). Backend `s3` block is partial (bucket + lock table via `-backend-config`).
  - `main.tf` — providers, core modules (ecr/s3/dynamodb), tool IAM, MCP Gateway, Harness runtime role, root
    variables (`project_prefix` **required, no default**, `aws_region`, `bedrock_model_id`,
    `dev_auth` — a string `"true"/"false"`).
  - `network.tf` — minimal VPC + 2 public subnets + IGW (workers run `assignPublicIp=ENABLED`, no NAT).
  - `ecs.tf` — `ecs` + `worker-task` modules (cluster + worker task def + API task def + scoped task role). Passes
    the Harness ARN and Gateway id/URL to the API task (surfaced by `/system/capabilities` + `/infrastructure/`).
  - `lambdas.tf` — 5 MCP tool Lambdas (`lambda-src/tool_{portfolio,strategy,planning,documents,execution}.py`)
    + `invoke_runtime` bridge + IAM. The tool_documents and invoke_runtime zips also bundle
    `backend/src/core/risk_scoring.py`. `tool_execution` reads optional env overrides `ECS_ASSIGN_PUBLIC_IP`,
    `WORKER_CONTAINER_NAME`, `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` (defaults in code).
  - `gateway-tools.tf` — 5 Gateway targets (portfolio/strategy/planning/documents/execution).
  - `harness.tf` — the Harness; **system prompt = base prompt + the 9 `harness_skill_files`** (discovery,
    risk_scoring, grouping, assessment, planning, design, execution, validation, handover) read from
    `../../../backend/resources/skills/*.md` via `file()`. `lifecycle.ignore_changes = [environment_variables]`
    works around a provider bug.
  - `step-functions.tf` — the `{prefix}-agentcore-workflow` state machine (`templatefile`) + SFN execution role +
    log group. The role may invoke the
    Harness and the invoke_runtime / tool_execution / tool_documents Lambdas.
  - `neptune.tf` — opt-in (`enable_neptune`): Neptune Serverless in two private subnets (no internet route), IAM DB auth,
    8182 from the API/worker SGs only; sets the hosted API's `GRAPH_BACKEND=neptune` + `NEPTUNE_ENDPOINT`.
  - `observability.tf` — CloudWatch Transaction Search, **on by default** (`enable_transaction_search = true`).
  - `kms.tf` — platform CMK (rotation on) for DynamoDB, ECR, log groups and Lambda env vars.
  - `public-hosting.tf` — opt-in (`enable_public_hosting`, default false): ECS API service + **internal** ALB in
    private subnets reached only via a CloudFront VPC origin + ALB access-log bucket + ACM cert on `portal_domain_name` +
    S3 site bucket + CloudFront (one origin for SPA and `/api/*`); access control is Cognito plus an optional AWS WAF viewer-IP
    allowlist (`cloudfront_allowed_cidrs` / `CLOUDFRONT_ALLOWED_CIDRS`).
  - `cognito.tf` — opt-in (`enable_cognito`, default false): user pool + Hosted-UI SPA app client +
    `Admin`/`Engineer`/`Viewer` groups + seed admin user (`admin_email`). Backend validates the JWT; `rbac.py` maps group→role.
- `modules/` — `dynamodb · s3 · ecr · ecs · worker-task` (only these; ALB/CloudFront live in `public-hosting.tf`).
  `worker-task` defines both the worker task (`{prefix}-worker-task`) and the API task (`{prefix}-api-task`, same
  image, uvicorn entrypoint).
- `step-functions/migration-workflow.v2.asl.json` (JSONata). Placeholders `${HarnessArn}` / `${InvokeRuntimeFn}` / `${ToolExecutionFn}` / `${ToolDocumentsFn}` are
  filled by `templatefile` in `step-functions.tf`.

## IAM gotchas (all were real failures)

- **SFN execution role** must allow `bedrock-agentcore:InvokeAgentRuntime` (plus `InvokeHarness`) on the
  harness ARN + `/*` — the `invokeHarness` task uses InvokeAgentRuntime under the hood.
- **Harness runtime role** (`agent_runtime_role`) has only `bedrock-agentcore:InvokeGateway` (this gateway) and the
  workload-identity token actions. Long-term memory is disabled, so it needs no Memory actions; re-enabling
  memory would also need `ListEvents`/`CreateEvent`/… here.
- **Tool Lambda role** needs `dynamodb:BatchGetItem` (`get_app_context`) and `states:SendTask*` (failure callbacks).
  `tool_lambda_sfn_callbacks` grants only the `SendTask*` callbacks; no tool Lambda may `StartExecution`.

## Notes

- **MCP Gateway** auth is `AWS_IAM` (`NONE` is rollback only). **Portal/API** auth is available via
  Cognito (`cognito.tf`) — enable it for any hosted demo; local dev uses the `DEV_AUTH` bypass.
- Deploy via `scripts/setup.sh [prefix]` (do not hand-run `terraform apply` without the backend-config and
  `-var project_prefix=…` it sets — `project_prefix` has no default).
- **UAT deploy:** `AWS_REGION=us-east-1 ENABLE_PUBLIC_HOSTING=true ADMIN_EMAIL=you@corp.com PORTAL_DOMAIN_NAME=… PORTAL_ACM_CERTIFICATE_ARN=… scripts/setup.sh uat`
  (the distribution refuses the default CloudFront certificate; point DNS at the `portal_cloudfront_domain_name` output)
  (turns on `enable_public_hosting` + `enable_cognito`, builds/deploys the UI, writes Cognito config into `.env`).
- Day-2 operations (flywheel, operations/monitoring tools) are out of scope for this release.
- `validate` needs modules installed: `terraform init -backend=false` then `terraform validate`.
- CI (`.gitlab-ci.yml`) has no Terraform/deploy jobs; deploys are `scripts/setup.sh`.
