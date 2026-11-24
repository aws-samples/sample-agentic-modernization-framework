---
name: design
description: Design agent skill — generates comprehensive migration design documents incorporating assessment data, target architecture (auto-derived per strategy), and execution details. The design doc is the single source of truth for downstream execution.
metadata:
  type: skill
  phase: design
  agent: design
---

# Skill: Migration Design Document Generation

## When this skill fires

- Assessment approved (with confirmed `target_compute` + `transformation_steps`) → generates design V1 (complete)
- User provides Account ID + VPC CIDR → generates V2 (execution-ready)
- After execution steps complete → generates V3+ (with actual resource ARNs)

## Procedure

### Step 1 — Gather all context
Load from DynamoDB + S3:
- App metadata (all fields from META record)
- Assessment results (ASSESSMENT# record) + the `assessment-summary` (containerization readiness, gaps, compute rationale)
- **Confirmed selections**: `target_compute`, `transformation_steps` (from assessment approval)
- Code analysis outputs — call `get_analysis_result(app_id, analysis_type="comprehensive-codebase-analysis")` and
  review EVERY ATX section, especially: `analysis/security-patterns`, `technical-debt/remediation-plan`,
  `analysis/complexity-analysis`, `analysis/dependency-analysis`, `reference/data-models`, `reference/api-reference`.
- User-provided notes and design_inputs
- Previous design version (if updating)

### Step 1.5 — Build the Findings Register (mandatory)
Extract every finding from the ATX sections + `assessment-summary.gaps` into a register. For EACH finding,
assign a standardized remediation using the **Assessment Findings → Remediation Matrix** below. Every
**High/Critical** finding MUST map to either a design decision (Section 3) or an execution step (Section 10/11),
or be listed as an accepted risk with rationale (Section 8). Do NOT drop findings silently.

### Step 2 — Generate the document

CRITICAL RULES:
1. ALL data marked as "(CONFIRMED)" is VERIFIED. Do NOT mark confirmed data as missing.
2. Only use [REQUIRES INPUT] for genuinely missing fields — NOT for derivable data.
3. If a field has ANY value, use it as-is. Never question confirmed data.
4. Use the **Target Architecture by Strategy** section below to auto-derive infrastructure values.
5. `target_compute` and `transformation_steps` are CONFIRMED at assessment approval — use them directly.
5b. **`target_compute` (META) is the SINGLE SOURCE OF TRUTH for compute.** If the `assessment-summary`
   prose or its `target_compute_rationale` names a DIFFERENT compute, the META `target_compute` field
   WINS — treat the summary prose as stale and design entirely for the confirmed value. NEVER let
   assessment prose override the confirmed field (this once produced a full EKS design for an app whose
   confirmed `target_compute` was `ecs_fargate`). Pick the matching **Target Architecture by Strategy**
   section from the confirmed compute, not from what the codebase "looks like".
5c. **The same applies to every APPROVED selection on META** — `migration_strategy`, `target_compute`,
   `target_database`, `cicd_target`, `target_account` (AWS Account ID), `vpc_cidr`, `target_region`.
   The assessment record may carry `consistency_warnings` (known errors in its prose/cost basis) and a
   `deferred_modernization` list — those items are OUT OF SCOPE for this migration; never plan them.
   A deterministic post-check (`Validate_Design`) compares the stored document against these selections
   and **fails the phase** on a contradiction, e.g.: an EC2 design whose target compute is ECS/Fargate/
   EKS/Lambda; an `ecs_fargate` design that proposes EC2/EKS/Lambda or never names Fargate in its compute
   statement / §3.3; a `lambda` design that proposes ECS/Fargate/EC2 (including §3.3 prose such as "deploy
   on Fargate instead"); `target_database=same_as_source` with a
   DMS plan or a move to RDS/Aurora; `rds_postgresql` designed as Aurora; a pipeline other than AWS
   CodePipeline; a different AWS Account ID or VPC CIDR; a different 7R strategy. Describing the SOURCE
   state ("currently on ECS", "no Aurora") is fine — the target statements are what is checked.
5e. **Never substitute a different compute, database or pipeline** — not for scale (peak TPS, duration),
   not for team skills, not because the code "looks" containerized/serverless. If the approved selection
   looks risky, keep it in the architecture and record the concern in §12 Risks (and, if material, as a
   reviewer note for the design_review gate). Only the reviewer changes a selection.
5d. **DR/HA inputs.** Section 6 (Operational Readiness) MUST use the app's `rpo`, `rto`, `uptime_sla`,
   `dr_availability`, `target_region` and `multi_region` META fields when present (state them explicitly,
   e.g. "RPO 15 min / RTO 1 h"), and say which design decisions meet them.
5e. **Status.** The document header `Status` is always **Pending Review** — a design is only approved at
   the `design_review` gate (a premature "Approved" stamp is normalized and flagged by the store tool).
6. The ONLY user inputs needed for design are:
   - AWS Account ID (new or existing) — if not provided, use "new dedicated account"
   - VPC CIDR range — if not provided, use 10.0.0.0/16
   - Domain name — if not provided, use "ALB DNS (no custom domain)"
   All other fields (compute, DB, security, monitoring, CI/CD) are auto-derived from the skill.

## Document Structure (12 sections)

This is the **single list** of required sections (the ASL Design prompt mirrors it; missing sections are
flagged at the `design_review` gate). Emit each as a level-2 heading, numbered and in this order:
`## 1. Executive Summary` … `## 12. Deployment Readiness Checklist`. Do not rename, merge or skip
sections — a section with nothing to say (e.g. §10 for rehost) states why ("Not applicable — rehost
migrates the code as-is").

Before §1 emit a **Target Infrastructure Parameters** table (`| Parameter | Value |`) with at least these
rows, taken verbatim from META: `AWS Account ID`, `Target Region`, `VPC CIDR`, `Target Compute`,
`Target Database`, `CI/CD Target`, `Migration Strategy`.

### 1. Executive Summary
- Strategy, timeline, key risks, decisions needed

### 2. Current State
- Application overview (language, framework, architecture, deployment)
- Infrastructure inventory (machines, OS, CPU/RAM, utilization)
- Dependencies & integrations
- Technical debt & risks
- Current costs (license + infrastructure)

### 3. Target State Architecture
- 3.1 AWS Account & Region (target account, region, tagging strategy)
- 3.2 Network Design (VPC, subnets, security groups, ALB/NLB, Route 53, ACM)
- 3.3 Compute Strategy (the APPROVED `target_compute`: ECS Fargate / EC2 / Lambda — never another)
- 3.4 Data Store Migration (target DB, instance class, Multi-AZ, DMS plan)
- 3.5 Security & IAM (roles, KMS keys, Secrets Manager, encryption)
- 3.6 Application Configuration (env vars, health check, ECR repo, domain, log group)

### 4. Migration Approach
- Strategy & rationale (from discovery + assessment)
- Transformation steps (ATX TDs in order)
- Data migration plan (DMS CDC vs offline)
- Cutover approach (blue/green, rolling, maintenance window)
- **4.5 Findings Remediation** — table of every assessment finding and its standardized countermeasure:
  Finding | Severity | Source section | Countermeasure (from matrix) | Where handled (design §/execution step/accepted risk)

### 5. Testing & Validation
- Pre-migration baselines
- Post-migration validation criteria
- Performance testing plan
- Security validation

### 6. Operational Readiness
- Monitoring & alerting (CloudWatch, alarms, dashboards)
- DR/HA design (multi-AZ, failover, backup) — from META `dr_availability`, `multi_region`, `uptime_sla`
- Backup & recovery — state the META `rpo` / `rto` and how the design meets them
- Runbooks (top 5 incident types)

### 7. Timeline & Milestones
- Key milestones with dates
- Dependencies & sequencing
- Resource requirements

### 8. Risks & Mitigations
Table format: Risk | Impact | Likelihood | Mitigation | Owner

### 9. Cost Analysis
- Current annual cost (infra + license)
- Projected AWS cost (compute + storage + network + managed services)
- Migration cost (one-time)
- ROI timeline (break-even point)

**Reconcile against the Assessment-phase estimate (do NOT silently replace it).**
The Assessment synthesis (loaded in Step 1 from the ASSESSMENT# record) may carry a
`cost_projection` object with a preliminary, ballpark target-cost figure. If present, this
section MUST show BOTH numbers and the delta between them — never hide or overwrite the
Assessment figure:
- Read `cost_projection.target_ballpark_annual_cost.value` (the AMF-derived ballpark produced
  at Assessment, before any detailed sizing) and `cost_projection.current_annual_cost` (the
  customer-supplied current ARR, if provided).
- State the reconciliation explicitly, e.g.:
  *"Assessment estimated ~$X/yr (preliminary ballpark); detailed sizing here puts the projected
  AWS run-cost at $Y/yr (delta: Z%)."* Compute the delta as `(Y − X) / X`.
- Briefly explain WHICH detailed sizing decisions moved the number (e.g. right-sized to
  `m6i.xlarge` instead of the ballpark class, Multi-AZ Aurora added, NAT/egress included,
  Reserved-Instance pricing applied) when that is knowable from this design's own §3 decisions.
- The Assessment figure stays labeled as the preliminary estimate; this §9 figure is the
  authoritative detailed number. Both remain visible.

**Graceful degradation — never error or block on missing prior data.** If no `cost_projection`
is present on the Assessment record (the app was assessed before this field existed), or
`cost_projection.current_annual_cost.status` is `missing`, do NOT fail or leave a gap: show this
section's own detailed AWS cost figure and add a one-line note —
*"No Assessment-phase cost estimate was available to reconcile against."* (and, if current ARR is
the missing piece, *"current annual cost not supplied by customer — savings delta not computable"*).
Proceed normally with the rest of the section.

### 10. Code Transformations
- Transformation steps applied (ATX TDs)
- Code changes summary
- Post-transformation validation

### 11. CI/CD Pipeline
- Pipeline provider: AWS CodePipeline (CodeCommit source → CodeBuild → deploy) — the only supported target
- Build process (compile, test, scan, package)
- Deployment strategy (blue/green, rolling, canary)
- Rollback procedure

### 12. Deployment Readiness Checklist
Auto-generated: ✅ done, ⬜ remaining

---

## Target Architecture by Strategy

Use the matching section below based on the app's `migration_strategy` AND confirmed `target_compute`.
Auto-derive all values from app metadata — user input is minimal. `retire` / `repurchase` / `retain`
are close-outs that skip Design (straight to Handover) — there is no section for them.

**Selectable targets:** rehost/relocate → compute `ec2` only · replatform/refactor → `ecs_fargate` |
`lambda`. Databases: `dynamodb`, `rds_postgresql`, `rds_mysql`, `aurora_postgresql`, `aurora_mysql`,
`none` (lift: `same_as_source`, `dynamodb` or `rds_mysql`). CI/CD: **AWS CodePipeline** only (`cicd_target=codepipeline`,
CodeCommit source). **EKS, EC2-for-replatform, any CI/CD other than CodePipeline, DocumentDB and RDS SQL
Server are not offered** — never design for them.

**Selection → section:** rehost → REHOST · relocate → RELOCATE · replatform/refactor + `ecs_fargate` →
REPLATFORM → ECS Fargate · replatform/refactor + `lambda` → REPLATFORM/REFACTOR → Serverless (Lambda).

**What Execution generates (design to THIS — never promise more):**

| target_compute | Terraform modules | Deploy (AWS CodePipeline) |
|---|---|---|
| `ecs_fargate` | vpc + ecs-fargate (cluster, service, ECR, ALB) + codepipeline | ECS deploy action (rolling, new task-definition revision) |
| `ec2` (lift only) | vpc + ec2 (launch template from `ami_id`, ASG, ALB, artifact bucket) + codepipeline | CodeDeploy in-place, one instance at a time behind the ALB, auto-rollback (revision from `cicd/codedeploy/`) |
| `lambda` | vpc + lambda (function, HTTP API, artifact bucket) + codepipeline | no Deploy stage: CodeBuild packages the function to the artifact bucket, runs `aws lambda update-function-code --publish` + a smoke invoke |

Plus `rds` / `dynamodb` for the approved `target_database`. The ALB is **internal** by default (VPC +
allowed private CIDRs); internet-facing only when the app sets `expose_publicly`, and never open to
0.0.0.0/0. AZ count comes from the app (`dr_availability.availability_zones` / `az_count`, default 2).

---

### REHOST → EC2 (Lift & Shift)

**Hard rules (checked by the Validate_Design post-check):**
- `target_compute` is **EC2** — never ECS, Fargate, EKS, Kubernetes or Lambda as the target compute.
  **No containerization** in this migration (containerizing is a Day-2 replatform decision).
- `target_database` is **`same_as_source`** — the database migrates **as-is with the servers** (MGN
  replicates the DB host, or it stays where it is). **No DMS, no engine change, no move to RDS/Aurora.**
  Or **`dynamodb`** — a new DynamoDB table (on-demand) whose name reaches the app as `TABLE_NAME`; the
  instance role gets CRUD on it. Still no DMS and no code changes.
- **No code transformations** — §10 states "Not applicable — rehost migrates the code as-is".
- CI/CD is AWS CodePipeline (the only supported `cicd_target`): it builds the artifact and deploys it to
  EC2 via CodeDeploy — never substitute a different pipeline tool.

**Compute:**
- Amazon EC2, same OS, right-sized instance
- Instance type mapping from current specs:
  - 1-2 vCPU, ≤4 GB → t3.medium
  - 2-4 vCPU, 4-8 GB → m6i.large
  - 4-8 vCPU, 8-16 GB → m6i.xlarge
  - 8-16 vCPU, 16-32 GB → m6i.2xlarge
  - 16+ vCPU → m6i.4xlarge
- If avg CPU < 20% → downsize one tier
- AMI: Match current OS (RHEL→RHEL, CentOS→AL2023, Ubuntu→Ubuntu, Windows→Windows)
- EBS: gp3 (general), io2 (high-IOPS databases)

**Database (`same_as_source`):**
- Same engine, same version, same licensing — the DB server is rehosted to EC2 by MGN alongside the
  app servers (or stays in place if it is out of scope). Size the DB EC2 instance with the mapping
  above; EBS io2 for high-IOPS databases.
- Moving to a managed service (RDS/Aurora) is a **replatform** decision — list it only as a Day-2
  recommendation in §8, never in the target architecture.

**Database (`rds_mysql` on a lift):**
- A MySQL source moves onto **Amazon RDS for MySQL** — same engine, so the application code does not
  change; it only gets the new endpoint (Spring datasource / `DB_*` env, password from the RDS-managed
  secret). Plan the data move as a MySQL dump/restore or AWS DMS full-load + CDC for the cutover window.
  Never plan Aurora, PostgreSQL or DynamoDB here.

**Migration Tool:** AWS Application Migration Service (MGN)
- Install agent → initial replication → continuous block-level replication → test cutover → production cutover
- Image: MGN-launched instances become the golden **AMI** for the ASG / launch template.

**Network:** New VPC (the confirmed `vpc_cidr`, default 10.0.0.0/16) across 2 AZs — public subnets (ALB/NLB), private app subnets (EC2), private data subnets (DB EC2); auto-created by IaC. Mirror current firewall rules into security groups. ALB/NLB if load balanced. All network resources are provisioned by the generated Terraform — **never ask the user for VPC/subnet IDs.**

**CI/CD (CodePipeline):** build the existing artifact (JAR/WAR/binary — no container image), upload it to
the app's artifact bucket and deploy with CodeDeploy in-place, one instance at a time behind the ALB,
auto-rollback. AMI updates via golden AMI + user data. Day-2 consider containerization.

**Cutover:** MGN test cutover → final sync → production cutover in the maintenance window; rollback =
repoint DNS/ALB to the source servers (kept running through the bake period).

**Cost:** EC2 instance pricing × 730 hrs × server count (app + DB servers) + EBS + ALB. Reserved Instance for 40% savings.

---

### RELOCATE → EC2 (Move As-Is, No Re-Architecture)

*Applies when `migration_strategy=relocate`: the workload moves to AWS unchanged — same binaries,
same deliverable (VM image, container image, executable) — typically from another hosting
environment / data center / VMware estate or from a deliverable with no source repo.*

**Hard rules (checked by the Validate_Design post-check):**
- `target_compute` is **EC2** — never ECS, Fargate, EKS, Kubernetes or Lambda as the target compute,
  even when the source code "looks" serverless or containerized (record that as a Day-2 risk/opportunity in §8).
- `target_database` is **`same_as_source`** — the data store stays as-is (already-AWS-managed stores such
  as DynamoDB are re-pointed, not migrated). **No DMS, no engine change, no move to RDS/Aurora.**
  Or **`dynamodb`** — a new DynamoDB table provisioned for the app (`TABLE_NAME` env); still no DMS.
- **No code transformations** (§10 = "Not applicable — relocate moves the deliverable as-is").
- CI/CD is AWS CodePipeline only.

**Compute:** EC2 sized like REHOST (instance mapping above). VMware workloads: MGN (or VM Import) to EC2.
A container-image deliverable runs **as-is on the EC2 host's container runtime** (Docker on EC2) — no
orchestrator change (no ECS/EKS/Fargate). Executable/binary deliverables are installed via user data / AMI.

**Migration Tool:** AWS MGN for servers/VMs; S3 + AMI bake for deliverables; DataSync for file shares.

**Network / Security:** identical to REHOST (confirmed `vpc_cidr`, 2 AZs, SGs mirroring current rules).

**CI/CD (CodePipeline):** promote the unchanged deliverable to EC2 with CodeDeploy in-place,
one instance at a time; no rebuild of application code.

**Cutover:** parallel run → DNS/ALB switch → bake period with the source kept live for rollback.

**Cost:** EC2 × 730 hrs × instance count + EBS + ALB (+ unchanged managed-store costs).

---

### REPLATFORM/REFACTOR → ECS Fargate (Containerize + Managed Services)

**Compute:**
- Amazon ECS on Fargate (serverless containers)
- Task sizing from current utilization:
  - avg CPU < 20% AND RAM < 4GB → 1 vCPU / 2 GB
  - avg CPU < 40% AND RAM < 8GB → 2 vCPU / 4 GB
  - avg CPU < 70% AND RAM < 16GB → 4 vCPU / 8 GB
  - Else → 4 vCPU / 16 GB
- Desired count: 2 (HA minimum), scale on CPU 60% + request count
- Container port by framework: Spring→8080, Django/FastAPI→8000, Express→3000, .NET→80, Go→8080
- Health check by framework: Spring→/actuator/health, Django→/health/, FastAPI→/health, Express→/health

**Database:** use the CONFIRMED `target_database` exactly (`rds_postgresql` → Amazon RDS for
PostgreSQL, NOT Aurora; `aurora_postgresql` → Aurora PostgreSQL; …). Only when `target_database` is
unset, derive it:
- Oracle → Aurora PostgreSQL (save license)
- SQL Server → Aurora PostgreSQL (RDS SQL Server is not offered)
- PostgreSQL → Aurora PostgreSQL
- MySQL → Aurora MySQL
- MongoDB → DynamoDB (DocumentDB is not offered; flag the data-model change as a risk)
- Instance class by size: <50GB→db.r6g.large, 50-200GB→xlarge, 200-1000GB→2xlarge, >1TB→4xlarge
- Multi-AZ always, DMS with CDC for migration

**Network:**
- New VPC (the confirmed `vpc_cidr`, default 10.0.0.0/16) across the app's AZ count (default 2)
- Public subnets (ALB only if `expose_publicly`), Private App subnets (ECS), Private DB subnets (RDS)
- ALB: internal by default; HTTPS (443) with ACM cert when a domain is set, HTTP→redirect
- Security: ALB←VPC CIDR + allowed private CIDRs (never 0.0.0.0/0), ECS←ALB only, DB←ECS only

**CI/CD:**
- AWS CodePipeline + CodeBuild (CodeCommit source) — the only supported `cicd_target`.
- Stages: Build → Test + coverage gate → ECR push → ECS deploy (no scan stage; add Amazon Inspector if required)
- Deploy: rolling ECS service update (new task-definition revision), ECS deployment circuit breaker
  rolls back on failed health checks

**Cost:** Fargate (vCPU×$0.04048/hr + mem×$0.004445/hr) × 730 × desired_count + Aurora + ALB + NAT

---

### REPLATFORM/REFACTOR → Serverless (Lambda)

*Applies when the approved `target_compute=lambda`.*

**Compute:**
- AWS Lambda (`{app_id}-function`) + HTTP API Gateway (`{app_id}-api`, IAM or JWT auth — never anonymous),
  in the VPC's private app subnets
- Runtime from the app language (Java → Corretto `java<version>`, Node → `nodejs<version>.x`, Python →
  `python<version>`); handler from the app (`lambda_handler`), else the language default
- Lambda: 512MB default, 30s timeout (API), up to 900s (background)
- High peak TPS or long-running requests are **risks to record in §12** (provisioned concurrency,
  timeout tuning, async offload via SQS) — never a reason to switch compute.

**Database:** the approved `target_database` exactly (e.g. `dynamodb` → DynamoDB on-demand;
`aurora_postgresql` → Aurora PostgreSQL, accessed through RDS Proxy). Do not decompose into additional
data stores (OpenSearch, ElastiCache, …) — at most list them as Day-2 recommendations in §8.

**Event Architecture:**
- EventBridge (default event bus)
- SQS + Lambda (async processing, DLQ for failures)
- Step Functions (orchestration, batch)
- If current Kafka → MSK Serverless (high throughput)

**CI/CD (CodePipeline):** infrastructure in the generated Terraform (not SAM/CDK). CI builds, tests and scans, packages the function (zip / jar) to the artifact bucket; deploy is
`aws lambda update-function-code --publish` followed by a smoke invoke (all-at-once; publish a new version
per deploy so rollback = repoint to the previous version).

**Cost:** Lambda $0.20/1M requests + DynamoDB on-demand + API Gateway $1/1M. Typically 60-80% less than EC2/ECS for variable workloads.

---

## Assessment Findings → Remediation Matrix (standardized)

Map each ATX/assessment finding to a standard AWS countermeasure. "Where handled" tells downstream
execution what to do (`provision_infrastructure`, `generate_cicd`, a transformation step, or accepted risk).

| Finding category (ATX source) | Standardized countermeasure | Where handled |
|---|---|---|
| **No code coverage** (no JaCoCo/coverage) | Add coverage gate to CI (fail < threshold) | `generate_cicd` (test+coverage stage) |
| **No SAST/static analysis** (no SpotBugs/PMD/Sonar) | Customer-owned: add Amazon Inspector CI/CD scanning (and ECR enhanced scanning) to the pipeline | Not generated by AMF — record as an open risk |
| **CSRF disabled / weak web security** | Enable CSRF for stateful; document exception for stateless APIs; WAF | Design §3.5 + transformation step |
| **Conditional/optional auth** (e.g. OAuth2 issuer guard) | Enforce auth in prod profile; Cognito/OIDC; secrets in Secrets Manager | Design §3.5 (+ compliance rules) |
| **Outdated runtime/framework** (tech-debt/outdated-components) | ATX version-upgrade transformation (Java/Spring/SDK) | transformation step |
| **High complexity / maintenance burden** | Flag for refactor; add quality gate; track as risk | Design §8 (risk) + CI quality gate |
| **Hardcoded secrets/config** | Externalize to Secrets Manager / SSM Parameter Store | Design §3.5 + transformation step |
| **No IaC / manual infra** | Generate standardized Terraform (modules) | `provision_infrastructure` |
| **No/weak observability** | CloudWatch logs+metrics+alarms, X-Ray tracing | Design §6 + `provision_infrastructure` |
| **Unencrypted data/transport** | KMS at rest, TLS/ACM in transit, enforce HTTPS | Design §3.5 + `provision_infrastructure` |
| **Single-AZ / no DR** | Multi-AZ, backups, RPO/RTO per app data | Design §6 |
| **Dependency vulnerabilities** (dependency-analysis) | Dependency scanning + pinned versions in CI | `generate_cicd` |

Rule: every High/Critical finding appears in Section 4.5 with a non-empty "Where handled".

## Compliance-Aware Recommendations

If the app's `compliance` list includes any of the below, ADD these as **recommendations** in Section 3.5
(Security & IAM) and the Findings Register. These are advisory design recommendations, not hard gates.

| Framework | Recommended controls to call out in the design |
|---|---|
| **HIPAA** | Encryption at rest (KMS) + in transit (TLS 1.2+); enforce authN/authZ (no optional auth); audit logging (CloudTrail + app audit); PHI data isolation; BAA-eligible services only; least-privilege IAM; VPC-private data stores |
| **PCI-DSS** | Network segmentation (isolated CDE subnets); WAF; encryption of cardholder data; key rotation; centralized logging + FIM; no cardholder data in logs; quarterly scan hooks in CI |
| **SOX** | Change management + approvals (already via gates); immutable audit trail; segregation of duties in IAM; artifact provenance in CI/CD |
| **GDPR** | Data residency (region pinning); encryption; data-subject deletion path; PII minimization; access logging |
| **SOC 2** | Logging/monitoring + alarms; access controls; backup/DR; encryption; documented incident runbooks (Section 6) |
| **FedRAMP** | GovCloud/FedRAMP-eligible services; FIPS 140-2 endpoints; boundary protection; continuous monitoring |

When a framework applies, prefer the compliant option in every §3 decision (e.g. HIPAA → enforce OAuth2,
KMS, private subnets) and note it explicitly as "Recommended for {framework}".

## Version Progression

- **V1** (after assessment approval): **Complete design document**. Target compute + transformation steps already confirmed at assessment. All 12 sections filled. Only [REQUIRES INPUT] for **AWS Account ID** (and Domain only if a custom domain is implied). Region, VPC/CIDR, subnets, container port, and health check use Standard Infrastructure Defaults. Everything else auto-derived from assessment + design skill rules.
- **V2** (after user provides required inputs): **Execution-ready**. All fields resolved. No [REQUIRES INPUT] remaining. This is the final approved version before execution begins.
- **V3+** (after execution): Updated with actual resource ARNs (ECS cluster ARN, RDS endpoint, ALB DNS), deployment timestamps, validation results, and cost actuals.

## Output Contract

- Stored via `documents___store_design_document`: S3 `applications/{app_id}/design/{version}.md` +
  DynamoDB `DESIGN#{version}` (with the deterministic `consistency_check`)
- DynamoDB META (set by the tool, not the agent): `design_version` (the version stored),
  `design_status=pending_review`, `design_generated_at`, `design_warnings`, `design_contradictions`

### Gate decision-summary (when this phase requests approval)
When the orchestration layer requests human approval for this phase (the
`design_review` gate — see `invoke_runtime.py`'s `_store_task_token`), attach a
`decision_summary` built from data this skill already produced, per
the decision-support contract (R1/R2):
```json
{
  "what_changed": "from the design version + doc — e.g. 'Design V1 generated: ECS Fargate target, 3 transformation steps, Aurora PostgreSQL DB; 1 required input (AWS Account ID)'",
  "confidence_or_risk": "from the Findings Register — count of High/Critical findings and how each is handled, plus any accepted risks (§8) and remaining [REQUIRES INPUT]",
  "if_approve": "User provides required inputs (Account ID) → Design V2 (execution-ready) → Execution begins",
  "if_reject": "Design regenerated with corrected assumptions / different target selection",
  "if_retry": "Re-run design generation (e.g. after re-reading updated assessment findings)"
}
```
This is populated from existing output (the design doc + Findings Register) — no new analysis.

## What V1 Must Derive (from confirmed assessment data)

Since `target_compute`, `cicd_target`, and `transformation_steps` are confirmed at assessment, V1 is NOT a skeleton. It must auto-derive:

- **Compute sizing** — from `target_compute` (CONFIRMED) + machine utilization data
- **CI/CD pipeline** — AWS CodePipeline + CodeBuild (`cicd_target=codepipeline`, the only supported target)
- **Database target** — the CONFIRMED `target_database` (`same_as_source` = as-is, no DMS); derive from
  current DB engine + strategy (Oracle → Aurora PG, etc.) only when it is unset
- **Network design** — VPC layout, subnets, security groups (use defaults from skill)
- **Monitoring** — CloudWatch + X-Ray (standard for all)
- **DR/HA** — from app RPO/RTO + multi_region settings
- **Transformation steps** — exact ATX TDs in execution order (from CONFIRMED list)
- **Timeline** — estimated from transformation count + complexity
- **Cost estimate** — from target compute sizing + DB + network
- **Deployment strategy** — what Execution generates: rolling ECS service update (ecs_fargate), CodeDeploy in-place one-at-a-time or artifact upload (ec2), `update-function-code --publish` (lambda)

## Standard Infrastructure Defaults (NEVER ask the user — apply to ALL strategies)

These are **standardized** and provisioned by the generated Terraform. Fill them with the default and
present them as decided design (not as inputs). **Never** mark any of these `[REQUIRES INPUT]`:

| Field | Standard default (auto-applied) |
|---|---|
| Target Region | `us-east-1` (or the app's `current_region` if set) |
| VPC + CIDR | New VPC `10.0.0.0/16`, auto-created by IaC |
| Subnets | Public + private-app + private-data subnets across the app's AZ count (default 2), auto-created by IaC |
| Security Groups | Derived (ALB←VPC CIDR + allowed private CIDRs — internal unless `expose_publicly`, never 0.0.0.0/0; compute←ALB; DB←compute) |
| Container Port | Derived from framework (Spring→8080, FastAPI/Django→8000, Express→3000, .NET→80, Go→8080); rehost → current app port or 80/443 |
| Health Check Path | Derived from framework (Spring→/actuator/health, else `/health`) |
| Encryption | At rest + in transit (KMS + TLS) — standard for all |
| Deployment Strategy | Rolling ECS update (ecs_fargate), CodeDeploy in-place one-at-a-time (ec2 + CodePipeline), `update-function-code --publish` (lambda) |
| Monitoring/Logging | CloudWatch + X-Ray + CloudWatch Logs |

## What V1 Asks User (ONLY when genuinely account-specific)

Keep asked inputs to the **absolute minimum** — ideally just one:

1. **AWS Account ID** — "new dedicated account" or an existing account number. *(the only field that may block execution)*
2. **Domain Name** *(optional)* — omit and default to "none (use ALB/EC2 DNS)"; only ask if a custom domain is implied by the app data.

Everything in **Standard Infrastructure Defaults** and everything derivable from confirmed assessment
data MUST be filled with its default/derived value — do NOT mark it `[REQUIRES INPUT]`.

## Quality Bar

- All 12 sections present
- **At most 1–2 `[REQUIRES INPUT]` fields in V1 (Account ID; Domain only if a custom domain is implied)**
- Region, VPC/CIDR, subnets, container port, and health check are **standard defaults — never inputs**
- Cost section shows calculation basis
- Rollback plan exists (never omit)
- Target architecture matches confirmed `target_compute` exactly
- Transformation steps list matches confirmed `transformation_steps` exactly

## Anti-patterns

- Do NOT mark fields as `[REQUIRES INPUT]` if data was provided in the prompt or covered by Standard Infrastructure Defaults
- Do NOT flag Target Region, VPC CIDR, Target VPC, Target Subnets, Container Port, or Health Check Path as inputs — they are standardized/derived
- **Do NOT write prose or status lines containing "REQUIRES INPUT"** (e.g. "Status: BLOCKED — N parameters are REQUIRES INPUT"). The ONLY place to flag an input is a single dedicated **Required Inputs** table with a `[REQUIRES INPUT]` cell — the UI parses these rows, so stray "REQUIRES INPUT" text becomes phantom input fields.
- Do NOT put `REQUIRES INPUT` inside the Deployment Readiness Checklist rows or blockquotes — mark defaulted items ✅ / derived
- Do NOT fabricate resource ARNs or account IDs
- Do NOT generate cost numbers without showing calculation basis
- Do NOT skip the rollback plan in any version
- Do NOT generate a design doc for an unassessed app
- **Self-check before returning (two hard gates):**
  1. **Inputs:** the literal `[REQUIRES INPUT]` must appear **EXACTLY ONCE** in the whole document — a
     single row labeled **`AWS Account ID`** (this exact canonical label) in the dedicated **Required Inputs**
     table, which is the ONLY table the UI parses into an input field. Every other place the account is
     referenced (the Target Infrastructure Parameters table, §3.1 AWS Account & Region, the §12 checklist)
     MUST use the same label `AWS Account ID` and MUST NOT contain the literal `[REQUIRES INPUT]` — write
     `(provided via Required Inputs)` in table cells and `⬜ AWS Account ID provided` in the checklist.
     **One value → one label → one input field:** never introduce a second label (e.g. `Target Account`)
     for the same account, or it renders as a duplicate phantom field. Any other `[REQUIRES INPUT]`
     occurrence (region, CIDR, VPC, subnets, container port, health check, domain) is a defect — replace it
     with its Standard Infrastructure Default.
  2. **Compute:** confirm every compute reference (Section 3.3, cost, CI/CD deploy strategy, checklist)
     names the confirmed `target_compute` — not a compute inferred from the codebase or assessment prose.
  3. **Database / CI/CD / account / network:** the Target Database, CI/CD Target, AWS Account ID and
     VPC CIDR statements match META exactly (`same_as_source` → no DMS/RDS/Aurora plan).
  4. **Sections + status:** all 12 sections present as `## N. …` headings; header `Status: Pending Review`.
