# AMF Architecture

AMF is a single **Amazon Bedrock AgentCore** deployment:

- One declarative **Harness** does the agent reasoning for each phase. It calls **MCP Gateway
  tools** (5 Lambda targets) to act on state.
- **AWS Step Functions** orchestrates the journey and the human approval gates.
- Correctness-critical steps are deterministic Lambda calls made directly by the state machine.
- Long-running **AWS Transform (ATX)** work and `terraform plan` run on **ECS Fargate**.

## System diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Frontend — React + CloudScape (Vite)                                     │
└───────────────────────────────────┬──────────────────────────────────────┘
                                     │ REST /api/v1
┌───────────────────────────────────▼──────────────────────────────────────┐
│  Backend API — FastAPI (Python 3.12), local (start.sh) or ECS behind ALB  │
│  portfolio · workflow · analysis · design · waves · chat · governance ·   │
│  graph · iac · notes · skills · system · infrastructure …                 │
└───────────────────────────────────┬──────────────────────────────────────┘
                                     │ StartExecution / SendTask*
┌───────────────────────────────────▼──────────────────────────────────────┐
│  Step Functions                                                            │
│   {prefix}-agentcore-workflow   (journey, JSONata, 51 states)              │
│                                                                            │
│  Agent phases:  bedrockagentcore:invokeHarness                             │
│  Pre-hooks / post-checks / gates:  lambda:invoke → invoke-runtime          │
│  Infra · CI/CD · validation:       lambda:invoke → tool-execution          │
│  Risk score:                       lambda:invoke → tool-documents          │
│  ATX · terraform plan:             lambda:invoke.waitForTaskToken          │
└──────────┬─────────────────────────────────────────────┬──────────────────┘
           │ invokeHarness                                │ ecs:RunTask
┌──────────▼──────────────────────┐        ┌──────────────▼──────────────────┐
│  AgentCore Harness (Claude)      │  MCP   │  ECS Fargate worker              │
│  + MCP Gateway, 5 targets:       ├───────►│  MODE=code-analysis | workbench │
│   portfolio · strategy ·         │  tools │       | infra                    │
│   planning · documents ·         │        │  → S3 + DynamoDB                 │
│   execution                      │        │  → SendTaskSuccess/Failure       │
│                                  │        └──────────────────────────────────┘
└──────────┬───────────────────────┘
      ┌────▼─────────────┐     ┌──────────────────┐
      │ DynamoDB          │     │ S3                │
      │ single table      │     │ one data bucket   │
      └───────────────────┘     └──────────────────┘
```

## Journey — `{prefix}-agentcore-workflow`

Source: `infra/step-functions/migration-workflow.v2.asl.json`, deployed by
`infra/environments/agentcore/step-functions.tf` via `templatefile`. The template has four
placeholders: `HarnessArn`, `InvokeRuntimeFn`, `ToolExecutionFn` and `ToolDocumentsFn`.

1. **Discovery**
   1. `PreHook_Discovery`/`PreCheck_Discovery` require the app, `app_name` and a linked repo.
   2. `Discovery` runs the Harness.
   3. `Score_Discovery` computes a deterministic risk score with `compute_risk_score`.
   4. `Validate_Discovery`/`Check_Discovery` run the post-check.
   5. `Approval_Discovery` is the `discovery_review` gate.
2. **ResolveStrategy → StrategyChoice.** Retire, retain and repurchase (`close_out`) take the
   close-out branch: `Handover_CloseOut` → `Validate_CloseOut` → `Check_CloseOut` →
   `Approval_CloseOut` (`handover_review`) → `MarkCompleted`.
3. **Assessment**
   1. `PreHook_Assessment` requires a strategy and a clonable repo URL.
   2. `LaunchATX` runs ATX code analysis on ECS (`waitForTaskToken`).
   3. `Assessment` runs the Harness.
   4. `Validate_Assessment`/`Check_Assessment` fail the post-check if the assessment summary
      contradicts the confirmed strategy. The phase re-runs once with the blockers, then goes to
      `MarkFailed`.
   5. `Approval_Assessment` is the `assessment_review` gate.
4. **Design**
   1. `PreHook_Design` requires a current-run `assessment-summary`.
   2. `Design` runs the Harness.
   3. `Validate_Design`/`Check_Design` compare the design against the approved compute, DB,
      CI/CD, account, CIDR and strategy. The phase re-runs once, then goes to `MarkFailed`.
   4. `Approval_Design` is the gate.
5. **Execution**
   1. `PreHook_Execution` requires valid targets, a 12-digit account, an RFC1918 CIDR and a repo.
   2. `TransformChoice` sends rehost/relocate past the transform. Other strategies run
      `LaunchTransformation` (ATX, `waitForTaskToken`).
   3. `DatabaseMigration` runs the Harness and produces a plan only.
   4. `InfrastructureProvisioning` calls `provision_infrastructure`. It renders Terraform from the
      template library, applies the exposure policy, and commits to a migration branch.
   5. `InfrastructureValidate` calls `run_terraform_plan`, which runs worker `MODE=infra`
      (`validate` + `plan`, never `apply`).
   6. `CICDSetup` calls `generate_cicd` (generate + commit).
   7. `Approval_Execution` is the gate.
6. **Validation.** `Validation` calls `validate_deployment`, a deterministic check, not the Harness.
   `Check_Validation` passes on `pass` or `pass_with_warnings`. `Approval_Validation` is the gate.
7. **Cutover → Handover.** `Cutover` runs the Harness, then `Approval_Cutover`. `Handover` runs the
   Harness, then `MarkCompleted` → `Complete`.

**Gates.** There are 7 approval states. Each stores a task token through `invoke_runtime`
`mode=store_token` and times out after 7 days.

- `store_token` attaches a deterministic `decision_summary` (changes, risk, approve/reject/retry
  consequences, contradictions, warnings), which the approval modal renders.
- Reject re-runs the phase for Discovery, Assessment, Design and close-out. The reviewer comment
  is appended to the prompt as REVIEWER FEEDBACK.
- Reject goes to `MarkFailed` for Execution, Validation and Cutover.

`current_step` is a `*_review` value only while a gate is open.

**Run scoping.** A new run resets the run-derived META fields (`workflow.RUN_DERIVED_META_FIELDS`).
Approvals are tied to the run's `execution_arn`.

## Components

- **Harness** (`harness.tf`). Its system prompt is a base prompt plus the `harness_skill_files`
  playbooks: discovery, risk_scoring, grouping, assessment, planning, design, execution,
  validation and handover. A skill edit takes effect only after `terraform apply`.
  Facts the agent must not invent are embedded in the prompt (`app_facts` from `ResolveStrategy`).
  The agent does not get them only through tools.
- **Tool Lambdas** (`lambda-src/tool_*.py`): portfolio, strategy, planning, documents and
  execution. `tool_execution` also registers ASL-only tools that are not on the
  Gateway: `run_terraform_plan`, `validate_deployment`, `run_ci_fix`, `deploy_infrastructure` and
  `destroy_infrastructure`.
- **invoke-runtime Lambda** (`lambda-src/invoke_runtime.py`). The bridge between Step Functions,
  the chat API and the Harness. Deterministic modes are `store_token`, `resolve_strategy`,
  `prehook_phase`, `validate_phase` and `mark_completed`. Any other mode
  invokes the Harness, for example `chat` (which uses a stable session id).
- **ECS worker** (`backend/src/worker/`). `code-analysis` and `workbench` run ATX. `infra` runs
  terraform validate+plan (`terraform_runner.py`).
  All of them call back via `sfn_callback.py` (idempotent). The same image also serves the hosted
  API (`{prefix}-api-task`).
- **Backend API** (`backend/src/api/v1/`). Covers portfolio CRUD and enrichment, workflow
  start/approve/cancel/status, analysis and design reads, wave planning, IaC, chat,
  governance, notes and skills. `GET /system/capabilities` returns feature flags plus
  region, harness id and model. `GET /infrastructure/` returns the configured resource names, and
  the Settings tab shows them.

## Data model — DynamoDB single table

Key builders live in `backend/src/db/keys.py`. Use them instead of hand-writing keys. `GSI1`
(`GSI1PK = TYPE#<entity>`) lists all items of a type. The table is never scanned.

| Entity | PK | SK |
|---|---|---|
| Application | `APP#<id>` | `META` (GSI1 `TYPE#APP`) |
| Machine / Repo / Owner / Software | `APP#<id>` | `MACHINE#` / `REPO#` / `OWNER#` / `SW#` |
| Machine connection edge | `MACHINE#<id>` | `CONN#…` |
| Workflow state | `APP#<id>` | `WORKFLOW#ACTIVE` (GSI1 `TYPE#WORKFLOW`) |
| Approval token / audit | `APP#<id>` | `TASK_TOKEN#<step>` / `APPROVAL_AUDIT#<ts>` |
| Analysis / Design | `APP#<id>` | `ANALYSIS#<type>#<ts>` / `DESIGN#<v>` |
| Migration plan / workbench plan | `APP#<id>` | `MIGRATION_PLAN#…` / `PLAN#<id>` |
| Execution records / tool-call traces | `APP#<id>` | `EXECUTION#<kind>#<ts>` / `TOOLCALL#<phase>#<ts>#<tool>` |
| ATX job (Lambda-launched) | `JOB#<id>` | `META` (GSI1 `TYPE#JOB`) |

Workflow status is read live from Step Functions, which is the source of truth. The DynamoDB
`status` is only a cache.

## S3 layout (`{prefix}-data-{account}-{region}`)

```
applications/<app_id>/analysis/<type>/<run_id>/…     # ATX + analysis reports
applications/<app_id>/design/…                       # design document versions
applications/<app_id>/execution/{transformation,infrastructure,cicd}/
applications/<app_id>/validation/ · handover/
chat-sessions/<session_id>.json
config/templates/                                    # template library (synced by setup.sh)
discovery/ · notes/
```

## Graph / wave planning

Wave planning (`core/wave_planner.py`, `/waves`, `/graph`) is enabled unless `GRAPH_BACKEND=none`.
The default `dynamodb` backend derives the dependency graph from the portfolio table
(`db/graph_dynamo.py`); `neptune` serves the same contract from Amazon Neptune.
The frontend hides Wave Planning when `/system/capabilities` reports it disabled.

## Deferred

- Secrets Manager hardening.
- CloudWatch dashboards and alarms. Transaction Search is on by default.
- Least-privilege IAM review.
- Day-2 operations (post-migration signal scanning and follow-up runs).

Portal auth (Cognito + RBAC) is available and is enabled for UAT.
