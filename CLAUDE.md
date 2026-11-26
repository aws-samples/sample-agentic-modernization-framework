# CLAUDE.md — Agentic Modernization Framework (AMF)

Guidance for Claude Code working in this repo. Keep it accurate; update it when the architecture changes.

## What this is

> **Proof of concept, not production.** Keep it that way in docs and code: sandbox accounts, synthetic data
> only, human-approved deploys. See `SECURITY.md` and the README disclaimer.

AMF orchestrates on-prem → AWS application migrations using a **single Amazon Bedrock AgentCore
architecture**: one declarative **Harness** reasons per phase and calls **MCP Gateway tools**
(Lambda-backed). **AWS Step Functions** durably orchestrates the journey + human approval gates.
Long-running **AWS Transform (ATX)** code analysis/transformation runs on **ECS Fargate**.
FastAPI backend (control plane) + React/CloudScape frontend.

> There is exactly ONE agent architecture (the Harness). The old ECS/Strands multi-agent path
> (`backend/src/agents/`, `backend/src/agentcore/runtimes/`) was **deleted** — do not reintroduce it.

## Architecture (canonical)

```
Frontend (React) → FastAPI (control plane) → Step Functions ({prefix}-agentcore-workflow)
  • pre-hooks     → lambda:invoke → invoke_runtime (mode=prehook_phase) → deterministic precondition gate
  • agent phases  → arn:aws:states:::bedrockagentcore:invokeHarness → Harness → MCP Gateway (5 tool Lambdas) → DynamoDB + S3
  • risk score    → lambda:invoke → tool_documents (compute_risk_score) — Score_Discovery, deterministic
  • post-checks   → lambda:invoke → invoke_runtime (mode=validate_phase) → deterministic phase-output eval
  • ATX phases    → lambda:invoke.waitForTaskToken → tool_execution → ECS Fargate worker (MODE=code-analysis|workbench) → SendTaskSuccess
                    (code-analysis also runs the repo scan → META detected_* wiring + ANALYSIS#ci-readiness; ATX is scoped to build_dir)
  • infra/CI-CD   → lambda:invoke → tool_execution directly: provision_infrastructure / generate_cicd (each commits to the
                    migration branch internally) — deterministic, not via Harness
  • tf plan       → lambda:invoke.waitForTaskToken → tool_execution run_terraform_plan → worker MODE=infra (validate + plan)
  • CI fix        → lambda:invoke.waitForTaskToken → tool_execution run_ci_fix → worker MODE=ci-fix (FixCIGaps; advisory)
  • deploy        → OFF by default (enable_deploy=false): deploy_infrastructure returns "skipped" at once, Check_Deploy
                    → Validation (static checks only). With ENABLE_DEPLOY=true: execution_review approval = Deploy of the
                    reviewed commit → worker MODE=infra INFRA_ACTION=apply (deploy role) + first CodePipeline run → Check_Deploy
  • validation    → lambda:invoke → tool_execution validate_deployment: L0 platform + L1 smoke / L2 contract / L3 data
                    (validation_probe; in-VPC probe Lambda for internal ALBs, SigV4 for HTTP APIs) — deterministic
  • approvals     → lambda:invoke.waitForTaskToken → invoke_runtime (mode=store_token; computes decision_summary)
```

Journey phases: **Discovery → Assessment → Design → Execution (Transform + DB Migration plan + Infra + tf plan +
CI fix + CI/CD, sequential) → Deploy (human-approved commit) → Validation → Cutover → Handover**. `rehost`/`relocate` skip `LaunchTransformation`.
7R `close_out` strategies (retire/retain/repurchase) skip Assessment→Cutover and go straight to Handover:
`Handover_CloseOut` → `Validate_CloseOut` → `Check_CloseOut` → `Approval_CloseOut` (step `handover_review`) → `MarkCompleted`.

**Gates** (7-day timeout each): `discovery_review`, `assessment_review`, `design_review`, `execution_review`,
`validation_review`, `cutover_review`, `handover_review`. Reject
re-runs the phase for Discovery/Assessment/Design/close-out (comment appended as REVIEWER FEEDBACK); reject →
`MarkFailed` for Validation/Cutover; an Execution (deploy-gate) reject regenerates infra/CI-CD (`InfrastructureProvisioning`). `decision_summary` is computed deterministically by
`invoke_runtime._build_decision_summary` (the ASL hard-codes the store_token payload, so the agent can't supply it).

**Deterministic pre-hooks** gate the four core phases (`PreHook_<phase>` Task → `PreCheck_<phase>` Choice →
`MarkFailed` on unmet preconditions). They run `invoke_runtime` `mode=prehook_phase`, reading only persisted
state, so a phase never starts with missing inputs (fails fast instead of emitting `[REQUIRES INPUT]`):
- **Discovery** — app exists + `app_name` + a linked repo (`REPO#`).
- **Assessment** — `migration_strategy` resolved + a linked repo with a clonable URL (a bare repo name → `repo_url_invalid:*`; completeness-gate gaps are advisory warnings, not blockers).
- **Design** — `migration_strategy` + `target_compute`/`target_database`/`cicd_target` each valid for the strategy + an `assessment-summary` analysis from the **current run** (not just any `ANALYSIS#` — the Discovery risk-score record no longer satisfies it).
- **Execution** — `migration_strategy` + `target_compute`/`target_database`/`cicd_target` each valid for the strategy (mirrors `core/strategy_options.py`) + 12-digit `target_account` + RFC1918 `vpc_cidr` + a linked repo.

**Deterministic post-checks** (`invoke_runtime` `mode=validate_phase`): **Discovery** (`Validate_Discovery` →
`Check_Discovery`) needs `migration_strategy` + `strategy_confidence` and self-heals a missing risk score;
**Assessment** needs a current-run `assessment-summary` with no `consistency_contradictions` (written for another
strategy than the confirmed one → one re-run with the blockers, then `MarkFailed`); **Close-out** (`phase=closeout`)
needs this run's `DESIGN#final` with no contradictions against the app's recorded facts
(`tool_documents._check_closeout_document`: machine count, owners, RPO/RTO, dependencies, cost, risk score, hard
deadline, past-dated milestones) — one re-run, then the `handover_review` gate shows any that remain; **Design**
(`Validate_Design` → `Check_Design`) needs the latest stored design version from this run with **no contradictions**
against the approved compute/DB/CI-CD/account/CIDR/strategy — on failure Design re-runs once with the blockers in its
prompt, then `MarkFailed`; **Validation** (`Check_Validation`) proceeds on `pass`/`pass_with_warnings`, else
`Failed_Validation`. Softer findings (missing design sections, assessment `consistency_warnings`, completeness gaps)
ride on the gate's `decision_summary.warnings` and show in the approval modal.

**One ASL** (JSONata, Terraform-owned in `infra/environments/agentcore/step-functions.tf`, ARNs injected via
`templatefile` — placeholders `${HarnessArn}`, `${InvokeRuntimeFn}`, `${ToolExecutionFn}`, `${ToolDocumentsFn}`):
**`infra/step-functions/migration-workflow.v2.asl.json`** → `{prefix}-agentcore-workflow` — the Day-1 journey above.

> **Day-2 operations are out of scope for this release** (no operations flywheel, operations/monitoring
> tools, `operations.md` playbook or `/operations` API). Don't reintroduce them without an approved spec.

## Repo layout

- `backend/src/api/v1/` — FastAPI routers: portfolio (+ portfolio_program), workflow, discovery, analysis, design,
  waves, graph, workbench, chat, skills, system, infrastructure, governance
- `backend/src/core/` — pure business logic: wave_planner, completeness, risk_scoring (also zipped into the
  tool_documents + invoke_runtime Lambdas), strategy_options, exposure_policy, validation/
- `backend/src/db/` — DynamoDB single-table (`keys.py`, `accessors.py`, `repository.py`, `helpers.py`, `reports.py`) +
  `graph_dynamo.py` (default dependency graph, derived from the table) + `neptune.py` (optional graph)
- `backend/src/worker/` — ECS worker MODEs: `code-analysis` + `workbench` (ATX) · `infra` (terraform validate+plan,
  `terraform_runner.py`)
- `backend/resources/skills/*.md` — 9 Harness phase playbooks (discovery, risk_scoring, grouping, assessment, planning,
  design, execution, validation, handover), composed into the prompt via `harness_skill_files` in `harness.tf`
- `backend/resources/templates/` — committable IaC + CI/CD templates (`terraform/`, `codepipeline/`)
- `frontend/src/` — React/CloudScape SPA
- `infra/environments/agentcore/` — the single Terraform deployment (+ `lambda-src/` tool Lambdas and their pytest suite)
- `infra/modules/` — `dynamodb · s3 · ecr · ecs · worker-task` (worker-task also defines `{prefix}-api-task`)
- `scripts/` — `setup.sh` (deploy), `start.sh`/`stop.sh` (local dev), `cleanup.sh` (data reset; `infra` mode),
  `seed_transformation_steps.py`
- `docs/` — architecture, deployment, api-reference, troubleshooting, cicd-setup, cost-consumption, solution-overview
- `input/` — local-only (gitignored) synthetic ADS data for the optional `test_discovery.py` cases, which skip
  when it's absent. Not read at runtime.

## Commands

```bash
# Deploy everything to AWS (one prefixed env; bootstraps TF state + builds worker image)
scripts/setup.sh [prefix]              # default prefix: amf

# Local dev: backend (uvicorn :8000) + frontend (vite :3000) against the cloud data plane
scripts/start.sh                       # scripts/stop.sh to stop

# Backend checks (ALWAYS run before considering backend work done)
backend/.venv/bin/python -m pytest backend/tests -q
backend/.venv/bin/ruff check backend/src backend/tests
(cd infra/environments/agentcore/lambda-src && ../../../../backend/.venv/bin/python -m pytest -q)   # tool Lambdas

# Frontend
cd frontend && pnpm install --frozen-lockfile && pnpm exec tsc --noEmit && pnpm test     # vitest

# Terraform
cd infra/environments/agentcore && terraform validate   # (init needs -backend-config; setup.sh handles it)

# Data reset (destructive; requires confirmation)
scripts/cleanup.sh [--yes]
```

## Conventions

- **Prefix everything.** All AWS resource names derive from `PROJECT_PREFIX` (default `amf` in config/setup.sh).
  The Terraform `project_prefix` variable is **required** (no default; `setup.sh` passes it). Default
  region: `us-east-1`.
- **`backend/src/config.py` is the single source of configuration.** No hardcoded resource names/ids, ARNs, regions
  or tunables in code — add a `Settings` field (env-overridable) and read it. Resource names are derived in
  `model_post_init`; use `settings.get_s3_bucket()` and `settings.get_sfn_state_machine_arn()` (not ad-hoc STS
  lookups). Lambdas take config from env vars set in `lambdas.tf`; the frontend reads region/harness id/model from
  `useCapabilities()` (`GET /system/capabilities`) and resource names from `GET /infrastructure/`.
- **DynamoDB single-table.** Build keys via `backend/src/db/keys.py` (never hardcode PK/SK/GSI). List via
  the `GSI1` (`GSI1PK=TYPE#<entity>`) partition — **never `table.scan`**.
- **Agent behavior = skills.** Phase procedures live in `backend/resources/skills/*.md` and are composed
  into the Harness system prompt in `harness.tf`. Editing a skill requires `terraform apply` to take effect.
- **The approved selection is what gets built.** Scope: `target_compute` is ec2 (rehost/relocate
  only) or ecs_fargate | lambda (replatform/refactor); close-outs have none. `target_database` is dynamodb |
  rds_postgresql | rds_mysql | aurora_postgresql | aurora_mysql | none (lift: same_as_source | dynamodb | rds_mysql — EC2 then gets the endpoint + the RDS-managed secret at boot). EC2 takes an
  MGN/golden `ami_id`, or `ami_id=al2023` (fresh AL2023 + Corretto + generated systemd unit). `cicd_target` is
  **codepipeline only** (CodeCommit source) — other CI/CD templates, the `cicd-role` OIDC
  module, EKS, DocumentDB and RDS SQL Server were removed. `tool_execution._resolve_compute` never
  defaults: an unset/invalid/unsupported compute makes `provision_infrastructure` / `generate_cicd` return
  `status:error`. Each compute gets its module, a CodePipeline wired to it (ECS deploy / CodeDeploy revision from
  `cicd/codedeploy/` / Lambda `update-function-code` in CodeBuild) and only its own CD template. `validate_deployment` fails `compute_module_present` when
  the IaC lacks the approved compute and runs live checks per compute. Adding a compute means: Terraform module
  + CodePipeline templates + `_COMPUTE_MODULES`/`_COMPUTE_FOR_STRATEGY` + the three strategy-option mirrors.
- **No third-party tools.** AMF ships no third-party scanner or SaaS integration: no Trivy/Semgrep stages in the
  generated buildspecs, no Neo4j, no ModelizeIT import, and **source repos are AWS CodeCommit only** (task-role
  credential helper; no Git token anywhere). `AWS/security-issue-fixer` needs scanner findings, so it is not
  recommended and the workbench skips it. Security scanning is customer-owned: README "Security scanning"
  (Amazon Inspector). Don't reintroduce a third-party tool without an approved spec.
- **Transformation catalog is live.** The Assessment checklist = `GET /workbench/transformations` (`atx custom
  def list`, AWS-managed only, cached `ATX_LIST_CACHE_TTL_SECONDS`); `frontend/src/workflow/transformCatalog.ts`
  only adds categories/labels. Never hardcode TD names that `atx custom def list` doesn't return.
- **Deterministic over LLM for critical logic.** The Harness ignores "MUST call" prompt instructions, so
  anything correctness-critical is done in code/ASL, not the prompt: strategy/rehost gating (`tool_documents.store_analysis_result`
  zeroes transformations for rehost/close-out), risk scoring, infra + CI/CD generation and repo commits (ASL invokes
  `tool_execution` directly), terraform validate+plan (worker `MODE=infra`), the network-exposure policy
  (`core/exposure_policy.py`; public exposure only with `expose_publicly`), validation, gate decision summaries,
  and the phase pre-hooks/post-checks.
- **S3 execution artifacts** use a uniform layout: `applications/{app_id}/execution/{transformation,infrastructure,cicd}/`.
  Templates that get committed live under `backend/resources/templates/`, each with a `templates.json` manifest;
  `setup.sh` syncs them to `s3://…/config/templates/`.
- **Everything is Terraform** except the TF-state S3 bucket + `{prefix}_tf_lock` DynamoDB table
  (bootstrapped imperatively by `setup.sh`).
- Deploy model: **cloud data/agents via Terraform; backend+frontend run locally for dev** (`start.sh`). Hosted UAT
  mode: `AWS_REGION=us-east-1 ENABLE_PUBLIC_HOSTING=true ADMIN_EMAIL=… PORTAL_DOMAIN_NAME=… PORTAL_ACM_CERTIFICATE_ARN=… [GRAPH_BACKEND=dynamodb|neptune|none]
  [CLOUDFRONT_ALLOWED_CIDRS=a.b.c.d/32] scripts/setup.sh uat` → ECS API service + **internal** ALB (private subnets, CloudFront VPC origin) + CloudFront on your domain (ACM cert, TLSv1.2_2021; the default cert is refused) + Cognito (+ Neptune
  Serverless for `neptune`). The
  WAF allowlist blocks every other viewer IP; Cognito JWT is still required behind it. No `start.sh` in hosted mode.

## Gotchas (learned the hard way)

- **Platform CMK (`kms.tf`).** DynamoDB, ECR, the ECS/worker/SFN log groups and the Lambda env vars use
  `alias/{prefix}-platform`. The key policy lets account principals use it via DynamoDB/ECR/Lambda (`kms:ViaService`)
  and CloudWatch Logs for this account's log groups, so roles need no kms:* grant. A new encrypted resource type
  needs its service added to that policy.

- The SFN role needs **`bedrock-agentcore:InvokeAgentRuntime`** (not just `InvokeHarness`) for
  `invokeHarness` tasks. The Harness runtime role is scoped to `InvokeGateway` + workload-identity tokens; it would
  need AgentCore Memory actions only if memory were re-enabled. Missing either → `AccessDeniedException` at the first agent phase.
- **The Harness has no long-term memory** (`memory { disabled {} }` in `harness.tf`). Left unset, AgentCore attaches
  a managed SEMANTIC memory under one shared actor, and agents recalled earlier runs' (and other apps') facts as
  current — APP-CLI's Design reported a previous run's Terraform commit as done. Don't re-enable it; facts reach
  phases through the prompt and tools.
- `config.py` derives the state machine name as **`{prefix}-agentcore-workflow`** (no `-v2` suffix) — keep `.env` in sync.
- `BEDROCK_MODEL_ID` must be an **inference profile enabled in the account/region**, or Harness init fails.
- Workflow status: `current_step` gets a `*_review` value **only at an approval gate** (via `invoke_runtime`
  `store_token`); that's what flips the UI to "Awaiting Approval". During a phase it stays "Running".
- **Deploy is off by default.** `enable_deploy` (Terraform, default `false`; `setup.sh` `ENABLE_DEPLOY=true`) gates the
  whole deploy path: without it there is no `{prefix}-deploy-role`, no worker assume-role grant, `deploy_infrastructure`
  returns `skipped`, `validate_deployment` skips live checks, `/workflow/teardown` refuses, and approving
  `execution_review` is an ENGINEER gate with no deploy commit (`settings.deploy_enabled`, `/system/capabilities`).
- **Deploy gate (when enabled).** Approving `execution_review` deploys: `POST /workflow/approve` must carry
  `inputs.deploy_commit` == META `exec_head_commit` (the migration-branch HEAD after CI/CD). The worker applies the
  repo's `infra/` AT that commit with `{prefix}-deploy-role` (PowerUser + `APP-*` IAM **only with the `{prefix}-app-permissions-boundary`**, Admin/FullAccess
  attach and `sts:AssumeRole APP-*` denied; `deploy.tf`; app IaC templates set `permissions_boundary`), only into
  `DEPLOY_ALLOWED_ACCOUNTS` (default: this account), refuses a moved branch, re-checks the exposure policy, then waits
  for the first pipeline run. Failure → back to the gate (≤3). Reject → Execution regenerates. Tear down is separate:
  `POST /workflow/teardown` (Admin, typed app id). Never Gateway-expose deploy/destroy — the agent must not deploy.
- **Wiring precedence:** META field → `detected_*` (repo scan) → default (listed in the execution-gate notes). The
  assessment modal pre-fills the detected values (`frontend/src/workflow/wiring.ts`).
- ATX is long-running (~10–25 min) on ECS via the `waitForTaskToken` callback — expect the workflow to
  pause at `LaunchATX`, not fail.
- Pre-hook contracts live in `invoke_runtime._prehook_phase` **and** the phase's actual input needs must stay
  in sync — if a phase reads a new field, add it to that phase's pre-hook or the phase can still start under-fed.
  A failed pre-hook routes to `MarkFailed` (not an approval pause); the recorded `blockers[]` say what's missing.
- **Run scoping.** A new run resets every run-derived META field (`workflow.RUN_DERIVED_META_FIELDS`) and approvals
  are tied to the run's `execution_arn` (`APPROVAL_AUDIT#` stores it + the gate's inputs). If you add a field that a
  phase derives, add it to that list or it will leak into the next run.
- **Agents get the facts in the prompt, not only via tools.** `ResolveStrategy` returns `app_facts` (confirmed
  strategy + recorded facts) that the Assessment and close-out prompts embed. A failed `get_app_context` (the tool
  role once lacked `dynamodb:BatchGetItem`) made every phase write blind — invented strategies, server counts, owners.
  `get_app_context` now degrades (`machines_error`) instead of failing; the ATX worker fails the callback when linked
  repos analyse nothing (clone failure / 0 files) instead of succeeding empty.
- **Retry comments reach the re-run.** A rejected Assessment / Design / close-out gate's comment is captured from the
  `StepRejected` cause (`reviewer_comment`) and appended to the phase prompt as REVIEWER FEEDBACK. Completeness-gate
  gaps shown on a gate (e.g. compliance, data_classification) can be filled in the approval modal
  (`frontend/src/workflow/gateGaps.ts`) — saved to META and folded into the Retry comment.
- **L2 enrichment keeps every field** (`portfolio.EnrichAppItem`, `extra="allow"`); L2 `machine_ids` replace the ADS
  auto-grouping. Existing apps need their L2 re-uploaded to backfill fields dropped before this fix.
- **Wave plans are deterministic**: `core/wave_planner.py` is a pure module that implements
  `planning.md` (eligibility, bands, dependency order incl. `depends_on`, shared DB, tier_1 spread, Long Tail for
  close-outs). The API and the `tool_planning` Lambda (zipped copy) both run it, so change the rules there, not in the prompt.
- **Wave planning** is on unless `GRAPH_BACKEND=none`; the default `dynamodb` backend derives the graph from the
  table (`db/graph_dynamo.py`), `neptune` (`db/neptune.py`, `neptune.tf`, `ENABLE_NEPTUNE=true`) is an
  IAM-auth openCypher projection of the table, re-synced on `NEPTUNE_SYNC_TTL_SECONDS` (`POST /graph/sync` forces it).
- The worker task is in **public subnets with no NAT** — Lambda-launched workers use `assignPublicIp=ENABLED`
  (`ECS_ASSIGN_PUBLIC_IP`). The API launches no ECS tasks itself.

## Deferred — do NOT assume these are done

**Portal/API authentication** is available via Cognito (`infra/environments/agentcore/cognito.tf`,
`enable_cognito`) — the UAT deploy turns it on and sets `DEV_AUTH=false`; local dev keeps `DEV_AUTH=true`.
**MCP Gateway auth** is `AWS_IAM` (`var.gateway_authorizer_type`; the Harness signs via `outbound_auth.aws_iam`; `NONE` is
rollback only). Still deferred: **Secrets Manager hardening**, and
**observability dashboards/alarms** (CloudWatch Transaction Search is on by default via `enable_transaction_search`).
IAM is deliberately broad in places (noted in the `.tf` comments).

## Non-negotiables (every change)

1. **No hardcoding: move it to config.** Any literal tunable, resource name/id, ARN, account ID, region, URL, model ID,
   timeout, threshold, retry count or magic number that you write *or find in code you touch* moves to config in
   the same change:
   - backend/worker: a `Settings` field in `backend/src/config.py` (env-overridable; default = today's value);
   - tool Lambdas: an env var set in `infra/environments/agentcore/lambdas.tf`, read once at module load;
   - Terraform: a variable;
   - frontend: `GET /system/capabilities` or `GET /infrastructure/`.

   Domain contract constants (strategy names, `db/keys.py` prefixes, ATX TD names from the live catalog) are not
   tunables. Tests may use literals.
2. **Security and performance first, before a spec is finalized.** A T1/T2 spec can't be put up for approval until
   its **Security review** and **Performance review** sections are filled in against the *current* code:
   - Security: authn/authz (`require_role`), input validation, injection/SSRF/path traversal, secrets handling,
     IAM least privilege, network exposure.
   - Performance: DynamoDB access pattern (GSI1, pagination, no scan, no N+1), blocking calls in `async` handlers,
     timeouts/retries, payload size, caching, Lambda cold start, frontend polling/bundle.

   Write "none: <reason>" rather than leaving either section blank. CI fails a new spec without both sections.

## How work is specified

Classify every change before you start:
- **Fix** (bug, polish, test, no-behaviour refactor): a clear commit message.
- **Feature** (new capability/config/flag, gate or phase behaviour, API/ASL/IaC contract change): describe the
  design in the pull request, including a **Security review** and a **Performance review** ("none: <reason>"
  if not applicable), and wait for review before merging.

**No customer or company names** anywhere in the repo; use app IDs. All CI checks are blocking (ruff pinned, tsc,
terraform fmt/validate, backend + lambda-src pytest, vitest).

## Security model

- **RBAC:** every `/api/v1` route has a role (router-level `require_role(VIEWER)`, ENGINEER on mutations, ADMIN for
  teardown, skills upload, approving `execution_review` = deploy, and the `expose_publicly`/`validation_write_tests` fields).
  `backend/tests/unit/test_rbac_routes.py` fails any new route without a role. A Cognito user with no group is a VIEWER.
- **Hosted guard:** the API refuses to start in hosted mode (`MODE=api` / `ENABLE_PUBLIC_HOSTING`) with `DEV_AUTH` on or
  without Cognito pool + client id (`auth/startup_guard.py`); Terraform has the same precondition on the API service.
- **Untrusted inputs:**
  - Repo build code (mvn/gradlew/pip/pytest/ruff) goes through `worker/sandbox.py` `run_untrusted`. It runs as
    `builduser` with `sandbox_env()`, so it has no AWS/SFN/token env and can't read `atxuser`'s environ or home.
    **Never** call `subprocess.run` on repo-controlled commands directly.
  - Git runs only through `worker/gitcmd.py` `run_git`, against AWS CodeCommit only (clone URLs are validated in
    `worker/argsafe.py`, the API and the Assessment pre-hook); it authenticates with the task role, so no token exists.
  - The smoke probe only touches the app's own table.
  - The agent's `update_app_field` is an allowlist.
  - `PUT /portfolio/apps` refuses server-only fields.
- **Output:** report HTML is escaped and served with a `default-src 'none'` CSP, iframes are `sandbox=""`, and the portal
  has a CloudFront response-headers policy (`var.portal_csp`).

## Do / Don't

- **Don't** reintroduce `backend/src/agents/` (Strands) or `agentcore/runtimes/` — single Harness only.
- **Don't** use `table.scan`; use `GSI1` queries. **Don't** hardcode account IDs/ARNs/resource names/tunables —
  add a `Settings` field in `config.py` (or a Terraform-set env var for Lambdas).
- **Do** run `ruff` + `pytest` (backend and lambda-src) and `pnpm exec tsc --noEmit` + `pnpm test` (frontend) before finishing.
- **Do** keep the worker decoupled from the API layer (use `db/repository.read_full_app`, not `api.*`).
- **Do** commit only when asked; work on a feature branch (never the default branch).
