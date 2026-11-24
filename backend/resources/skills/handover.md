---
name: handover
description: Handover agent skill — generates operational runbook, architecture documentation with actual resource ARNs, DR playbook, and knowledge transfer package for operations team.
metadata:
  type: skill
  phase: handover
  agent: handover
---

# Skill: Migration Handover

## When this skill fires

- Validation phase passed (smoke + security + performance all green)
- Workflow enters Phase_Handover in Step Functions
- This is the FINAL step before marking migration complete

## Procedure

### Step 1 — Gather Deployed State
Read from DynamoDB + S3:
- All resource ARNs created during execution (from app META)
- Design document (final version with actual values)
- Validation reports (smoke, security, performance)
- CI/CD pipeline URL
- Monitoring dashboard URL

### Step 2 — Generate Operational Runbook
Create `runbook.md` with the following sections:

```markdown
# Operational Runbook: {app_name} ({app_id})

## 1. Service Overview
- Application: {app_name}
- Environment: production
- Compute: {target_compute} ({cluster_arn / function_arn})
- Database: {db_endpoint}
- Load Balancer: {alb_dns}
- CI/CD: {cicd_target} ({pipeline_url})

## 2. Health Monitoring
- Health check URL: {alb_dns}{health_check_path}
- CloudWatch Dashboard: {dashboard_url}
- Log Group: {log_group}
- Key alarms:
  - CPU > 80% → auto-scale (info)
  - Error rate > 5% → page on-call (critical)
  - P95 latency > {latency_sla}ms → investigate (warning)
  - Unhealthy targets > 0 → immediate (critical)

## 3. Common Operations
### Restart service
- ECS: aws ecs update-service --force-new-deployment
- Lambda: no restart needed (stateless)
- EC2 (rehost/relocate): re-run the CodeDeploy deployment, or `systemctl restart <service>` via SSM

### Scale up/down
- ECS: aws ecs update-service --desired-count {N}
- Lambda: update provisioned concurrency
- Auto-scaling handles most cases

### View logs
- CloudWatch: aws logs tail {log_group} --follow
- Filter errors: aws logs filter-log-events --log-group {log_group} --filter-pattern "ERROR"

### Deploy new version
- Push to main branch → CI/CD triggers automatically
- Blue/green: CodeDeploy shifts traffic after health check passes
- Rollback: aws deploy stop-deployment (or revert git commit)

## 4. Incident Response
### Application not responding
1. Check ALB target health: aws elbv2 describe-target-health
2. Check ECS task status: aws ecs describe-tasks
3. Check logs for errors: aws logs tail {log_group}
4. If task crashing: check memory/CPU limits, recent deployments
5. Rollback if needed: aws ecs update-service --task-definition {previous_td}

### High latency
1. Check CloudWatch P95 latency metric
2. Check database connections: SELECT count(*) FROM pg_stat_activity
3. Check auto-scaling: is desired count at max?
4. Check NAT Gateway throughput (if private subnets)
5. Scale horizontally or vertically if needed

### Database issues
1. Check RDS/Aurora status: aws rds describe-db-instances
2. Check connections: CloudWatch DatabaseConnections metric
3. Failover (Aurora): aws rds failover-db-cluster
4. Point-in-time recovery: aws rds restore-db-instance-to-point-in-time

## 5. Disaster Recovery
- RPO: {rpo} | RTO: {rto}
- Backup: Aurora automated (35-day retention) + PITR
- Multi-AZ: {multi_az status}
- Failover: automatic for Aurora Multi-AZ (<60s)
- Cross-region: {dr_region if configured}

## 6. Rollback Procedure
1. Identify the issue (deployment? infra? data?)
2. For deployment rollback:
   - ECS: update-service with previous task definition revision
   - Lambda: update alias to previous version
3. For infrastructure rollback:
   - terraform plan (shows drift) → terraform apply with previous state
4. For database rollback:
   - PITR to timestamp before issue
   - DMS reverse replication (if available)

## 7. Contacts
- App Owner: {owner_name} ({owner_email})
- On-call: {oncall_rotation}
- Escalation: {escalation_path}
- AWS Support: Case via AWS Console (Business/Enterprise support)

## 8. Architecture Reference
- VPC: {vpc_id} ({vpc_cidr})
- Subnets: public={public_subnets}, private={private_subnets}
- Security Groups: ALB={alb_sg}, App={app_sg}, DB={db_sg}
- IAM Roles: Task={task_role_arn}, Execution={exec_role_arn}
- KMS Key: {kms_key_arn}
- ECR: {ecr_url}
```

### Step 3 — Generate Decommission Plan
For the old infrastructure:
1. List all source servers (from machine_ids)
2. Identify what can be decommissioned immediately vs. after bake period
3. Recommended bake period: 2 weeks (keep old infra running, no traffic)
4. Schedule: Week 1 → monitor new, Week 2 → verify no issues, Week 3 → decommission old

### Step 4 — Store Handover Package
Store all documents in S3:
- `applications/{app_id}/handover/runbook.md`
- `applications/{app_id}/handover/decommission-plan.md`
- `applications/{app_id}/handover/architecture-reference.md`

Update DynamoDB:
- `handover_status`: "completed"
- `handover_at`: timestamp
- `migration_completed_at`: timestamp

## Output Contract

- Runbook: comprehensive operational guide with actual commands and ARNs
- Decommission plan: timeline for shutting down old infrastructure
- Architecture reference: all resource ARNs, network diagram, access info
- All stored in S3 and referenced in final design document version

## Quality Bar

- Runbook must have ACTUAL resource ARNs (not placeholders)
- Every incident scenario has step-by-step response
- Rollback procedure tested (not just documented)
- Contact information populated (at least app owner)
- Decommission plan has specific dates/timeline

## Anti-patterns

- Do NOT generate runbook with placeholder ARNs — read actual deployed state
- Do NOT skip decommission plan — old infrastructure costs money
- Do NOT mark complete without storing documents in S3
- Do NOT copy-paste generic runbook — must be app-specific with real values
