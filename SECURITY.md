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
- **Cognito MFA is optional by default** (`cognito_mfa = "OPTIONAL"`, TOTP). Users who don't enroll sign in with
  a password only. Set `cognito_mfa = "ON"` (`COGNITO_MFA=ON scripts/setup.sh ...`) for any shared environment.
- **S3 bucket name:** if `S3_BUCKET` isn't set, the backend derives the data bucket name from the account ID (via
  STS) as `{prefix}-data-{account}-{region}`, and refuses to start S3 work if STS fails. Deployments set
  `S3_BUCKET` explicitly.
- **Browser tokens:** the SPA keeps the Cognito access and ID tokens in `sessionStorage` (per tab, cleared on
  sign-out and when the tab closes). Any script that runs in the page could read them; the strict CSP limits
  that risk.

## Accepted security debt (non-production sample; review by 2027-02-22)

AMF is a sample proof of concept. It shows how an agentic solution (Amazon Bedrock AgentCore, AWS Step
Functions, AWS Transform) can run an application migration end to end, in a sandbox account with
synthetic data. The items below are accepted for that purpose: each keeps the sample simple to deploy
and read, and each has a compensating control. Anyone building on AMF for real workloads must close
them first. Numbers are stable; fixed items stay listed for reference.

| # | Item | Where | Status and reason |
|---|---|---|---|
| 1 | `transform-custom:*` on `Resource: "*"` (worker role) | `infra/modules/worker-task/main.tf` (`TransformCustomCli`) | **Accepted (sample).** Several AWS Transform CLI actions have no resource type, and AWS-managed transformation definitions are AWS-owned packages, so `*` is required for resources. The CLI's agent loop calls actions that aren't CloudTrail management events, so a complete action list can't be verified, and a missing action would break the 10-25 minute analysis runs the sample demonstrates. Control: sharing, resource-policy and delete actions are explicitly denied, guarded by a test. For production: enumerate the actions once AWS Transform documents them. |
| 2 | `autoscaling:*` and `elasticloadbalancing:*` (app permissions boundary) | `infra/environments/agentcore/deploy.tf` (`CodeDeployInPlace`) | **Accepted (sample).** Applies only when deploy is enabled (off by default) and only to this account's ARNs. CodeDeploy in-place deployments need the `AWSCodeDeployRole` action set; listing it explicitly doesn't fit IAM's 6,144-character limit alongside the other boundary statements. For production: split the boundary or drop EC2 in-place deploys. |
| 3 | AWS managed `ReadOnlyAccess` on the worker role | `infra/modules/worker-task/main.tf` | **Accepted (sample).** `terraform plan` refreshes data sources across whatever services the generated IaC uses; a hand-written read list would fail plans whenever a template changes. Control: read-only, no write is granted, the sample runs in a sandbox account. For production: a plan-scoped read policy. |
| 4 | S3 bucket-name fallback | `backend/src/config.py` (`get_s3_bucket`) | **Fixed.** Without `S3_BUCKET` the name is derived from the account ID only; if STS fails it raises instead of guessing an account-less name. `setup.sh` and Terraform always set `S3_BUCKET`. |
| 5 | Cognito MFA | `infra/environments/agentcore/cognito.tf` | **Configurable.** `cognito_mfa` (`COGNITO_MFA` in `setup.sh`): `OPTIONAL` by default so a sandbox PoC needs no authenticator app; `ON` requires TOTP for everyone and is recommended for any hosted demo other people sign in to. Local runs don't use Cognito. |
| 6 | Cognito tokens in `sessionStorage` | `frontend/src/auth/cognito.ts` | **Accepted (sample).** Per tab, cleared on sign-out and tab close; the strict CSP (`default-src 'self'`, no inline script) limits script injection. A backend-for-frontend with HttpOnly cookies is a separate auth component, beyond what this sample sets out to show. |
| 7 | Broad `except Exception` (about 250 places) | `backend/src`, `infra/environments/agentcore/lambda-src` | **Accepted (sample).** Mostly degrade paths that log and return a safe default so one failed lookup doesn't fail a long-running migration step. Correctness-critical checks (pre-hooks, post-checks, approvals, validation) fail closed. For production: narrow to the expected exceptions. |
| 8 | Python dependency ranges aren't locked; `pip install --upgrade pip` in the image | `pyproject.toml`, `docker/Dockerfile.worker` | **Accepted (sample).** Bounded ranges keep the sample installable as dependencies get security releases; frontend dependencies are locked (`pnpm-lock.yaml`). For production: a hash-pinned lock file and a pinned pip. |
| 9 | AWS CLI and AWS Transform CLI installers | `docker/Dockerfile.worker` | **Fixed.** The AWS CLI is pinned and verified against the AWS CLI team's PGP signature (key fingerprint checked); the AWS Transform CLI is pinned, its installer is checked against a pinned sha256 before it runs, and the installer verifies the archive checksum. |
| 10 | Resource tags | `infra/` | **Fixed.** Provider `default_tags` (`project`, `environment`, `managed_by`, plus `extra_tags`) on every taggable resource; ECS worker tasks and the API service propagate them. |
| 11 | S3 access logging is off for the data and site buckets | `infra/modules/s3/main.tf`, `infra/environments/agentcore/public-hosting.tf` | **Accepted (sample).** A sandbox PoC with synthetic data; logging needs a separate log bucket and its lifecycle. Control: both buckets block public access, require TLS and use server-side encryption; CloudTrail records bucket management. For production: a log bucket. |
| 12 | IAM role descriptions | `infra/` | **Fixed.** Every AMF IAM role has a description. |
