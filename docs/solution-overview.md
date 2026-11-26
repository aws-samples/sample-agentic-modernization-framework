# AMF solution overview

> AMF is sample code (a proof of concept), not a supported AWS product or service. See the
> README disclaimer and [SECURITY.md](../SECURITY.md).

## What AMF is

AMF (Agentic Modernization Framework) is a reference implementation of an end-to-end agentic workflow
on AWS, applied to application modernization. One **Amazon Bedrock AgentCore Harness** reasons inside
each phase and calls Lambda-backed **MCP Gateway** tools. **AWS Step Functions** owns the journey and
its human approval gates. **AWS Transform** runs code analysis and transformation on Amazon ECS Fargate.

## The problem it illustrates

Moving an application portfolio to AWS involves discovery, assessment, design and execution work that
is often manual, spread across teams, and hard to keep consistent. Knowledge is lost between phases and
documents drift from what is actually built.

## How AMF approaches it

The agent does the analysis and writing inside each phase; everything correctness-critical is
deterministic code:
- pre-hooks and post-checks around each phase
- strategy gating
- template-driven infrastructure and CI/CD generation
- `terraform validate` and `plan` (deploy is off by default; when enabled, only after an Admin approves the exact commit)
- a network-exposure policy
- deployment validation

Engineers approve at **7 gates** (discovery, assessment, design, execution, validation, cutover,
handover). Each gate shows a decision summary computed in code.

**Design choices:**
- **One governed agent:** a single Harness with 9 phase playbooks (`backend/resources/skills/*.md`), composed into its prompt by Terraform. Playbook changes are reviewed like code and take effect on `terraform apply`.
- **Deterministic over LLM:** pre-hooks fail a phase fast when its inputs are missing. Post-checks catch contradictions (for example, an assessment written for the wrong strategy, or a design that conflicts with the approved compute, database, CI/CD, account or CIDR), re-run the phase once with the blockers in the prompt, and fail it after that.
- **Template-driven infrastructure:** standardized Terraform modules (VPC, ECS Fargate, Lambda, EC2, RDS/Aurora, DynamoDB, S3, CodePipeline) are selected from the confirmed targets, validated and planned in a worker, then committed to a migration branch. The agent does not write the IaC.
- **Facts in the prompt:** the confirmed strategy and the app's recorded facts are embedded in each phase prompt, so a failed tool call can't leave the agent working blind.
- **Dependency-aware wave planning:** the dependency graph is derived from the DynamoDB table by default; Amazon Neptune is optional.
- **AWS Transform integration:** comprehensive code analysis at Assessment, and approved transformations (Java, Spring Boot, AWS SDK, …) at Execution.

## How it works

1. Upload L1 discovery data (AWS Application Discovery Service exports). Servers are auto-grouped into applications. L2 enrichment adds owners, repos and constraints.
2. Start the workflow. Discovery links infrastructure, recommends a 7R strategy and computes a deterministic risk score (0–100). Retire, retain and repurchase go straight to a close-out handover.
3. Assessment runs AWS Transform code analysis, then the Harness writes the assessment summary. The engineer confirms target compute, database, CI/CD, AWS account, VPC CIDR and the transformations.
4. Design generates the migration design document. It is checked against the approved selections before the gate.
5. Execution runs **sequentially**: code transformation (skipped for rehost and relocate), database migration plan, Terraform generation with `terraform validate` + `plan`, then CI/CD generation. Everything is committed to a migration branch.
6. Validation is a deterministic check of the IaC, the CI/CD coverage gate and the transformation outcome, plus the live pipeline / ECS / ALB state when deploy is enabled.
7. Cutover (Go/No-Go), then handover documentation.

**Availability:** AMF deploys to an AWS account with `scripts/setup.sh [prefix]` (Terraform). An optional hosted mode adds CloudFront, an internal ALB, the ECS API and Cognito.

---

## Solution Features

| Category | Feature | Implementation |
|----------|---------|----------------|
| **Discovery** | L1 data ingestion (AWS ADS) | Upload → batch ingest → auto-grouping |
| **Discovery** | Automatic application grouping | Hostname pattern analysis; L2 `machine_ids` override |
| **Discovery** | Language/framework detection from repos | Worker repo scan (`detected_*`) |
| **Discovery** | 7R strategy recommendation | Rule engine + confidence scoring, confirmed or overridden at the gate |
| **Discovery** | Migration risk score | Deterministic `compute_risk_score` (0–100) |
| **Assessment** | ATX code analysis | Clone → ATX CLI on ECS Fargate → S3 reports |
| **Assessment** | Target compute / DB / CI-CD confirmation | Valid options per strategy (`core/strategy_options.py`) |
| **Assessment** | Transformation step selection | Pre-checked from code findings; cleared for rehost / close-out |
| **Assessment** | Consistency post-check | Wrong-strategy assessment → one re-run → fail |
| **Design** | Auto-generated design document | Harness + design playbook |
| **Design** | Contradiction post-check | Against approved compute / DB / CI-CD / account / CIDR / strategy |
| **Execution** | Sequential, deterministic sub-steps | Transform → DB plan → IaC + terraform plan → CI/CD |
| **Execution** | Standardized Terraform modules | `backend/resources/templates/terraform/` |
| **Execution** | Network-exposure policy | Public exposure only when explicitly requested |
| **Execution** | CI/CD generation | AWS CodePipeline + CodeBuild (CodeCommit source; ECS deploy, CodeDeploy for EC2, Lambda update-function-code) |
| **Validation** | Deployment verification | `validate_deployment` (deterministic) |
| **Gates** | 7 HITL gates with decision summary | What changed / risk / approve-reject-retry consequences; retry comments reach the re-run |
| **Handover** | Runbook / close-out document | Fact-checked against the app record |
| **Wave Planning** | Dependency graph | DynamoDB-derived (default) or Amazon Neptune |
| **Wave Planning** | Shared DB detection + complexity scoring | Port-based analysis; tier × machines × dependencies |
| **Governance** | Approval audit trail | Runs and gate outcomes per app, from Step Functions history |
| **Platform** | One Harness, 9 playbooks, 5 MCP tool targets | `harness.tf`, `gateway-tools.tf` |
| **Platform** | Single DynamoDB table + single S3 bucket | Single-table design with GSI1 |
| **Platform** | Auth | Cognito JWT + RBAC (hosted); dev bypass (local) |

---

## Frequently Asked Questions

**Q: What is AMF?**
A: An AI-powered framework that automates application migration and modernization to AWS. One AgentCore Harness reasons within each phase. Step Functions orchestrates the journey. Deterministic code enforces anything correctness-critical. Humans approve at every major milestone.

**Q: How is this different from AWS Migration Hub?**
A: Migration Hub tracks status; AMF does the work. It produces assessments and design documents, transforms code through AWS Transform, and generates and commits infrastructure and CI/CD, with a human gate at every decision.

**Q: What AWS services does AMF use?**
A:
- **Core:** Step Functions (orchestration), Bedrock AgentCore Harness and MCP Gateway (`us.anthropic.claude-sonnet-4-6`), Lambda (7 MCP tool targets plus the invoke-runtime bridge), ECS Fargate (ATX worker, terraform plan, API in hosted mode), DynamoDB, S3, ECR, CloudWatch / X-Ray Transaction Search, AWS Transform.
- **Optional:** Cognito, CloudFront, ALB (hosted), and Amazon Neptune for the graph.

**Q: How does the skills system work?**
A: There is one Harness and 9 markdown playbooks (discovery, risk_scoring, grouping, assessment, planning, design, execution, validation, handover). Terraform composes them into the Harness system prompt. A playbook change takes effect after `terraform apply`. Uploading through the Settings UI only stages the files.

**Q: What if the AI makes a wrong recommendation?**
A: The checks are layered:
- Pre-hooks stop a phase that lacks its inputs.
- Post-checks catch contradictions and re-run the phase once.
- Every phase ends at a human gate. Engineers can override the strategy and targets, reject with a comment that is fed into the re-run, or cancel the run.

The AI proposes; humans decide.

**Q: How does infrastructure provisioning work?**
A: The `provision_infrastructure` tool selects standardized Terraform modules from the confirmed `target_compute`, `target_database` and `cicd_target`, and fills their variables from the app record. A worker (`MODE=infra`) then runs `terraform validate` and `plan`. The exposure policy is checked, and the code is committed to the app's migration branch. AMF never runs `terraform apply`; engineers apply manually or through the generated pipeline.

**Q: Can this handle a large portfolio?**
A: The data model is built for portfolio scale. L1 discovery batch-ingests thousands of servers, wave planning works across the portfolio, and Step Functions runs one execution per app. Concurrency controls across simultaneous runs are on the roadmap.

**Q: Is there any customer-specific code in the framework?**
A: No. Infrastructure is parameterized through Terraform variables, and all resource names derive from a project prefix. Secrets come from Secrets Manager or `.env` (local only).

**Q: What data sources are supported for L1 discovery?**
A: AWS Application Discovery Service (ADS) exports, uploaded through the UI. Synthetic sample data can be kept locally under `input/` (not tracked in git).

**Q: How long does a typical workflow take per application?**
A: Each long-running step has a hard timeout in the state machine (`infra/step-functions/migration-workflow.v2.asl.json`): AWS Transform code analysis 2 h (`LaunchATX`), code transformation 4 h (`LaunchTransformation`), `terraform validate` + `plan` 30 min (`InfrastructureValidate`) and deploy 2 h (`Deploy`); each approval gate waits up to 7 days. These are upper bounds, not typical durations. In our own sandbox test runs, code analysis finished in roughly 10–25 minutes and each Harness phase in a few minutes; your times depend on repository size and the transformations selected. Human approvals usually dominate the wall-clock time.
