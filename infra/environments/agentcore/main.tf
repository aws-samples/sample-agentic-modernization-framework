# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
# AgentCore Environment — Minimal deployment (no VPC, no Neptune)
#
# Deploys:
#   - DynamoDB (portfolio table)
#   - S3 (data bucket)
#   - ECR (still needed for ATX assessment containers)
#   - Lambda MCP tool targets (app-context, wave-store)
#   - Step Functions workflow
#
# NOT deployed (graph DB disabled):
#   - Neptune
#   - VPC / subnets (not needed without Neptune)
#   - graph-query Lambda (requires VPC)

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
  }

  backend "s3" {
    key     = "agentcore/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region

  # Every taggable resource gets these; ECS tasks and the API service inherit them
  # (propagateTags). Add your own (owner, cost-center, ...) with var.extra_tags.
  default_tags {
    tags = merge(var.extra_tags, {
      project     = var.project_prefix
      environment = "agentcore"
      managed_by  = "terraform"
    })
  }
}

variable "extra_tags" {
  description = "Additional tags for every AMF resource (for example owner, cost-center). project, environment and managed_by are always set."
  type        = map(string)
  default     = {}
}

data "aws_caller_identity" "current" {}

# Partition for every ARN built here (aws, aws-us-gov, aws-cn) — never a literal "arn:aws".
data "aws_partition" "current" {}

locals {
  partition = data.aws_partition.current.partition
  # The one data bucket (modules/s3 creates it with this exact name). Use this
  # local everywhere instead of re-deriving the name.
  data_bucket = "${var.project_prefix}-data-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

# ── Core Resources ───────────────────────────────────────────────────

module "ecr" {
  source         = "../../modules/ecr"
  project_prefix = var.project_prefix
  kms_key_arn    = aws_kms_key.platform.arn
}

module "s3" {
  source         = "../../modules/s3"
  project_prefix = var.project_prefix
  aws_account_id = data.aws_caller_identity.current.account_id
  aws_region     = var.aws_region
}

module "dynamodb" {
  source         = "../../modules/dynamodb"
  project_prefix = var.project_prefix
  # Table is {prefix}_portfolio with the prefix's hyphens normalised to
  # underscores (e.g. prefix "my-env" -> my_env_portfolio) to match the backend.
  table_name  = "${replace(var.project_prefix, "-", "_")}_portfolio"
  kms_key_arn = aws_kms_key.platform.arn
}

# ── Lambda MCP Tools (no VPC needed — no Neptune) ────────────────────

resource "aws_iam_role" "tool_lambda" {
  name        = "${var.project_prefix}-agentcore-tool-role"
  description = "AMF MCP Gateway tool Lambdas (portfolio, strategy, planning, documents, execution)."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "tool_lambda_policy" {
  name = "${var.project_prefix}-agentcore-tool-policy"
  role = aws_iam_role.tool_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        # BatchGetItem: tool_portfolio.get_app_context resolves META.machine_ids → MACHINE# records.
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem"]
        Resource = [module.dynamodb.table_arn, "${module.dynamodb.table_arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = ["arn:${local.partition}:s3:::${local.data_bucket}", "arn:${local.partition}:s3:::${local.data_bucket}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:${local.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_prefix}-*:*"
      }
    ]
  })
}

# The Gateway tool Lambdas (portfolio, strategy, planning, documents, execution)
# are defined in lambdas.tf from lambda-src/. (The earlier app_context/wave_store
# Lambdas were superseded by tool_portfolio/tool_planning and have been removed.)

# ── AgentCore Gateway (MCP Protocol) ─────────────────────────────────

resource "aws_iam_role" "gateway_role" {
  name        = "${var.project_prefix}-agentcore-gateway-role"
  description = "AMF AgentCore MCP Gateway: invokes the AMF tool Lambdas."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Gateway → tool-Lambda invoke permissions live in lambdas.tf
# (gateway_invoke_all_tools + per-target aws_lambda_permission in gateway-tools.tf).

# Inbound auth: only the Harness calls the gateway, and it signs
# with its runtime role (SigV4, harness.tf outbound_auth). An unsigned caller gets 403.
variable "gateway_authorizer_type" {
  description = "MCP Gateway inbound auth. AWS_IAM (default): SigV4, Harness signs with its runtime role. NONE is a documented ROLLBACK value only: it lets anyone with the URL list and call every tool."
  type        = string
  default     = "AWS_IAM"
  validation {
    condition     = contains(["AWS_IAM", "NONE"], var.gateway_authorizer_type)
    error_message = "gateway_authorizer_type must be AWS_IAM (default) or NONE (rollback only)."
  }
}

resource "aws_bedrockagentcore_gateway" "main" {
  name          = "${var.project_prefix}-mcp-gateway"
  protocol_type = "MCP"
  role_arn      = aws_iam_role.gateway_role.arn

  authorizer_type = var.gateway_authorizer_type
}

# Gateway targets, Lambdas, and Harness are in separate .tf files:
# - gateway-tools.tf (targets + permissions)
# - lambdas.tf (Lambda functions)
# - harness.tf (AgentCore Harness)

# ── Outputs (from main.tf resources) ─────────────────────────────────

output "dynamodb_table_name" {
  description = "DynamoDB table name"
  value       = module.dynamodb.table_name
}
output "s3_bucket" {
  description = "S3 bucket"
  value       = local.data_bucket
}
output "ecr_repository_url" {
  description = "ECR repository URL"
  value       = module.ecr.assessment_worker_repo_url
}
output "agentcore_gateway_id" {
  description = "AgentCore gateway ID"
  value       = aws_bedrockagentcore_gateway.main.gateway_id
}
output "agentcore_gateway_endpoint" {
  description = "AgentCore gateway endpoint"
  value       = "https://${aws_bedrockagentcore_gateway.main.gateway_id}.gateway.bedrock-agentcore.${var.aws_region}.amazonaws.com/mcp"
}

# ── Variables ────────────────────────────────────────────────────────

variable "project_prefix" {
  description = "Prefix for every resource name (e.g. uat). Required: scripts/setup.sh always passes -var project_prefix. No default on purpose: a default lets a bare `terraform apply` silently plan against the wrong environment."
  type        = string
  validation {
    # Lowercase, starts with a letter, 2-12 chars. 12 keeps the app permissions boundary (deploy.tf), which
    # repeats the prefix, under IAM's 6,144-character managed-policy limit even for a hyphenated prefix.
    condition     = can(regex("^[a-z][a-z0-9-]{1,11}$", var.project_prefix)) && !endswith(var.project_prefix, "-")
    error_message = "project_prefix must be 2-12 characters: lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
  validation {
    # Generated app resources are named app-* / APP-*: an "app" prefix would make the app
    # permissions boundary's control-plane Deny (deploy.tf) match every app resource.
    condition     = !can(regex("^app(-|$)", var.project_prefix))
    error_message = "project_prefix must not be \"app\" or start with \"app-\": that collides with generated app resource names."
  }
}

variable "aws_region" {
  description = "AWS region for the whole environment."
  type        = string
  default     = "us-east-1"
}

variable "bedrock_model_id" {
  description = "Bedrock inference profile used by the Harness and the worker/API tasks. Must be enabled in the account/region."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "dev_auth" {
  type        = string
  description = "When \"true\", the API auto-authenticates all requests as a dev user (no Cognito). Demo-only; set via terraform.tfvars for the demo env, keep \"false\" otherwise."
  default     = "false"
}

variable "log_retention_days" {
  description = "Baseline CloudWatch log retention (days). Used by the Step Functions log groups and by any per-use override set to null."
  type        = number
  default     = 30
}

variable "sfn_log_retention_days" {
  description = "Retention (days) for the two Step Functions log groups. null = log_retention_days."
  type        = number
  default     = null
  nullable    = true
}

variable "ecs_log_retention_days" {
  description = "Retention (days) for the ECS cluster log group (/ecs/{prefix}). null = log_retention_days."
  type        = number
  default     = 7
  nullable    = true
}

variable "site_noncurrent_version_days" {
  description = "Days the portal bucket keeps a replaced UI build (noncurrent object versions) before expiring it."
  type        = number
  default     = 30
}

variable "s3_abort_incomplete_upload_days" {
  description = "Days before an unfinished multipart upload to the portal bucket is aborted."
  type        = number
  default     = 7
}

variable "ecs_container_insights" {
  description = "Container Insights on the ECS cluster (enabled | enhanced | disabled)."
  type        = string
  default     = "enabled"
}

variable "worker_log_retention_days" {
  description = "Retention (days) for the worker/API task log group (/ecs/{prefix}-migration-worker). null = log_retention_days."
  type        = number
  default     = 365
  nullable    = true
}

locals {
  sfn_log_retention_days    = coalesce(var.sfn_log_retention_days, var.log_retention_days)
  ecs_log_retention_days    = coalesce(var.ecs_log_retention_days, var.log_retention_days)
  worker_log_retention_days = coalesce(var.worker_log_retention_days, var.log_retention_days)
}

# ECS cluster/task-def/subnets are created in this environment (ecs.tf, network.tf)
# and wired into the execution tool Lambda from module/VPC outputs — no literals.

# ── IAM: Agent/Harness Execution Role ────────────────────────────────

resource "aws_iam_role" "agent_runtime_role" {
  name        = "${var.project_prefix}-agentcore-runtime-role"
  description = "AMF AgentCore Harness runtime: calls this deployment's MCP Gateway and the Bedrock model."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "agent_runtime_permissions" {
  name = "${var.project_prefix}-agentcore-runtime-perms"
  role = aws_iam_role.agent_runtime_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
        Resource = [module.dynamodb.table_arn, "${module.dynamodb.table_arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = ["arn:${local.partition}:s3:::${var.project_prefix}-data-*", "arn:${local.partition}:s3:::${var.project_prefix}-data-*/*", "arn:${local.partition}:s3:::${var.project_prefix}-skills-*", "arn:${local.partition}:s3:::${var.project_prefix}-skills-*/*"]
      },
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        # Inference profiles (e.g. us.anthropic.claude-sonnet-4-6) route cross-region,
        # so the caller needs the profile ARN + foundation-model in every routed region.
        Resource = [
          "arn:${local.partition}:bedrock:*::foundation-model/*",
          "arn:${local.partition}:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        ]
      },
      {
        # SigV4 calls to the MCP Gateway. Explicit so it survives the
        # Phase C scoping of the broad statement below.
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeGateway"]
        Resource = [local.gateway_arn]
      },
      {
        # Workload identity tokens for the Harness runtime (AgentCore Runtime execution-role
        # actions). Long-term memory is disabled (harness.tf memory { disabled {} }), so no
        # Memory actions; tool calls go through InvokeGateway above. CloudTrail (30 days) showed
        # no other AgentCore management calls by this role.
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        ]
        Resource = [
          "arn:${local.partition}:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
          "arn:${local.partition}:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:${local.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_prefix}-*:*"]
      }
    ]
  })
}
