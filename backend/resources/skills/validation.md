---
name: validation
description: Validation agent skill — runs post-execution tests to confirm the migrated application works correctly. Covers smoke testing, security scanning, and performance validation against SLAs.
metadata:
  type: skill
  phase: validation
  agent: validation
---

# Skill: Migration Validation

## When this skill fires

- Execution phase completed (code transformed, infra provisioned, DB migrated, CI/CD set up)
- Workflow enters Phase_Validation in Step Functions
- 3 parallel tasks: smoke test, security scan, performance test

## Procedure

### Step 1 — Smoke Test
Validate the application is running and accessible:
1. Get ALB DNS / API Gateway URL / EC2 IP from app META (resource ARNs stored during execution)
2. Hit health check endpoint (from design doc: `/health`, `/actuator/health`, etc.)
3. Verify HTTP 200 response
4. Check response body contains expected health indicator
5. Verify container/function logs have no startup errors (check CloudWatch last 5 min)
6. Test 2-3 critical API endpoints (if documented in design doc)

**Pass criteria:**
- Health check returns 200
- No ERROR/FATAL in startup logs
- Response time < 5 seconds

**Fail criteria:**
- Health check timeout or non-200
- Container crash loop (restartCount > 0)
- Critical errors in logs

### Step 2 — Security Scan
Run automated security checks:
1. **Container image scan** — check ECR scan results if Amazon Inspector enhanced scanning is enabled; otherwise report "not scanned"
   - Critical: 0 allowed
   - High: document, warn if > 5
2. **Dependency vulnerabilities** — from ATX reports or `npm audit` / `mvn dependency-check`
3. **IAM policy review** — check task role doesn't have `*` actions on `*` resources
4. **Encryption verification** — confirm:
   - DB encryption at rest (KMS)
   - S3 bucket encryption enabled
   - ALB uses HTTPS (ACM cert attached)
   - Secrets in Secrets Manager (not env vars)
5. **Network security** — verify:
   - No security group allows 0.0.0.0/0 on non-standard ports
   - DB security group only allows access from app tier
   - No public IP on compute (except ALB)

**Pass criteria:**
- 0 critical vulnerabilities
- No wildcard IAM policies
- All data encrypted at rest + in transit

**Report format:**
```markdown
## Security Validation Report
- Critical vulnerabilities: 0 ✅
- High vulnerabilities: 2 ⚠️ (documented, accepted risk)
- IAM policy: least privilege ✅
- Encryption: at rest + in transit ✅
- Network: no over-permissive rules ✅
- Overall: PASS
```

### Step 3 — Performance Test
Validate against application SLAs:
1. Read `performance` from app META: `peak_tps`, `latency_sla_ms`, `uptime_sla`
2. Run load test at 50% of peak_tps for 2 minutes (warmup)
3. Ramp to 100% peak_tps for 5 minutes
4. Measure:
   - P50, P95, P99 latency
   - Error rate (HTTP 5xx)
   - CPU/memory utilization under load
   - Auto-scaling behavior (did it scale?)
5. Compare against SLA thresholds

**Pass criteria:**
- P95 latency < `latency_sla_ms`
- Error rate < 1%
- No OOM kills or crashes under load
- Auto-scaling triggered appropriately

**Tools:**
- For basic: `curl` + `ab` (Apache Bench) or `hey`
- For production: Artillery, Locust, or k6

**Report format:**
```markdown
## Performance Validation Report
- Peak TPS tested: {peak_tps}
- P50 latency: {x}ms ✅ (SLA: {latency_sla_ms}ms)
- P95 latency: {x}ms ✅
- P99 latency: {x}ms
- Error rate: {x}% ✅ (threshold: <1%)
- Max CPU: {x}% | Max Memory: {x}%
- Auto-scaling: triggered at {x}% CPU ✅
- Overall: PASS
```

## Output Contract

Stored in DynamoDB + S3:
- `APP#{app_id}` META: `validation_status` = passed/failed, `validation_at`
- S3: `applications/{app_id}/validation/smoke-report.md`
- S3: `applications/{app_id}/validation/security-report.md`
- S3: `applications/{app_id}/validation/performance-report.md`

### Gate decision-summary (when this phase requests approval)
When the orchestration layer requests human approval for this phase (the
`validation_review` gate — see `invoke_runtime.py`'s `_store_task_token`), attach a
`decision_summary` built from data this skill already produced, per
the decision-support contract (R1/R2):
```json
{
  "what_changed": "from the 3 validation reports — e.g. 'Smoke PASS (health 200); Security PASS (0 critical, 2 high accepted); Performance PASS (P95 180ms < 250ms SLA)'",
  "confidence_or_risk": "from the pass/fail per category + any accepted high-severity findings or SLA margins",
  "if_approve": "Proceed to cutover / handover (runbook generation)",
  "if_reject": "Return to execution to remediate the failed validation category",
  "if_retry": "Re-run the specific failed validation (smoke / security / performance)"
}
```
This is populated from existing report output — no new analysis.

## Quality Bar

- All 3 validations must pass for overall PASS
- Security scan MUST have 0 critical vulnerabilities
- Performance test MUST meet SLA thresholds
- Smoke test MUST verify health check + clean logs
- Reports stored in S3 with evidence (not just pass/fail)

## Anti-patterns

- Do NOT skip validation to save time
- Do NOT mark PASS without actual evidence (URL, response, metrics)
- Do NOT test at lower than documented peak_tps
- Do NOT ignore high vulnerabilities — document and get acceptance
