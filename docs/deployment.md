# Deployment Guide

> **Proof of concept, not for production use.** Deploy only into a dedicated, non-production sandbox
> account and use synthetic data only. Read [SECURITY.md](../SECURITY.md) first. Hosted mode
> (`ENABLE_PUBLIC_HOSTING=true`) needs `PORTAL_DOMAIN_NAME` and a us-east-1
> `PORTAL_ACM_CERTIFICATE_ARN`; the API's ALB is internal and is reached only through a CloudFront VPC origin.

AMF deploys as a **single, prefixed AgentCore environment**. Terraform
(`infra/environments/agentcore`) provisions everything **except** the Terraform remote-state S3
bucket and the DynamoDB lock table. `scripts/setup.sh` bootstraps those two first. All resource
names derive from one prefix.

## One command

```bash
scripts/setup.sh                 # prefix defaults to "amf"
scripts/setup.sh myprefix        # custom prefix
```

The script runs these steps in order:

1. **Prerequisites.** Checks for `python3`, `aws`, `docker` and `terraform`, then derives
   `ACCOUNT_ID` and `REGION`.
2. **`.env` + Python venv.** Copies `.env.example` and runs `pip install -e ".[dev]"`. These are
   for running the API locally against the cloud resources.
3. **Bootstrap backend.** Creates the state bucket `${prefix}-tf-state-${account}-${region}`
   (versioned, encrypted, public access blocked) and the lock table `${prefix}_tf_lock`.
4. **`terraform apply`.** Provisions all of the following:
   - networking: VPC (2 public subnets + IGW)
   - ECS: cluster, worker and API task definitions
   - storage: DynamoDB, S3, ECR
   - Lambdas: the 5 MCP tool Lambdas + the `invoke-runtime` bridge
   - AgentCore: the MCP Gateway (5 targets) and the Harness
   - the `${prefix}-agentcore-workflow` state machine
   - Transaction Search
   - with hosting enabled: CloudFront, ALB, the ECS API service and Cognito

   It then syncs `backend/resources/templates/` to `s3://<data-bucket>/config/templates/`.
   `provision_infrastructure` and `generate_cicd` render from that location.
5. **Image.** Builds `docker/Dockerfile.worker` (`linux/amd64`) and pushes it to `${prefix}-worker`
   as `:latest` plus a timestamp tag.
   - With `ENABLE_PUBLIC_HOSTING=true`, it force-redeploys `${prefix}-api-service`, which runs the
     same image.
   - **6b** (hosting only) builds the SPA with `frontend/.env.production.local` and uploads it to the
     site bucket and CloudFront.
6. **`.env` sync.** Writes `PROJECT_PREFIX`, `AWS_REGION`, `DYNAMODB_TABLE`, `S3_BUCKET`,
   `SFN_STATE_MACHINE_NAME`, `AGENTCORE_INVOKE_LAMBDA`, `AGENTCORE_GATEWAY_ID`/`_URL`,
   `AGENTCORE_HARNESS_ARN` and `GRAPH_BACKEND=dynamodb`. It also writes `DEV_AUTH` and, when
   Cognito is on, the Cognito ids.

### Hosted / UAT toggles (environment variables for `setup.sh`)

| Variable | Default | Effect |
|---|---|---|
| `ENABLE_PUBLIC_HOSTING` | `false` | CloudFront + ALB + ECS API service + S3 site; builds and deploys the UI |
| `ENABLE_COGNITO` | = `ENABLE_PUBLIC_HOSTING` | Cognito pool, Admin/Engineer/Viewer groups and seed admin; sets `DEV_AUTH=false` |
| `ADMIN_EMAIL` | `admin@example.com` | Seed admin username and email |
| `ENABLE_DEPLOY` | `false` | `true` creates the deploy role; approving `execution_review` then applies the reviewed commit |

## Terraform layout

```
infra/environments/agentcore/     # the single root module
├── main.tf            # providers, partial s3 backend, core modules, tool IAM, MCP Gateway, runtime role, variables
├── network.tf         # VPC + 2 public subnets + IGW (workers use assignPublicIp=ENABLED, no NAT)
├── ecs.tf             # ecs + worker-task modules (worker task, API task, scoped task role)
├── lambdas.tf         # 5 tool Lambdas + invoke-runtime bridge + IAM
├── gateway-tools.tf   # 5 Gateway targets (portfolio/strategy/planning/documents/execution)
├── harness.tf         # AgentCore Harness — base prompt + harness_skill_files playbooks
├── step-functions.tf  # journey state machine, SFN role, log group
├── observability.tf   # CloudWatch Transaction Search (enable_transaction_search, default true)
├── public-hosting.tf  # CloudFront, ALB, ECS API service, site bucket (enable_public_hosting)
├── cognito.tf         # user pool, SPA client, groups, seed admin (enable_cognito)
└── lambda-src/        # tool Lambda + invoke-runtime sources and their tests

infra/modules/         # dynamodb · s3 · ecr · ecs · worker-task
infra/step-functions/  # migration-workflow.v2.asl.json
```

- **Provider.** AWS `>= 6.0`, needed for `aws_bedrockagentcore_*`.
- **State backend.** The `s3` block is partial. `setup.sh` supplies `bucket` + `dynamodb_table`
  via `-backend-config`. The key is `agentcore/terraform.tfstate`.
- **Harness quirk.** The Harness has `lifecycle { ignore_changes = [environment_variables] }` as a
  workaround for a provider bug.

### Variables

| Variable | Default | Notes |
|---|---|---|
| `project_prefix` | **required** (no default) | 2–12 chars, lowercase letters/digits/hyphens, starts with a letter, not `app`/`app-*`. `setup.sh` always passes `-var`; a manual apply must too |
| `aws_region` | `us-east-1` | |
| `bedrock_model_id` | `us.anthropic.claude-sonnet-4-6` | Must be an enabled inference profile |
| `dev_auth` | `"false"` | Hosted API `DEV_AUTH` |
| `enable_deploy` | `false` | Off: no deploy role, nothing is applied, Validation runs static checks (`deploy.tf`). `setup.sh` `ENABLE_DEPLOY=true` |
| `enable_public_hosting` / `api_desired_count` | `false` / `1` | `public-hosting.tf` |
| `enable_cognito` / `admin_email` / `extra_callback_urls` | `false` / `admin@example.com` / `[]` | `cognito.tf` |
| `enable_transaction_search` / `transaction_search_indexing_percentage` | `true` / `100` | `observability.tf` |

## Manual Terraform (optional)

```bash
cd infra/environments/agentcore
terraform init -reconfigure \
  -backend-config="bucket=${PREFIX}-tf-state-${ACCOUNT}-${REGION}" \
  -backend-config="dynamodb_table=${PREFIX}_tf_lock" \
  -backend-config="key=agentcore/terraform.tfstate"
terraform apply -var="project_prefix=${PREFIX}" -var="aws_region=${REGION}"
```

Always pass `-var project_prefix`; see the Variables table above.

## Worker / API image

The single image `{prefix}-worker` contains Python 3.12, AWS CLI v2, Node 20, the ATX CLI and the
Terraform CLI. It runs as the non-root user `atxuser`. The `tool-execution` Lambda launches it
for each worker mode:

| MODE            | Purpose                                                     | Callback |
|-----------------|-------------------------------------------------------------|----------|
| `code-analysis` | ATX comprehensive codebase analysis (`LaunchATX`)           | SendTaskSuccess/Failure |
| `workbench`     | ATX code transformation (`LaunchTransformation`)            | SendTaskSuccess/Failure |
| `infra`         | `terraform validate` + `plan` on generated IaC (`InfrastructureValidate`) | SendTaskSuccess/Failure |

The hosted API (`{prefix}-api-task`) runs the same image with a uvicorn entrypoint. Its env
includes `PROJECT_PREFIX`, `S3_BUCKET`, `AGENTCORE_HARNESS_ARN` and `AGENTCORE_GATEWAY_ID`/`_URL`.
After you change backend or worker code, rebuild and push the image by re-running `setup.sh`.

## IAM (summary)

| Role | Key permissions |
|------|------------------|
| SFN execution | `bedrock-agentcore:InvokeHarness` + `InvokeAgentRuntime`; `lambda:InvokeFunction` on invoke-runtime, tool-execution and tool-documents; logs |
| Harness runtime | Bedrock model invoke, AgentCore Memory (`ListEvents`, …), Gateway |
| Tool Lambda | DynamoDB (incl. `BatchGetItem`), S3 data bucket, `ecs:RunTask`, Secrets Manager (`{prefix}/*`), `states:SendTask*` |
| Worker task | DynamoDB, S3 data bucket, `states:SendTask*`, Bedrock, AWS Transform, Secrets |
| Gateway | Invoke the 5 tool Lambdas |

> MCP Gateway auth is `AWS_IAM` from the next apply (verify with `scripts/verify_gateway_auth.sh`).
> Still deferred: Secrets Manager hardening,
> dashboards and alarms, and a least-privilege IAM review.
