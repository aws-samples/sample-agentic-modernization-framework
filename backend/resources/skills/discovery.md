---
name: discovery
description: Discovery agent skill — portfolio enrichment, machine linking, and 7R strategy recommendation. Runs as the first step of every workflow.
metadata:
  type: skill
  phase: discovery
  agent: discovery
---

# Skill: Discovery & Strategy Recommendation

## When this skill fires

- Workflow starts for any application
- User requests "discover", "enrich", "recommend strategy"
- First phase of the Step Functions workflow (Orchestrate_Discovery)

## Procedure

### Step 1 — Load application context
Call `get_app_context(app_id)` to retrieve all available metadata:
- Identity: app_id, app_name, business_unit, tier, status
- Technology: language, framework, architecture, deployment_method
- Infrastructure: runtime (OS, app server, version), database (engine, version, size, licensing)
- Environment: current_compute, current_region, containerization, cicd_target
- Dependencies: upstream, downstream, shared_db, integrations
- Performance: peak_tps, latency_sla_ms, uptime_sla
- Cost: monthly_infra, annual_license
- Constraints: dc_lease_end, eosl_date, hard_deadline
- DR: rpo, rto, multi_region, dr_regions
- People: owners, repos
- Risks: known risk factors

### Step 2 — Search and link machines
Call `search_machines` to find unassigned machines matching this application's hostname patterns or IP ranges. Link any matches via `assign_machines_to_app`.

### Step 3 — Language/framework from repos
Do not guess the stack. Linked repos are AWS CodeCommit repos; the Assessment code-analysis run clones
them and records `detected_*` (language, build tool, framework, runtime). Until then, use only the
language/framework already recorded on the app and note any that are missing.

### Step 4 — Calculate completeness
Call `calculate_completeness(app_id)`. Do not compute it yourself — the tool is deterministic and
mirrors `backend/src/core/completeness.py` (the same score the UI shows). It returns the weighted
`score_percent`, per-category filled/missing fields, `missing_fields`, and `phase_gates` +
`phase_gate_missing` (e.g. the assessment gate needs app_name, tier, machine_ids, compliance, rpo,
rto, target_region). Owners and repos are counted from the linked OWNER#/REPO# records.

Report the missing fields that lower confidence; the recommendation adjusts confidence by
completeness (> 80% → +10, < 40% → −15, floor 40%).

### Step 5 — Recommend strategy
Call `recommend_strategy` with ALL available fields. ALWAYS provide a recommendation regardless of completeness — lower confidence is acceptable.

Use the **7R Strategy Decision Rules** below to evaluate and explain the recommendation.

### Step 6 — Store results
The `recommend_strategy` tool automatically persists:
- `migration_strategy` → the recommended strategy (NOT overwritten when a human already confirmed a
  strategy in the current run — then only the fields below are refreshed)
- `strategy_confidence` / `strategy_rationale` → always describe `migration_strategy` (for a
  human-confirmed strategy: "Human-confirmed X; engine recommended Y (n%) because …")
- `recommended_strategy` / `recommended_confidence` / `recommended_rationale` / `strategy_scores` →
  the engine's own recommendation and per-strategy confidence

Discovery summary is stored to S3: `applications/{app_id}/discovery/discovery-summary.json`

---

## 7R Strategy Decision Rules

The rules are implemented deterministically in `recommend_strategy` (`tool_strategy.py`); the matched
triggers are listed in the rationale. Two kinds of evidence:
- **Signal rules** on structured fields (`current_compute`, `containerization`, `deployment_method`,
  `architecture`, `language`, `tier`, `change_history`, `constraints`, `database`, `current_cost`).
- **Keyword rules**: whole-word triggers in human-authored text (name, description, notes, architecture,
  language, framework, current_compute, …) — never in fields this pipeline derives.

**Precedence:** (1) close-out intent (retire / retain / repurchase) → (2) structured signal rules →
(3) keyword-only matches for relocate/rehost/replatform/refactor. Within a class the highest confidence
wins; ties follow the order below. A lone keyword such as "microservices" is one weak signal — it can
not override compound evidence from the structured fields (refactor needs 3+ signals).

### 1. RETIRE (Decommission)
**Triggers:**
- Zero active users AND no active development
- Notes explicitly mention "deprecated", "decommission", "replaced"
- End-of-life with confirmed replacement in place

**Confidence:** 85-90%  
**Assessments needed:** None

### 2. RETAIN (Keep as-is)
**Triggers:**
- Restricted data classification WITH shared database dependency
- FedRAMP compliance on mainframe runtime
- Active legal/regulatory hold preventing modification
- Vendor-locked appliance with no migration path

**Confidence:** 70-75%  
**Assessments needed:** None

### 3. REPURCHASE (Replace with SaaS)
**Triggers:**
- Application category matches CRM, ERP, ITSM, HR systems
- Notes indicate SaaS evaluation in progress
- High license cost with available SaaS alternative
- Low customization + commodity functionality

**Confidence:** 60-80%  
**Assessments needed:** Comprehensive codebase analysis

### 4. RELOCATE (VMware Cloud on AWS)
**Triggers:**
- VMware-dependent workload
- Tight timeline + complex monolith on VMware
- DC lease expiring without time for deeper modernization

**Deterministic signal:** `current_compute` mentions VMware / vSphere / ESXi **and** a DC-exit signal —
`constraints.dc_lease_end` set, or the text mentions "dc exit", "datacenter exit/closure",
"lease expiry/expiring", "no code change".

**Confidence:** 70% (+5 per additional exit signal, max 80%)  
**Assessments needed:** Comprehensive codebase analysis

### 5. REHOST (Lift-and-Shift to EC2)
**Triggers:**
- Legacy language (COBOL, RPG, Fortran, PL/I, Natural)
- Tier 3 monolith in maintenance mode (no active development)
- Hard deadline too aggressive for deeper modernization (<6 months)
- Complex proprietary middleware without cloud equivalent
- EOL OS requiring same-version EC2 deployment

**Deterministic signals:** legacy `language` (COBOL, RPG, Fortran, PL/I, Natural); `tier_3` monolith with
`change_history.active_development = false`.

**Confidence:** 70-80%  
**Assessments needed:** Comprehensive codebase analysis

### 6. REPLATFORM (Containerize + Managed Services)
**Triggers (compound — need 2+ signals):**
- Modern language (Java, Python, Node, C#, Go, Ruby, PHP) + monolith/SOA + not (or only partially) containerized → containerize to ECS Fargate
- Partially containerized (a Docker image, no orchestrator) → ECS Fargate
- Function-shaped code (serverless architecture / Lambda handler) still run on servers → Lambda
- End-of-life runtime (Python ≤ 3.8, Java ≤ 8, Node ≤ 16) → runtime upgrade with the replatform
- Managed database source (DynamoDB / Aurora / RDS) → no data migration needed
- Oracle/SQL Server with enterprise licensing → migrate to Aurora/RDS
- Manual deployment → add CI/CD pipeline
- High annual license cost (>$30K/yr) → managed services reduce cost
- Stateful workload on bare-metal → PaaS equivalent exists

**Deterministic signals** (2+ required): modern `language` + `architecture` monolith/SOA/n-tier +
`containerization` none or partial; `containerization` partial / Docker without an orchestrator (non-monolith);
`architecture` serverless/function or `framework` "Lambda handler"/serverless while `current_compute` is not
Lambda; an EOL runtime in `runtime_version` / `language_version` / `runtime` / `description`;
`database.engine` DynamoDB/Aurora/RDS; `database.engine` Oracle/SQL Server with enterprise/commercial/BYOL
licensing; `deployment_method` manual; `current_cost.annual_license` > $30K.

**Sub-strategy routing** (selectable replatform computes: ECS Fargate, Lambda — EKS is not offered):
- Monolith/SOA/containerizable service (incl. microservices) → ECS Fargate (replatform-ecs skill)
- Function-shaped handler (event-driven, stateless) → Lambda

**Confidence:** 60-85% (60 + 8 per signal, max 85%)  
**Assessments needed:** Comprehensive + Modernization readiness

### 7. REFACTOR (Re-architect / Cloud-Native)
**Triggers (compound — need 3+ signals):**
- Tier 1 strategic application WITH active development
- Large team (6+) on monolith → microservices enable independent deployment
- Already containerized microservices → optimize for serverless/cloud-native
- High release velocity (daily/multiple-daily) → cloud-native enables faster iteration
- Revenue-critical with scaling requirements

**Deterministic signals** (3+ required): `tier_1` with active development; team ≥ 6 on a monolith;
microservices already containerized (`containerization` yes/full/docker/kubernetes — "partial" does not
count); daily / multiple-daily / continuous `release_frequency`.

**Sub-strategy routing:**
- Event-driven + stateless → Serverless/Lambda (refactor-serverless skill)
- Already containerized microservices → ECS Fargate (EKS is not offered)

**Confidence:** 65-90% (65 + 8 per signal beyond 2, max 90%)  
**Assessments needed:** Comprehensive + Modernization + Agentic readiness

---

## Confidence Adjustments

`recommend_strategy` applies only the completeness adjustment (Step 4). The table below is guidance
for explaining which missing fields hold confidence down — it is not a second scoring pass.

| Missing Field | Penalty |
|---------------|---------|
| language | -4% |
| framework | -4% |
| last_release_date | -4% |
| user_count | -4% |
| repos | -4% |
| database details | -4% |
| runtime details | -4% |

Maximum penalty: -20% (floor at 30%)

## Compound Signal Weights

| Signal Category | Weight |
|----------------|--------|
| Technology stack (language, framework, architecture) | 2x |
| Database (engine, licensing, EOSL) | 2x |
| Business context (tier, active_dev, team_size) | 3x |
| Deployment method | 1x |
| Cost factors | 1x |
| Timeline constraints | 1x |
| Release velocity | 1x |

## Assessment Mapping

| Strategy | Assessments |
|----------|-------------|
| Retire | None |
| Retain | None |
| Repurchase | AWS/comprehensive-codebase-analysis |
| Relocate | AWS/comprehensive-codebase-analysis |
| Rehost | AWS/comprehensive-codebase-analysis |
| Replatform | AWS/comprehensive-codebase-analysis, AWS/modernization-readiness-analysis |
| Refactor | AWS/comprehensive-codebase-analysis, AWS/modernization-readiness-analysis, AWS/agentic-readiness-analysis |

---

## Output Contract

The discovery agent must produce:
- Strategy recommendation with confidence score
- Completeness assessment
- List of missing fields that would improve confidence
- Which assessments to run based on strategy

### Gate decision-summary (when this phase requests approval)
When the orchestration layer requests human approval for this phase (the
`discovery_review` gate — see `invoke_runtime.py`'s `_store_task_token`), attach a
`decision_summary` built from data this skill already produced, per
the decision-support contract (R1/R2):
```json
{
  "what_changed": "from the recommendation — e.g. 'Recommend REPLATFORM (72%); linked 3 machines; detected Java 11/Spring Boot 2.7'",
  "confidence_or_risk": "from strategy_confidence + the list of missing fields that lowered it",
  "if_approve": "Assessment runs the strategy-gated analyses for the recommended 7R strategy",
  "if_reject": "Discovery re-runs / strategy is manually overridden before assessment",
  "if_retry": "Re-enrich (e.g. after linking repos or supplying missing critical fields) to raise confidence"
}
```
This is populated from existing output — no new analysis.

## Quality Bar

- Strategy is NEVER "TBD" — always provide a recommendation
- Confidence is honest (reduced per missing critical field)
- Missing data is listed with impact ("would change recommendation" vs "nice to have")
- Machine linking attempted even if no matches found

## Anti-patterns

- Do NOT refuse to recommend due to missing data. Low confidence is better than no recommendation.
- Do NOT fabricate data. If a field is empty, say it's empty.
- Do NOT skip the `recommend_strategy` tool call. It must be called every time.
