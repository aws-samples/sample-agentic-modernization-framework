# Security

## Reporting a vulnerability

If you discover a potential security issue in this project, notify AWS/Amazon Security through the
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/) or by
email to [aws-security@amazon.com](mailto:aws-security@amazon.com).
Please **do not** create a public GitHub issue.

## Proof of concept: not for production use

The Agentic Modernization Framework (AMF) is **sample code for demonstration and learning**. It is
not a supported AWS product or service, and it is **not intended for production use**. Before you
deploy any part of it, you must do your own security review and testing, and you must adapt the
controls to your organisation's requirements.

You are responsible for:

- Deploying AMF only into a **dedicated, non-production sandbox AWS account** that you own and
  control, not into accounts that run production workloads or hold production data.
- Using **synthetic or anonymised data only**. Do not upload real server inventories (AWS ADS
  exports), credentials, personal data, or other confidential data.
- Reviewing every generated artefact (Terraform, CI/CD definitions, transformed code) before
  approving a deploy, and deleting the resources when you're done (`POST /workflow/teardown`,
  `scripts/cleanup.sh infra`).
- The cost of the AWS resources you create, and compliance with the laws and policies that apply
  to you.

This follows the AWS [Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/).

## Security design (summary)

| Area | Control | Where |
|---|---|---|
| Agent actions | Correctness-critical steps run as deterministic code; the agent can't deploy or destroy (no Gateway tool), and every deploy needs a human approval of the exact commit | `step-functions/migration-workflow.v2.asl.json`, `tool_execution.py` |
| Deploy scope | Only into `DEPLOY_ALLOWED_ACCOUNTS` (default: this account). App IAM roles must carry the app permissions boundary: its Allow statements are limited to this account (S3 by `aws:ResourceAccount`, since S3 ARNs carry no account ID), except actions with no resource-level permissions; it denies AMF's own tables, buckets, secrets, state machine, ECS, ECR, CodeCommit and platform KMS key. Admin/FullAccess managed policies can't be attached, and app roles can't be assumed by the deploy role | `infra/environments/agentcore/deploy.tf` |
| AuthN/AuthZ | Cognito Hosted UI (code + PKCE). The API validates JWTs and enforces RBAC on every route. `DEV_AUTH` is local-only, and both the API and Terraform refuse it in hosted mode | `backend/src/auth/`, `public-hosting.tf` |
| MCP Gateway | `AWS_IAM` (SigV4) authorizer | `infra/environments/agentcore/main.tf` |
| Network | Hosted mode is opt-in. The ALB is internal (private subnets), and CloudFront reaches it only through a VPC origin. The viewer side needs an ACM certificate with a TLSv1.2_2021 minimum, there's an optional WAF IP allowlist, and security headers + CSP are set | `public-hosting.tf` |
| Encryption | A customer-managed KMS key (rotation on) covers DynamoDB, ECR, CloudWatch Logs and Lambda env vars. S3 uses SSE with public access blocked and TLS-only bucket policies | `kms.tf`, `infra/modules/*` |
| Untrusted repos | Repo builds run as an unprivileged user with no AWS credentials (`worker/sandbox.py`). Git clones AWS CodeCommit only, authenticated by the task role with no token (`worker/gitcmd.py`, `worker/argsafe.py`). AMF runs no third-party scanner; see README "Security scanning" for Amazon Inspector | `backend/src/worker/` |
| Generated IaC | Templates use a per-app KMS key, TLS 1.2+/1.3 listener policies, access logs and permissions boundaries, and the network-exposure policy gates public exposure | `backend/resources/templates/`, `backend/src/core/exposure_policy.py` |
| Scanning | ASH (bandit, checkov, semgrep, detect-secrets, grype) runs in CI with `fail_on_findings: true`. Suppressions are inline, each with a reason | `ash_config.yaml`, `infra/.checkov.yaml` |

## Known limitations (accepted for a PoC)

These are documented and deliberately out of scope. Address them before any wider use:

- AWS Transform (ATX) runs a repository's build inside the AWS-managed CLI with the worker task's
  credentials.
- `terraform plan`/`apply` run in the worker with AWS credentials on the generated IaC committed to the
  migration branch. A malicious commit on that branch could run code through Terraform providers or data
  sources, so protect the branch and review the commit before you approve the deploy gate. Deploy is off by
  default (`enable_deploy = false`): AMF then creates no deploy role and applies nothing.
- Tool Lambdas aren't VPC-attached, and they have no reserved concurrency, DLQ or code signing.
- The worker and API tasks use public subnets with a public IP for egress (no NAT gateway).
  Their security groups admit no inbound traffic from the internet.
- No VPC flow logs, CloudFront or WAF logging, or alarms and dashboards.
- The S3 data bucket uses the AWS-managed `aws/s3` key rather than the platform CMK.
- **App roles are not isolated from each other.** The permissions boundary separates apps from AMF and from other
  accounts, not from other apps in the same account: CodeCommit can't be scoped per app (linked repo names come
  from the customer), and actions with no resource type are account-wide. Use one sandbox account for AMF.
- **Broad grants that remain, with reasons:**
  - Worker task role: `transform-custom:*` on `*` (the ATX CLI calls actions with no resource type, and
    AWS-managed transformation definitions are AWS-owned); sharing, resource-policy and delete actions are
    explicitly denied. It also has the AWS managed `ReadOnlyAccess` policy, which `terraform plan` uses to
    read existing resources; this lets the worker read most resources in the account.
  - Harness runtime role: `bedrock-agentcore` is limited to `InvokeGateway` on this deployment's gateway and
    the workload-identity token actions; Bedrock model invocation is limited to foundation models and this
    account's inference profiles.
- **Cognito MFA is optional** (`mfa_configuration = "OPTIONAL"`, TOTP). Users who don't enroll sign in with
  a password only. Set it to `ON` for any shared environment.
- **S3 bucket-name fallback:** if `S3_BUCKET` isn't set, the backend derives the data bucket name from the
  account ID (via STS) as `{prefix}-data-{account}-{region}`. Deployments set `S3_BUCKET` explicitly; set it
  for local runs too.
- **Browser tokens:** the SPA keeps the Cognito access and ID tokens in `sessionStorage` (per tab, cleared on
  sign-out and when the tab closes). Any script that runs in the page could read them; the strict CSP limits
  that risk.

## Accepted security debt (non-production sample; fix by 2027-02-22)

Recorded on 2026-11-24 as accepted for this sample and due within 90 days. Each entry says where it is and why it's
accepted for now.

| # | Item | Where | Why accepted for now |
|---|---|---|---|
| 1 | `transform-custom:*` on `Resource: "*"` (worker role) | `infra/modules/worker-task/main.tf` (`TransformCustomCli`) | `*` is required for resources: several AWS Transform CLI actions have no resource type, and AWS-managed transformation definitions are packages owned by AWS, not by this account. The full action set is kept because the CLI's agent loop calls actions that aren't logged as CloudTrail management events, so the complete list can't be verified from CloudTrail yet. Sharing, resource-policy and delete actions are explicitly denied. Fix: enumerate the actions from a full CLI run. |
| 2 | `autoscaling:*` and `elasticloadbalancing:*` (app permissions boundary) | `infra/environments/agentcore/deploy.tf` (`CodeDeployInPlace`) | Limited to this account's ARNs. CodeDeploy in-place deployments need the `AWSCodeDeployRole` action set (ASG lifecycle hooks, ELB target registration); listing those explicitly doesn't fit IAM's 6,144-character managed-policy limit alongside the other boundary statements. Applies only when deploy is enabled. Fix: restructure the boundary to make room. |
| 3 | AWS managed `ReadOnlyAccess` on the worker role | `infra/modules/worker-task/main.tf` | `terraform plan` refreshes data sources across whatever services the generated IaC uses. This lets the worker read most resources in the account. Fix: a plan-scoped read policy. |
| 4 | S3 bucket-name fallback when `S3_BUCKET` is unset | `backend/src/config.py` (`get_s3_bucket`) | Deployments always set `S3_BUCKET`; the STS-derived `{prefix}-data-{account}-{region}` name is a local-dev convenience and isn't cached when STS fails. Fix: require `S3_BUCKET`. |
| 5 | Cognito MFA is optional | `infra/environments/agentcore/cognito.tf` (`mfa_configuration = "OPTIONAL"`) | Keeps sandbox sign-in simple; TOTP is available to every user. Fix: default to `ON`. |
| 6 | Cognito tokens in `sessionStorage` | `frontend/src/auth/cognito.ts` | Per tab and cleared on sign-out and tab close; the strict CSP limits script injection. Fix: a backend-for-frontend with HttpOnly cookies. |
| 7 | Broad `except Exception` (about 250 places) | `backend/src`, `infra/environments/agentcore/lambda-src` | Mostly degrade paths that log and return a safe default rather than failing a workflow step. Fix: narrow to expected exceptions. |
| 8 | Python dependency ranges aren't locked; `pip install --upgrade pip` in the image | `pyproject.toml`, `docker/Dockerfile.worker` | Frontend dependencies are locked (`pnpm-lock.yaml`); Python runtime dependencies use bounded ranges. Fix: a hash-pinned lock file and a pinned pip. |
| 9 | AWS CLI and AWS Transform CLI installers aren't checksum-verified | `docker/Dockerfile.worker` | Both are downloaded over HTTPS from AWS-owned endpoints (`awscli.amazonaws.com`, `transform-cli.awsstatic.com`); Terraform, Maven and Node.js are pinned and checksum-verified. Fix: verify the AWS CLI's PGP signature and pin the AWS Transform CLI version. |
| 10 | Resource tags are incomplete | `infra/` | Not a security control in this sample; resource names carry the prefix. Fix: `default_tags` on the providers. |
| 11 | S3 access logging is off for the data and site buckets | `infra/modules/s3/main.tf`, `infra/environments/agentcore/public-hosting.tf` | Needs a separate log bucket; both buckets block public access, require TLS and use server-side encryption. Fix: a log bucket. |
| 12 | IAM roles have no descriptions | `infra/` | Role names follow `{prefix}-<purpose>`. Fix: add descriptions. |

