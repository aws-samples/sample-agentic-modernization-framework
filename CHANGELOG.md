# Changelog

All notable changes to this project are documented here.

## 0.1.0 — initial public release (unreleased)

First release of AMF as sample code (proof of concept; see the README disclaimer and `SECURITY.md`).

**Capabilities**
- One Amazon Bedrock AgentCore Harness with 9 phase playbooks; Lambda-backed MCP Gateway tools (5 targets).
- AWS Step Functions journey: Discovery → Assessment → Design → Execution → Validation → Cutover → Handover,
  with 7 human approval gates, deterministic pre-hooks and post-checks, and close-out paths for retire,
  retain and repurchase.
- AWS Transform code analysis and transformation on Amazon ECS Fargate.
- Template-driven Terraform (VPC, ECS Fargate, Lambda, EC2, RDS/Aurora, DynamoDB, S3) and AWS CodePipeline
  generation, validated with `terraform validate` + `plan` and committed to a reviewable migration branch.
- Deterministic wave planning on a DynamoDB-derived dependency graph (Amazon Neptune optional).
- React/CloudScape portal with Cognito authentication and role-based access (Admin, Engineer, Viewer).

**Safety defaults**
- Deploy is off by default (`enable_deploy = false`); when enabled, only an Admin's approval of the exact
  reviewed commit applies it, into allow-listed accounts only.
- AWS services only: source repositories are AWS CodeCommit; no third-party scanners or SaaS integrations.
- Scoped IAM, a customer-managed KMS key, TLS-only S3 buckets, an internal ALB behind CloudFront when hosted.
