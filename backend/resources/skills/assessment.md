---
name: assessment
description: Assessment agent skill — runs code analysis, modernization readiness, agentic readiness. Produces transformation step recommendations and target compute recommendation for user approval before design.
metadata:
  type: skill
  phase: assessment
  agent: assessment
---

# Skill: Application Assessment

## When this skill fires

- Discovery phase approved with a confirmed strategy
- Strategy determines which assessments run (per ASL ChooseAssessmentPath):
  - Retire/Retain → skip (no assessment needed)
  - Rehost/Relocate (default) → comprehensive code analysis only
  - Replatform/Repurchase → comprehensive + modernization readiness
  - Refactor → comprehensive + modernization + agentic readiness

## Procedure

### Step 1 — Component analysis (always runs)
Analyze the infrastructure inventory:
- Count app-tier vs DB-tier servers
- Identify HA configuration (active-active, active-passive, single)
- Catalog OS versions, EOSL status, licensed software
- Map machine utilization patterns

### Step 2 — Code analysis (if repos linked)
Run ATX `AWS/comprehensive-codebase-analysis` on linked repositories:
- Project overview, architecture, components
- Technical debt, complexity, security patterns
- Migration order, validation criteria
- Dependency analysis (internal and external)

If no repos linked: note `code_analysis: skipped` and reduce confidence.
If no repos but a **deliverable** (Docker image URL — ECR/JFrog — or executable/binary path) is
provided: this is a deliverable-based rehost/relocate with no source. Note `code_analysis: skipped
(deliverable-based)`, record the deliverable ref(s), and do NOT reduce confidence for the missing
source — the artifact is the migration unit and flows to Execution/deploy as-is.

### Step 3 — Modernization readiness (if strategy = Replatform/Refactor/Repurchase)
Run ATX `AWS/modernization-readiness-analysis`:
- Score across 5 categories: Infrastructure, Application, Data, Security, Operations
- Identify top gaps and remediation priorities
- Generate modernization roadmap

### Step 4 — Agentic readiness (if strategy = Refactor only)
Run ATX `AWS/agentic-readiness-analysis`:
- Evaluate suitability for AI-assisted operations
- Assess automation readiness
- Identify agentic integration opportunities

### Step 5 — Dependency analysis
Map all integration points:
- Upstream dependencies (what this app consumes)
- Downstream dependencies (what consumes this app)
- Shared databases (migration blockers)
- Cross-app network connections

### Step 6 — Synthesis + Recommendations
Combine all tool outputs and produce:

#### 6a — Target Compute Recommendation

**MANDATORY strategy override (applies first, before any finding-based row):** if
`strategy` is **rehost** or **relocate**, `target_compute` MUST be **`ec2`** — these are
lift-and-shift moves that run the workload as-is on EC2. Do NOT recommend `ecs_fargate`
or `lambda` for rehost/relocate no matter what the code analysis found (containerisation is a
*replatform/refactor* decision, deferred here). The rows below apply ONLY to
replatform/refactor/rearchitect.

Otherwise, based on assessment findings, recommend target compute. For replatform/refactor the
only selectable computes are **`ecs_fargate`** and **`lambda`** (EC2 is lift-and-shift only; EKS is
not offered):

| Condition | Recommendation |
|-----------|----------------|
| Strategy = rehost / relocate | **EC2** (no choice — lift-and-shift) |
| Monolith / containerizable service (incl. already containerized or microservices) | **ECS Fargate** |
| Function-shaped handler (event-driven, stateless, low sustained TPS, runs ≤ 15 min) | **Lambda (serverless)** |
| High TPS > 1000 + long-running | **ECS Fargate** |

Provide rationale based on assessment data. User can override at approval.

**Respect an already-confirmed selection.** If `target_compute` is ALREADY set in app META (e.g. the
user confirmed compute at the discovery/strategy gate, *before* this assessment ran), that value is
authoritative — do NOT recommend or narrate a different compute. Write `target_compute_rationale` to
justify the **confirmed** value; if the codebase points elsewhere, record that only as an
"alternative considered" note within the rationale. The stored `target_compute_rationale` MUST NEVER
name a compute that differs from the `target_compute` field — downstream Design consumes this prose,
and a contradiction silently overrides the user's choice (this produced an EKS design for a confirmed
`ecs_fargate` app).

**Strategy consistency is enforced in code** (`store_analysis_result`, analysis_type=assessment-summary)
— write it correctly the first time, because the tool will otherwise rewrite / flag it:
- For **rehost / relocate / retain** (and close-outs): `transformations` is zeroed, `target_compute`
  forced to `ec2`, and any upgrade / containerization / ATX-TD item in `migration_order`,
  `recommendations`, `containerization_readiness` or `qualification_criteria.runtime_support` is moved
  to `deferred_modernization` (labelled "out of scope for <strategy>"). Put such ideas there yourself.
  `containerization_readiness` is `not_applicable` for these strategies.
- A `target_compute_rationale` or `cost_projection.target_ballpark_annual_cost` basis that names a
  compute other than the confirmed one (e.g. Fargate/Lambda for an EC2 rehost), a different 7R
  strategy, Aurora when `target_database` is `same_as_source`/`rds_*`, or a `cicd_target` that differs
  from META is recorded in `consistency_warnings` (on the record and META `assessment_warnings`) and
  shown at the `assessment_review` gate. Price the ballpark for the CONFIRMED compute + database
  (rehost/relocate: EC2 + the database as-is).
- `atx_job_id`, `atx_run_id`, `analysis_completed_at` are stamped from the real ATX job / analysis
  record — do not invent them. `report_md` must be markdown (a JSON dump is replaced by a rendering).

#### 6b — CI/CD Pipeline
`cicd_target` is always **`codepipeline`** — AWS CodePipeline sourcing the app's **CodeCommit** repo
is the only supported target. Record any existing CI configuration found in the repo as a
source-estate fact (what it builds/tests), not as a target option. A repo that is not on CodeCommit must be mirrored there before Execution — flag it.

#### 6c — Transformation Decision (STRICT canonical schema — always emit this exact shape)

**MANDATORY strategy gate (evaluate FIRST):** code transformation applies ONLY to
code-changing strategies — **replatform, refactor, rearchitect**. If `strategy` is
**rehost, relocate, retire, retain, or repurchase**, the workload migrates AS-IS (or is
closed out), so you MUST emit **`"recommended": []` and `"gaps": []`** — even if the ATX
analysis found modernization opportunities. Those findings are DEFERRED (out of scope for
this migration) and must NOT appear as transformations or gaps. Note the deferral briefly in
the assessment narrative instead. Only populate the two lists below for
replatform/refactor/rearchitect.

From the code-analysis + modernization findings, classify every code-modernization
requirement into exactly one of two lists and emit them under a single `transformations`
object. Do NOT use any other key name (`transformation_steps`, `atx_transformations`,
`recommended_next_steps`, `recommendations`, …) for transformation data — the control
plane reads ONLY `transformations.recommended` and `transformations.gaps`.

```json
{
  "transformations": {
    "recommended": [
      {
        "td_name": "AWS/java-version-upgrade",
        "order": 1,
        "scope": "java/ — ~15 pom.xml files with <source>1.8</source>",
        "rationale": "Java 8 → 17; ATX catalog TD exists and applies.",
        "build_context": "targetJDKVersion=17",
        "effort": {"range": "3-5 days", "basis": "ATX-automated: runtime upgrade category, ~15 files in scope"}
      }
    ],
    "gaps": [
      {
        "requirement": ".NET 5.0 → .NET 8.0",
        "scope": "csharp/api-cors-lambda-crud-dynamodb/src/Lambdas (6 .csproj)",
        "reason": "No ATX custom transformation exists for this change.",
        "severity": "high",
        "effort": {"range": "8-13 days", "basis": "No ATX automation; engineer must author custom logic; severity=high raises the base estimate one tier"}
      }
    ]
  }
}
```

**Classification rules (MANDATORY):**
- `recommended` — a requirement goes here **only if** its `td_name` is an EXACT match in the
  **AWS Transform Catalog** tables below AND there is code-analysis evidence for it (see Pre-check
  Logic). `td_name` MUST be copied verbatim from the catalog (e.g. `AWS/java-aws-sdk-v1-to-v2`).
  Order them in execution sequence via `order`. These are the checked/executable transforms.
- `gaps` — a code change the analysis says is needed but which has **no matching TD in the catalog**
  (e.g. `.NET 5→8`, `JUnit 4→5`, a bespoke CORS/config code fix). The migration engineer must author
  a custom ATX definition for these; they render in **red** and are NOT executable until built.
- **Both arrays are ALWAYS present.** If the analysis found nothing to modernize (code already
  current), emit `"recommended": []` and `"gaps": []` — never omit the object, never invent entries.
- Never place a non-catalog name in `recommended`, and never place a catalog TD in `gaps`.

**Effort estimation (MANDATORY on every entry — per the recommendations/effort contract
R3/R4).** Every entry in `recommended` and `gaps` carries an `effort` object with `range` (a
ballpark, e.g. `"3-5 days"`) and `basis` (a one-line reason a reviewer can sanity-check). This is a
**deterministic lookup, not a guess repeated per app** — apply exactly these rules, in order:

1. **Base range by category** (for `recommended`, keyed off `td_name`'s catalog category from the
   AWS Transform Catalog tables below):
   | Category | Base range |
   |---|---|
   | Runtime Upgrades | 2-4 days |
   | SDK Migrations | 2-4 days |
   | Framework Upgrades & Migrations | 3-5 days |
   | Architecture & Optimization | 3-6 days |
2. **Base range for `gaps`** (no ATX automation exists, so always higher than the nearest
   `recommended` category — an engineer must author the logic from scratch): start from
   `severity` — `low` → 3-5 days, `medium` → 5-8 days, `high` → 8-13 days. `severity` is already a
   required field on every gap entry (see schema above), so this needs no new input.
3. **Scope adjustment.** If `scope` names a file/module count (e.g. "~15 pom.xml files",
   "6 .csproj"), and that count is large relative to the category's typical case (>10 files for
   Runtime/SDK, >5 modules for Framework/Architecture), bump the range one step up (e.g.
   `2-4 days` → `3-6 days`). Do not adjust down — only up, and only when a real count is present in
   `scope`; never invent a count that isn't there.
4. **`basis` must name which rule fired** (category + any scope adjustment for `recommended`;
   severity tier for `gaps`) — so a reviewer can see the estimate is rule-derived, not invented.
   Never write a `basis` that just restates the range with no reasoning (e.g. "3-5 days" is not a
   valid basis; "ATX-automated: runtime upgrade category" is).
5. **Strategy-gated requirements still apply.** If the MANDATORY strategy gate above emitted
   `"recommended": []` and `"gaps": []` (rehost/relocate/retire/retain/repurchase), there is nothing
   to estimate — do not emit an `effort` object anywhere in that case.

User reviews and can check/uncheck `recommended` before approving.

#### 6d — Cost Projection (ballpark, ARR-as-input)
Emit a `cost_projection` object per the decision-support contract (R3/R4/R5/R7).
**This object is MANDATORY output on every single assessment, with no exceptions** — it does not
become optional just because the app has no cost data on file:
```json
{
  "cost_projection": {
    "current_annual_cost": {
      "value": "monthly_infra*12 + annual_license, or null",
      "source": "customer_input",
      "status": "provided | missing"
    },
    "target_ballpark_annual_cost": {
      "value": "derived from target_compute class, no detailed sizing",
      "source": "amf_estimate"
    },
    "estimate_confidence": "low",
    "note": "Preliminary — refined in Design Section 9 Cost Analysis"
  }
}
```
**Worked example — app has NO `current_cost` data on file (this is the common case, not an edge
case — most Discovery records never got cost data filled in). Emit this exact shape, do not omit
the object:**
```json
{
  "cost_projection": {
    "current_annual_cost": {"value": null, "source": "customer_input", "status": "missing"},
    "target_ballpark_annual_cost": {"value": "~$18,000/yr (ECS Fargate, 2 vCPU/4GB, 1 task, low TPS)", "source": "amf_estimate"},
    "estimate_confidence": "low",
    "note": "current_cost not on file for this app — customer Finance input needed to compute ROI. Preliminary target estimate only; refined in Design Section 9."
  }
}
```
- `current_annual_cost.value` comes **only** from the app's existing `current_cost.monthly_infra` /
  `current_cost.annual_license` fields (the same data Discovery/portfolio already captures). Set
  `status: "missing"` if that data is absent or zero — **never invent, estimate, or infer the
  customer's current cost.** That number is customer-owned ground truth (mirrors how an external
  customer's Finance team supplies their own current provider cost rather than having AWS guess it).
- `target_ballpark_annual_cost` IS something Assessment may derive from the `target_compute` class —
  that side is AMF's own estimate, and it's fine for it to be approximate; label it as such. It MUST
  be priced for the CONFIRMED `target_compute` and `target_database` (never a compute/DB the strategy
  rules out — e.g. no Fargate/Lambda/Aurora basis for a rehost to EC2 with `same_as_source`).
- If `status` is `"missing"`, note it in the assessment's own confidence/risk narrative so it surfaces
  as an open item at the gate (do not silently omit the whole `cost_projection` object).
- **Missing `current_cost` data is a reason to set `status: "missing"` — it is never a reason to
  skip emitting `cost_projection` entirely.** The object with a `missing` status IS the correct,
  complete output for that case.

#### 6e — Qualification Criteria (transparency)
Emit a `qualification_criteria` array making the "is this a good AWS Transform candidate" call
inspectable, per R9. **This array is MANDATORY output on every single assessment** — populate it
from checks this skill already performs in Steps 2/3/5 (code analysis, dependency analysis); do not
invent new criteria or change the recommendation logic, and do not skip it because every check
happens to pass:
```json
{
  "qualification_criteria": [
    {"criterion": "dependency_count", "value": "<count from Step 5 dependency analysis>", "threshold": "<=20", "pass": true},
    {"criterion": "runtime_support", "value": "<language/runtime from code analysis>", "pass": true},
    {"criterion": "brownfield_integration_complexity", "value": "<derived from Step 5 upstream/downstream + shared-DB findings>", "pass": true}
  ]
}
```
**Worked example — a Java/Dropwizard app, 5 upstream deps, no shared DB, already on a current
runtime (every check passes — this is still mandatory output, not an optional "all clear" that can
be left out):**
```json
{
  "qualification_criteria": [
    {"criterion": "dependency_count", "value": 5, "threshold": "<=20", "pass": true},
    {"criterion": "runtime_support", "value": "Java 21 / Dropwizard 4.0.7", "pass": true},
    {"criterion": "brownfield_integration_complexity", "value": "low (no shared DB, 5 upstream deps, no circular dependencies)", "pass": true}
  ]
}
```
Map directly to existing Step 5 (dependency analysis: upstream/downstream deps, shared DBs, cross-app
network connections) and Step 2/3 (code analysis language/runtime, modernization readiness gaps) —
these are the checks that already inform `target_compute_rationale` and the strategy recommendation;
this array just exposes them as named, thresholded rows instead of leaving them buried in prose.

---

## AWS Transform Catalog (ATX Custom Definitions)

### Runtime Upgrades

| TD Name | Language | Status | Pre-check When |
|---------|----------|--------|----------------|
| `AWS/java-version-upgrade` | Java | GA | Source JDK < target (specify target: 11, 17, 21) |
| `AWS/python-version-upgrade` | Python | GA | Python 3.8 or 3.9 detected |
| `AWS/nodejs-version-upgrade` | Node.js | GA | Older Node.js version detected |
| `AWS/lambda-nodejs-runtime-upgrade` | Node.js Lambda | EA | Lambda runtime < nodejs24.x |
| `AWS/oracle-java-to-corretto` | Java | EA | Oracle JDK distribution detected |
| `AWS/ruby-upgrade` | Ruby | EA | Ruby 2.x detected |

### SDK Migrations

| TD Name | Language | Status | Pre-check When |
|---------|----------|--------|----------------|
| `AWS/java-aws-sdk-v1-to-v2` | Java | GA | AWS SDK v1 imports in code |
| `AWS/python-boto2-to-boto3` | Python | GA | boto2 imports detected |
| `AWS/nodejs-aws-sdk-v2-to-v3` | Node.js | GA | AWS SDK v2 (`aws-sdk`) package |

### Framework Upgrades & Migrations

| TD Name | Language | Status | Pre-check When |
|---------|----------|--------|----------------|
| `AWS/spring-boot-version-upgrade` | Java/Spring | GA | Spring Boot < 4.x |
| `AWS/JBoss-to-Spring-Boot` | Java/JBoss | EA | JBoss/WildFly app server detected |
| `AWS/angular-version-upgrade` | Angular | EA | Older Angular version |
| `AWS/vue.js-version-upgrade` | Vue.js | EA | Vue 2.x detected |
| `AWS/early-access-angular-to-react-migration` | Angular→React | EA | Migration preference |

### Architecture & Optimization

| TD Name | Language | Status | Pre-check When |
|---------|----------|--------|----------------|
| `AWS/java-performance-optimization` | Java | GA | Performance issues in analysis |
| `AWS/early-access-java-x86-to-graviton` | Java | EA | Targeting Graviton/ARM64 |
| `AWS/oracle-service-bus-to-aws` | Java/OSB | EA | Oracle Service Bus detected |
| `AWS/mulesoft-to-aws-native-java` | Java/MuleSoft | EA | MuleSoft flows detected (Java target) |
| `AWS/mulesoft-to-aws-native-python` | Python/MuleSoft | EA | MuleSoft flows detected (Python target) |
| `AWS/payshield-hsm-to-aws-payment-cryptography` | Java | GA | PayShield HSM usage |

### Observability

| TD Name | Language | Status | Pre-check When |
|---------|----------|--------|----------------|
| `AWS/early-access-log4j-to-slf4j-migration` | Java | EA | Log4j 1.x or 2.x detected |
| `AWS/datadog-monitors-to-cloudwatch-alarms` | IaC | EA | DataDog monitors in code |

### Security

Do not recommend `AWS/security-issue-fixer`: it needs scanner findings and AMF ships no scanner, so
the workbench skips it. Record dependency/secret-scanning needs as a gap (Amazon Inspector, see README).

### Codebase Analysis (run during assessment — not user-selectable)

| TD Name | Scope | Status | When |
|---------|-------|--------|------|
| `AWS/comprehensive-codebase-analysis` | All strategies | GA | Always (code analysis step) |
| `AWS/modernization-readiness-analysis` | Replatform/Refactor | EA | Step 3 |
| `AWS/agentic-readiness-analysis` | Refactor only | EA | Step 4 |

### GenAI & Language Migrations

| TD Name | Language | Status | Pre-check When |
|---------|----------|--------|----------------|
| `AWS/GenAI-to-Bedrock-Migration-Assessment` | GenAI | EA | OpenAI/Gemini SDK detected |
| `AWS/vba-to-python-migration` | VBA→Python | EA | VBA macros detected |

---

## Pre-check Logic (auto-select transforms based on findings)

After assessment reports are generated, apply these rules to pre-check transformations:

```
IF language == "Java":
  IF jdk_version < 17:
    CHECK "AWS/java-version-upgrade" (target: 17 or 21)
  IF jdk_distribution == "Oracle":
    CHECK "AWS/oracle-java-to-corretto"
  IF framework == "Spring Boot" AND spring_version < 4:
    CHECK "AWS/spring-boot-version-upgrade"
  IF aws_sdk_v1_detected:
    CHECK "AWS/java-aws-sdk-v1-to-v2"
  IF log4j_detected:
    CHECK "AWS/early-access-log4j-to-slf4j-migration"
  IF app_server in ["JBoss", "WildFly"]:
    CHECK "AWS/JBoss-to-Spring-Boot"
  IF performance_issues_flagged:
    CHECK "AWS/java-performance-optimization"
  IF target_graviton:
    CHECK "AWS/early-access-java-x86-to-graviton"

IF language == "Python":
  IF python_version in ["3.8", "3.9"]:
    CHECK "AWS/python-version-upgrade" (target: 3.12 or 3.13)
  IF boto2_detected:
    CHECK "AWS/python-boto2-to-boto3"

IF language == "Node.js":
  IF node_version < 20:
    CHECK "AWS/nodejs-version-upgrade" (target: 20 or 22)
  IF aws_sdk_v2_detected:
    CHECK "AWS/nodejs-aws-sdk-v2-to-v3"

IF language == "Ruby":
  IF ruby_version.startswith("2."):
    CHECK "AWS/ruby-upgrade"

IF framework == "Angular" AND angular_version < 18:
  CHECK "AWS/angular-version-upgrade"

IF framework == "Vue" AND vue_version.startswith("2."):
  CHECK "AWS/vue.js-version-upgrade"
```

---

## Customization Parameters

Each transformation supports `additionalPlanContext` for org-specific customization:
- `AWS/java-version-upgrade`: Target version (e.g., "Upgrade to Java 17")
- `AWS/spring-boot-version-upgrade`: Target version (e.g., "Target Spring Boot 3.2")
- `AWS/nodejs-version-upgrade`: Target version (e.g., "Upgrade to Node.js 20 LTS")
- `AWS/python-version-upgrade`: Target version (e.g., "Migrate to Python 3.12")

Also supports `buildCommand` override:
- Maven: `mvn clean install -DskipTests`
- Gradle: `gradle build`
- npm: `npm run build`
- pip: `pip install -e .`

---

## MANDATORY pre-output checklist

Before concluding this phase, confirm every item below is true. Do not finalize the assessment
until all five are satisfied — a missing one is a defect in this skill's output, not an
acceptable gap, and silently skipping an item here is exactly how a correct instruction earlier
in this file gets lost in a long response:

1. ☐ `transformations` object emitted with BOTH `recommended` and `gaps` arrays present (6c) —
   empty arrays are fine, a missing object is not.
2. ☐ Every `transformations.recommended`/`.gaps` entry carries an `effort` object (6c).
3. ☐ `cost_projection` object emitted (6d) — with `status: "missing"` if `current_cost` data is
   absent, never omitted outright.
4. ☐ `qualification_criteria` array emitted (6e) — populated from Steps 2/3/5 even when every
   check passes.
5. ☐ If this phase is requesting the `assessment_review` gate, a `decision_summary` object is
   attached to the `store_token` call (see "Gate decision-summary" below) — this is a SEPARATE
   step from writing the ASSESSMENT# record; finishing the report does not finish the phase.

## Output Contract

### Stored at assessment approval:
- `target_compute` — ECS Fargate / Lambda (replatform/refactor) or EC2 (rehost/relocate) (user confirmed)
- `target_compute_rationale` — why this was recommended
- `cicd_target` — `codepipeline` (the only supported target)
- `transformations` — the canonical object from Step 6c: `{ recommended: [{td_name, order, scope,
  rationale, build_context?, effort}], gaps: [{requirement, scope, reason, severity, effort}] }`.
  `recommended` td_names are the ordered ATX steps to execute; `gaps` are code changes with no ATX
  TD (engineer must author a custom definition). Every entry carries an `effort` ballpark (see 6c's
  effort-estimation rule). Always emit both arrays (empty if none).
- `transformation_config` — per-TD config (target version, build command, additionalPlanContext)
- `cost_projection` — ballpark ARR-vs-target estimate, see Step 6d (customer input + AMF estimate, kept distinct)
- `qualification_criteria` — inspectable checks behind the candidacy call, see Step 6e

### Feeds into:
- **Design document** — target architecture uses `target_compute` + `cicd_target`; Design §9 reconciles
  against `cost_projection.target_ballpark_annual_cost` (see design.md)
- **Execution phase** — runs `transformation_steps` in order via ATX, generates CI/CD via `cicd_target`

### Stored in DynamoDB:
- APP META: `target_compute`, `transformation_steps`
- ASSESSMENT# record: full reports + recommendations, including `cost_projection` and `qualification_criteria`

### Gate decision-summary (when this phase requests approval)
When the orchestration layer requests human approval for this phase (the
`assessment_review` gate — see `invoke_runtime.py`'s `_store_task_token`), attach a
`decision_summary` built from data this skill already produced, per
the decision-support contract (R1/R2). **This is a required argument on the
`store_token` tool call for this gate, not an optional enrichment** — a gate request without it
is an incomplete tool call, the same category of error as omitting `app_id` or `step`:
```json
{
  "what_changed": "e.g. 'Assessed 1 app: replatform to ECS Fargate, 3 transformation steps, dependency count 8/20'",
  "confidence_or_risk": "from strategy_confidence + any 'missing' cost_projection status + qualification_criteria failures",
  "if_approve": "Design phase generates V1 using the confirmed target_compute + transformation_steps",
  "if_reject": "Assessment re-runs with revised strategy/compute selection",
  "if_retry": "Re-run assessment analysis (e.g. after linking repos that were missing)"
}
```
**Worked example — this app's actual shape (refactor strategy, no cost data, 4 gaps, no automatable
transforms):**
```json
{
  "what_changed": "Assessed APP-CATALOG: confirmed refactor strategy retained (60% confidence); 0 ATX-automatable transformations, 4 custom-authoring gaps (Dropwizard→Spring Boot framework migration + 3 infra-coupling fixes); current_cost not on file.",
  "confidence_or_risk": "strategy_confidence=60% (missing business context fields); cost_projection.current_annual_cost status=missing — customer Finance input needed; qualification_criteria all pass (low dependency count, current runtime).",
  "if_approve": "Design phase generates V1 using ECS Fargate target compute + the 4 custom-authoring gaps as scoped work items",
  "if_reject": "Assessment re-runs with revised strategy or compute selection",
  "if_retry": "Re-run assessment analysis after linking current_cost or additional repos"
}
```
This is populated from existing output — no new analysis — and is a small addition on top of the
report this skill already generates, not a replacement for it.

---

## Quality Bar

- All applicable ATX analyses complete (not skipped silently)
- Strategy confidence updated based on findings
- Target compute recommendation provided with rationale
- Transformation steps pre-checked based on actual code findings (not guessed)
- Blockers explicitly listed (not buried in prose)
- Assessment status set to "assessed" in app META record
- `cost_projection.current_annual_cost` reflects actual `current_cost` data or is marked `missing` — never invented
- `qualification_criteria` reflects real checks already performed in Steps 2/3/5 — never placeholder rows
- Every `transformations.recommended` / `.gaps` entry carries an `effort` object with a rule-derived
  `basis` (category/severity/scope-adjustment) — never a flat number repeated across apps
- `cost_projection` and `qualification_criteria` are present on EVERY assessment, with no exceptions
  for apps with no cost data or apps where every qualification check passes
- `decision_summary` is attached to the `store_token` call for the `assessment_review` gate on
  EVERY approval request — producing the ASSESSMENT# record alone does not complete this phase

## Anti-patterns

- Do NOT recommend Refactor without code visibility (repos linked)
- Do NOT run modernization analysis for Rehost strategy
- Do NOT skip dependency analysis — it determines wave ordering
- Do NOT mark assessment "complete" if ATX failed — surface the error
- Do NOT pre-check transformations without evidence from code analysis
- Do NOT recommend EKS, EC2 (for replatform/refactor) or a CI/CD other than CodePipeline — they are not selectable
- Do NOT write a `target_compute_rationale` that names a compute different from an already-confirmed
  `target_compute` META value — the user's selection wins; the rationale must justify it, not contradict it
- Do NOT estimate, guess, or infer `cost_projection.current_annual_cost` — if `current_cost` data is
  absent, set `status: "missing"` and stop; that figure must come from the customer, never from AMF
- Do NOT omit `effort` from a `transformations.recommended`/`.gaps` entry, and do NOT write the same
  range for every entry regardless of category/severity/scope — apply the 6c effort-estimation rule
  and show the reasoning in `basis`
- Do NOT omit `cost_projection` because `current_cost` data is absent — emit the object with
  `status: "missing"`; an absent object and a `missing` status are not the same thing, and only the
  second one is correct
- Do NOT omit `qualification_criteria` because every check happens to pass — "all green" is a valid
  result that still needs to be shown, not a reason to skip the array
- Do NOT request the `assessment_review` gate (call `store_token`) without a `decision_summary`
  argument — this is a required field on that tool call, not an optional add-on, and omitting it is
  the same category of mistake as omitting `app_id`
