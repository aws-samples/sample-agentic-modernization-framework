# Cost & Token Consumption

These are ballpark figures to size a run. Where a number was measured, it says so. Everything else is an **estimate** derived from the code and config, so re-measure in your account before quoting it. This page doesn't list prices; use current AWS pricing for your region.

## Where the cost goes

| Component | Driver | Notes |
|---|---|---|
| **AWS Transform (ATX)** | Agent minutes per analysis or transformation | Runs on ECS Fargate through `waitForTaskToken`. `LaunchATX` times out at 2 h; `LaunchTransformation` at 4 h. |
| **Bedrock (AgentCore Harness)** | Input and output tokens per Harness invocation | Model: `us.anthropic.claude-sonnet-4-6` (`BEDROCK_MODEL_ID`, which must be an inference profile enabled in the account). |
| **ECS Fargate** | Worker task vCPU-hours | ATX worker, `MODE=infra` (terraform validate + plan, 30 min timeout), workbench plans. When public hosting is on, the API service also runs continuously. |
| **Lambda** | Invocations × duration | 5 MCP tool Lambdas (30–120 s timeouts) plus `invoke-runtime` (900 s, which waits on the Harness for chat and pre/post checks). |
| **Step Functions** | State transitions | Standard workflows. The journey has 51 states. Approval gates wait up to 7 days. Wait time costs nothing; transitions do. |
| **DynamoDB / S3** | On-demand requests + storage | One table, one data bucket. Negligible at pilot scale. |
| **CloudWatch** | Log ingestion + retention | Step Functions log groups keep 30 days. ECS / Lambda logs. |
| **X-Ray Transaction Search** | Spans indexed | On by default (`enable_transaction_search = true`, `transaction_search_indexing_percentage = 100`). Lower the percentage, or turn it off, to cut cost. |
| **CloudFront + ALB** | Hosted UAT only (`enable_public_hosting`) | Fixed hourly ALB cost and CloudFront requests. Off by default; local dev has none of these. |
| **Cognito** | Monthly active users | Only with `enable_cognito`. |

## ATX agent minutes (measured, per run)

Measured on `aws-appconfig-java-sample` (Spring Boot 2.x, Java 8):

| Analysis type | Min | Max | Avg | Notes |
|---|---|---|---|---|
| **Comprehensive Codebase Analysis** | ~94 | ~94 | **~94 min** | Full code documentation (30+ files) |
| Modernization Readiness Analysis | ~99 | ~180 | ~148 min | Not run by the journey |
| Agentic Readiness Analysis | ~75 | ~115 | ~94 min | Not run by the journey |
| Portfolio Modernization | ~95 | ~136 | ~108 min | Not run by the journey |

The journey runs only `AWS/comprehensive-codebase-analysis` (`ATX_CODE_ANALYSIS_TD_NAME`) at Assessment. Transformations run at Execution, and only for the ATX steps approved at the Assessment gate. They are skipped for rehost and relocate. Agent minutes are compute time, not wall-clock time; one analysis has typically taken 10–25 min wall clock.

## Harness tokens (estimate)

Every Harness invocation sends the full system prompt: the base prompt (about 1.7 K characters) plus the 9 skill playbooks composed by `harness.tf`.

| Item | Size |
|---|---|
| Skill playbooks (`wc -c backend/resources/skills/*.md`) | **~120 K characters** (design 34 K, assessment 30 K, execution 14 K, discovery 12 K, planning 9 K, handover 6 K, grouping 5 K, validation 5 K, risk_scoring 5 K) |
| System prompt, approx. tokens (~4 chars/token) | **~30–31 K input tokens per invocation**, before any app facts, tool results or conversation |
| Facts and tool results per phase | Varies. Prompt facts are capped (`invoke_runtime`); tool results (app context, analysis, design) add to it. |

**Harness invocations per journey:**
- Day-1 run: `Discovery`, `Assessment`, `Design`, `DatabaseMigration`, `Cutover`, `Handover`. That is 6, plus up to 1 extra each for Assessment and Design when a post-check re-runs them, plus any reviewer "Retry".
- Close-out runs (retire / retain / repurchase): `Handover_CloseOut` only, plus at most 1 re-run.
- Each Harness phase makes several model turns, because it calls tools. Each turn re-sends the growing context.

The system prompt is fixed, so it dominates input tokens. Trimming the skill playbooks is the most effective token lever. Infrastructure generation, CI/CD generation, validation and the pre/post checks are deterministic Lambdas and use no tokens.

**Chat advisor.** Each message is a Harness invocation through `invoke-runtime`, with the advisor persona prepended. It costs the same order of input tokens as a phase turn, not the ~2 K once estimated for the old engine.

## End-to-end duration (estimate per app)

| Segment | Duration |
|---|---|
| Discovery (Harness + risk score + post-check) | minutes |
| Assessment: ATX code analysis | 10–25 min (dominant) |
| Assessment / Design Harness phases | minutes each |
| Execution: transformation (if any), DB plan, IaC + terraform plan, CI/CD | ATX transform up to 4 h; terraform plan up to 30 min |
| Human gates | Unbounded (7-day timeout per gate) |

## Cost levers

- Remove the ATX transformations you don't need at the Assessment gate (each one is a separate ATX run).
- Keep skill playbooks tight. They are re-sent on every Harness turn.
- Lower `transaction_search_indexing_percentage` outside of debugging.
- Leave `enable_public_hosting` off unless you need a shared portal. Local dev (`scripts/start.sh`) has no ALB or CloudFront cost.
- Run `scripts/cleanup.sh` between demos to drop stale data, and `scripts/cleanup.sh infra` to tear an environment down (destructive).
