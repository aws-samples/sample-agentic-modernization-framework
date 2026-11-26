---
name: execution
description: Execution agent skill — drives the actual migration work using confirmed selections from assessment (target_compute, cicd_target, transformation_steps). Runs parallel sub-tasks for code transformation, infrastructure, database migration, networking, and CI/CD setup.
metadata:
  type: skill
  phase: execution
  agent: execution
---

# Skill: Migration Execution

## When this skill fires

- Design document V1 approved (with Account ID + VPC CIDR provided by user)
- Workflow enters Phase_Transform in Step Functions
- Confirmed inputs available: `target_compute`, `cicd_target`, `transformation_steps`

## Inputs (from assessment approval + design doc)

| Field | Source | Example |
|-------|--------|---------|
| `target_compute` | Assessment approval | ecs_fargate, lambda (replatform/refactor); ec2 (rehost/relocate only) |
| `cicd_target` | Assessment approval | codepipeline (the only supported target; CodeCommit source) |
| `transformation_steps` | Assessment approval | [AWS/java-version-upgrade, AWS/spring-boot-version-upgrade] |
| `target_account` | Design V1 user input | 123456789012 |
| `vpc_cidr` | Design V1 user input | 10.0.0.0/16 |
| `domain_name` | Design V1 user input | app.example.com (or "none") |
| Design Document | S3 | Full architecture spec (all 12 sections) |

## Who does what — read first

Step Functions runs code transformation, infrastructure, Terraform validation and CI/CD
**deterministically** (ASL states `LaunchTransformation`, `InfrastructureProvisioning`,
`InfrastructureValidate`, `CICDSetup` invoke the tool Lambdas directly and commit to the repo).
The Harness is invoked ONLY for the **Database Migration** sub-step (`DatabaseMigration` state).

**Do NOT call** `start_code_transformation`, `provision_infrastructure`,
`run_terraform_plan`, `generate_cicd` or `commit_cicd_to_repo` from the Harness — the workflow
already ran them; calling them again re-renders artifacts and pushes duplicate commits. Sections
1, 2 and 5 below describe what the workflow does so you can reference the results.

## Execution Sub-Tasks

The following run via Step Functions:

### 1. Code Transformation — run by the workflow (replatform/refactor only)
Read `transformation_steps` from app META (confirmed at assessment):
```
For each step in transformation_steps:
  atx custom def exec -n {step_name} -p {repo_path} -x -t --do-not-learn
```

Steps execute in ORDER within this task (sequential ATX runs):
- Clone repo; work on the migration branch **`migration/<app_id lowercased>`** (created from the
  repo's default branch when missing). Nothing is ever pushed to the default branch.
- Run ATX transformation. The derived goal comes from the **TD name first** (java-version-upgrade →
  "Java <target_version|21>", python-version-upgrade → "Python …", java-aws-sdk-v1-to-v2 → SDK v2
  migration); the app language is only a fallback for language-neutral TDs. A TD written for another
  language than the app's (e.g. python-version-upgrade on a Java app) is **skipped** and recorded in the
  plan's `skipped_steps`.
- Validate: ATX `validation_summary.md` OVERALL STATUS + the build command
- Push to the migration branch
- Next transformation step

Per-step status is honest: `completed` = changes pushed AND build verified (validation COMPLETE with a
build command); `partial` = changes pushed but unverified (validation PARTIAL/missing or no build
command); `no_change` = ATX produced no diff (e.g. already on the target version). Plan status is
`completed_with_errors` when the synthesis report failed (e.g. Bedrock AccessDenied), never a clean
`completed`.

Output: transformed code on the migration branch, ATX reports in S3, per-step outcomes on PLAN#.

### 2. Infrastructure Provisioning (generate + stage) — run by the workflow
The workflow calls **`provision_infrastructure`**. It uses the standardized Terraform module library and the
design doc Section 3 to GENERATE (never apply) infrastructure-as-code for the confirmed `target_compute`:
- VPC + subnets across `az_count` AZs (from `vpc_cidr`; default 2, or META `dr_availability.availability_zones`)
- Compute by `target_compute`:
  | target_compute | Module |
  |---|---|
  | ecs_fargate | `ecs-fargate` (cluster, service, ALB, ECR, logs) |
  | lambda | `lambda` (function + HTTP API + artifact bucket; CI uploads the package, CD runs `update-function-code --publish`) |
  | ec2 (rehost/relocate only) | `ec2` — launch template from a parameterized `ami_id` (AWS MGN / golden AMI, **no containers**), ASG in private subnets, ALB + target group + health check, SGs, SSM instance profile, artifact bucket, CloudWatch log group + alarms |
- Database by `target_database`: `rds` module for rds_postgresql / rds_mysql /
  aurora_postgresql / aurora_mysql (KMS-encrypted, credentials in Secrets Manager via
  `manage_master_user_password`, `db_instance_class` + `db_multi_az` vars); `dynamodb` module for
  dynamodb; nothing for same_as_source / none. `db_multi_az` = explicit
  multi_az flag → uptime_sla ≥ 99.95% → multi_region DR posture → false.
- CodePipeline module (the only CI/CD target; needs a CodeCommit repo), for every compute: ecs_fargate →
  ECS deploy action; ec2 → CodeDeploy in-place one-at-a-time behind the ALB; lambda → CodeBuild deploys the
  function (`update-function-code --publish`). It runs the committed `cicd/buildspec.yml` (single source of
  truth) and sources the repo's default branch.
- `ami_id` (ec2) and `db_instance_class` (managed DB) are captured at assessment approval and enforced by the
  Execution pre-hook — never invent them.

**Generate + stage only** — the tool renders a root `main.tf` (declares + wires ONLY the variables of the
included modules) + a matching `terraform.tfvars` + `backend.tf` (S3 state in the TARGET account, named
`{project_prefix}-tfstate-{account}-{region}` / `{project_prefix}-tfstate-lock`; the generated README has the
bootstrap commands) and stages them to `s3://{bucket}/applications/{app_id}/execution/infrastructure/`.
A human runs `terraform plan/apply`. The tool does NOT run apply.

The same tool then commits the staged Terraform into the application's linked CodeCommit repository
under `infra/` (one commit on the migration branch `migration/<app_id>`), and the `InfrastructureValidate`
state runs `terraform init/validate/plan` in an ECS worker. If no CodeCommit repo is linked the commit is
`skipped` (not an error).

Output: staged Terraform in S3 + committed to the repo under `infra/` (commit_id) + an EXECUTION# record.

### 3. Database Migration (PLAN-ONLY — parked)
No automated DB-migration tool yet. In this phase the Harness only **documents the DB migration plan**
in the design doc Section 3.4 (engine mapping, SCT need, DMS full-load + CDC vs offline, validation
approach). Do NOT claim a DB was migrated. A `setup_database_migration` tool is planned but not yet built.

### 4. Network Configuration (OBD_Network)
Read from design doc Section 3.2:
- Security groups (ALB → ECS → DB tiers)
- ACM certificate (if domain provided)
- Route 53 hosted zone + record (if domain)
- NAT Gateway (for private subnets)
- VPC endpoints (S3, ECR, Secrets Manager)

Output: Security group IDs, certificate ARN, DNS record.

### 5. CI/CD Pipeline (generate + stage) — run by the workflow
The workflow calls **`generate_cicd`** (`cicd_target=codepipeline`, the only supported target). It renders
the standardized CodePipeline templates and stages them to `s3://{bucket}/applications/{app_id}/execution/cicd/`:
`cicd/buildspec.yml` (run by the codepipeline module's CodeBuild project) + `cicd/CODEPIPELINE_DEPLOY_NOTE.md`,
plus `cicd/codedeploy/appspec.yml` + `cicd/codedeploy/scripts/*.sh` for an EC2 target.

Pipeline stages (the design Findings-Register countermeasures — real, blocking stages in every template):
1. Build + unit tests + **coverage gate** (JaCoCo for Maven, coverage.py for Python, c8 for Node;
   `coverage_threshold`, default 70). Maven runs `mvn -B verify` at `build_dir` (repo root by default,
   so multi-module reactors build fully); artifacts `**/target/*.jar`.
2. Package — container targets: Docker build + ECR push; **EC2 targets: no
   Docker/ECR — the built artifact is uploaded to S3** (`{app_id}-artifacts-{account}-{region}`, created by
   the `ec2` module); **Lambda targets: the function package is uploaded to the artifact bucket** (created by
   the `lambda` module)
3. Deploy — ecs_fargate: rolling ECS service update; lambda: `update-function-code --publish` + smoke
   invoke (from CodeBuild); ec2: CodeDeploy in-place from the `cicd/codedeploy/` revision

The JDK/Python/Node version is derived (planned version-upgrade target → META runtime / language_version →
assessment detected stack → 17 / 3.12 / 20), never a blanket default. The pipeline sources ONE branch
(EventBridge on CodeCommit push): the repo's default branch, i.e. it builds and deploys after the migration
branch is merged — or META `deploy_branch` (e.g. the migration branch, to deploy it before merge).

The same tool then commits the staged CI/CD files into the application's linked CodeCommit repository
(one commit on the migration branch). If no CodeCommit repo is linked the commit is `skipped`.
Every compute gets CI + CD: ECS deploy action (ecs_fargate), CodeDeploy (ec2), CodeBuild-run
`update-function-code` (lambda).

**Repo host:** CodePipeline sources CodeCommit only (other hosts need a CodeStar connection, not
provisioned). On a mismatch the files are still
staged/committed, but the EXECUTION#cicd record carries `provider_mismatch: true` + a `warnings` entry
("AWS CodePipeline will not run on a <host> repo") and META `exec_cicd_warning` is set — report it; never describe that
pipeline as running.

Output: staged CI/CD files in S3 + committed to the repo (commit_id) + an EXECUTION# record.

---

## Post-Execution: Validation Phase

After all 5 parallel tasks complete + approval, validation runs:

### Smoke Test
- Hit health check endpoint
- Verify HTTP 200
- Check container logs for startup errors

### Security Scan
- AMF generates no scan stage. If required, the customer adds Amazon Inspector (CI/CD scanning,
  ECR enhanced scanning); report it as not run otherwise.
- IAM Access Analyzer: over-permissive roles

### Performance Test
- Load test at `peak_tps` level (from app data)
- Measure: P50, P95, P99 latency
- Compare against `latency_sla_ms`
- Report: pass/fail + metrics

Output: Validation report (pass/fail per category).

---

## Post-Validation: Handover Phase

After validation approved:

### Runbook Generation
Generate operational runbook covering:
1. **Service restart procedure** — ECS task restart, EC2 instance refresh, Lambda version rollback
2. **Scaling procedure** — manual scale up/down commands
3. **Log access** — CloudWatch log group, search queries
4. **Alarm response** — what to do when each alarm fires
5. **Rollback procedure** — how to revert to previous version
6. **Database failover** — Aurora failover steps
7. **Contact list** — on-call, escalation path
8. **Architecture diagram** — with actual resource ARNs

Output: Runbook stored in S3 as `applications/{app_id}/handover/runbook.md`

---

## Output Contract

Each sub-task stores results in DynamoDB and S3:
- DynamoDB: `APP#{app_id}` META updated with resource ARNs
- S3: `applications/{app_id}/execution/{task_name}/` — logs, reports, artifacts
- Design doc updated to final version with actual ARNs

### Gate decision-summary (when this phase requests approval)
When the orchestration layer requests human approval for this phase (the
`execution_review` gate — see `invoke_runtime.py`'s `_store_task_token`), attach a
`decision_summary` built from data this skill already produced, per
the decision-support contract (R1/R2):
```json
{
  "what_changed": "from the 5 sub-task outputs — e.g. '2 transformation steps completed + 1 no_change on migration/app-he; Terraform staged to S3; CI/CD committed (commit abc123); DB migration plan documented'",
  "confidence_or_risk": "from build/validation results per sub-task — any ATX step partial/unverified or failed, skipped_steps (language mismatch), provider_mismatch, completed_with_errors synthesis, skipped commits (no CodeCommit repo), infra staged-not-applied",
  "if_approve": "Validation phase runs smoke + security + performance tests",
  "if_reject": "Failed sub-task is re-run before validation",
  "if_retry": "Re-run the specific execution sub-task that failed (transformation / infra / CI/CD)"
}
```
This is populated from existing sub-task output — no new analysis.

## Quality Bar

- All 5 parallel tasks must complete before approval gate
- Build must succeed after transformation (not just ATX success) — only `completed` steps are verified
- Database replication validated (row count match)
- CI/CD first build passes
- Validation tests all pass before handover

## Anti-patterns

- Do NOT proceed to validation if any execution sub-task failed
- Do NOT claim infrastructure was applied — infra (Terraform) is **generated, staged in S3, and
  committed to the repo under `infra/`** (by the workflow), but never `terraform apply`-ed;
  DB migration is **plan-only** (no tool yet). A human applies them. CI/CD files are likewise
  committed to the repo (by the workflow) but the pipeline itself is not run here.
- Do NOT skip documenting the DB migration plan for apps with DB dependencies
- Do NOT generate CI/CD if `cicd_target` is "none"
- Do NOT omit the design Findings-Register coverage gate from the CI/CD, and do NOT claim a security scan stage exists
- Do NOT report a `partial` / `no_change` step as a verified transformation, or a `provider_mismatch`
  pipeline as one that will run
- Do NOT commit or push generated code/IaC/CI-CD to the default branch — the migration branch only
- Do NOT mark handover complete without runbook generation
