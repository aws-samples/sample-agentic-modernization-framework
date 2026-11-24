# kics-scan disable=0afbcfe9-d341-4b92-a64c-7e6de0543879,4849211b-ac39-479e-ae78-5694d506cb24,e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   0afbcfe9: AWS-managed log encryption; customer-managed KMS deferred / adopting team's choice
#   4849211b: SG is attached by Lambda-launched ECS tasks (RunTask), not in Terraform
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
# Assessment Worker ECS Fargate task definition
# Runs the full assessment pipeline: tools + ATX + Bedrock synthesis + DynamoDB update
# One task per application assessment — scales horizontally

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for encryption at rest."
  type        = string
}

variable "project_prefix" {
  description = "Project prefix"
  type        = string
}
variable "aws_region" {
  description = "Aws region"
  type        = string
}
variable "aws_account_id" {
  description = "Aws account ID"
  type        = string
}
variable "execution_role_arn" {
  description = "Execution role ARN"
  type        = string
}
variable "worker_image" {
  description = "Worker image"
  type        = string
}
variable "vpc_id" {
  description = "VPC ID"
  type        = string
}
variable "dynamodb_table_arn" {
  description = "DynamoDB table ARN"
  type        = string
}
variable "dynamodb_table_name" {
  description = "DynamoDB table name"
  type        = string
}
variable "atx_s3_bucket" {
  description = "ATX S3 bucket"
  type        = string
}
variable "bedrock_model_id" {
  type        = string
  description = "Bedrock inference profile (root var.bedrock_model_id is the single source)."
}

variable "cognito_user_pool_id" {
  description = "Cognito user pool ID"
  type        = string
  default     = ""
}

variable "cognito_app_client_id" {
  description = "Cognito app client ID"
  type        = string
  default     = ""
}

variable "agentcore_harness_arn" {
  description = "AgentCore harness ARN"
  type        = string
  default     = ""
}

variable "agentcore_gateway_id" {
  description = "AgentCore gateway ID"
  type        = string
  default     = ""
}

variable "agentcore_gateway_url" {
  description = "AgentCore gateway URL"
  type        = string
  default     = ""
}

variable "api_secrets" {
  description = "Secrets Manager values injected into the API task only: env name -> secret ARN."
  type        = map(string)
  default     = {}
}

variable "api_extra_environment" {
  description = "Extra env vars for the API task only (e.g. GRAPH_BACKEND / NEPTUNE_ENDPOINT)."
  type        = map(string)
  default     = {}
}

variable "dev_auth" {
  type        = string
  description = "When \"true\", the API auto-authenticates all requests as a dev user (no Cognito). Demo-only; keep \"false\" for real environments."
  default     = "false"
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR allowed to reach the worker/API task on port 8000 (ALB). Pass the VPC's actual cidr_block."
}

variable "worker_container_name" {
  type        = string
  description = "Container name in the worker and API task definitions (the execution Lambda / API override this container by name)."
  default     = "migration-worker"
}

variable "worker_cpu" {
  type        = string
  description = "Worker task CPU units (ATX + Bedrock calls)."
  default     = "4096"
}

variable "worker_memory" {
  type        = string
  description = "Worker task memory (MiB)."
  default     = "8192"
}

variable "api_cpu" {
  type        = string
  description = "Hosted API task CPU units."
  default     = "1024"
}

variable "api_memory" {
  type        = string
  description = "Hosted API task memory (MiB)."
  default     = "2048"
}

variable "log_retention_days" {
  type        = number
  description = "Retention (days) of the worker/API task log group."
  default     = 365
}

data "aws_partition" "current" {}

locals {
  partition = data.aws_partition.current.partition
}

# Security group for assessment worker tasks
resource "aws_security_group" "worker_task" {
  #checkov:skip=CKV2_AWS_5:SG is attached by Lambda-launched ECS tasks (RunTask networkConfiguration), not in Terraform
  name_prefix = "${var.project_prefix}-worker-task-"
  vpc_id      = var.vpc_id
  description = "Security group for worker task ECS containers"

  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
    description = "ALB health checks and API traffic"
  }

  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS for AWS services (Bedrock, S3, DynamoDB, CodeCommit)"
  }

  tags = { project = var.project_prefix, managed_by = "terraform" }
}

# CloudWatch log group
resource "aws_cloudwatch_log_group" "assessment" {
  name              = "/ecs/${var.project_prefix}-migration-worker"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = { project = var.project_prefix }
}

# Task role — needs DynamoDB, S3, Bedrock, Transform access
resource "aws_iam_role" "worker_task" {
  name = "${var.project_prefix}-worker-task-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
  tags = { project = var.project_prefix }
}

# The MODE=infra worker runs `terraform plan` against this (UAT) account. Plan
# issues read-only Describe/List/Get calls across many services; attach the AWS
# managed ReadOnlyAccess so plan can refresh data sources. Nothing is applied
# (no write perms are granted for terraform apply). Tighten before production.
resource "aws_iam_role_policy_attachment" "worker_task_readonly" {
  role       = aws_iam_role.worker_task.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "worker_task_policy" {
  name = "${var.project_prefix}-worker-task-policy"
  role = aws_iam_role.worker_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:DeleteItem",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem",
        ]
        Resource = [
          var.dynamodb_table_arn,
          "${var.dynamodb_table_arn}/index/*"
        ]
      },
      {
        Sid    = "S3Access"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:DeleteObject",
        ]
        Resource = [
          "arn:${local.partition}:s3:::${var.atx_s3_bucket}",
          "arn:${local.partition}:s3:::${var.atx_s3_bucket}/*"
        ]
      },
      {
        Sid    = "BedrockAccess"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream",
        ]
        Resource = [
          # Cross-region inference profiles (us.*) route to any US region's
          # foundation model — APP-TE 2026-10-13 was denied on us-east-2.
          "arn:${local.partition}:bedrock:*::foundation-model/*",
          "arn:${local.partition}:bedrock:${var.aws_region}:${var.aws_account_id}:inference-profile/*"
        ]
      },
      {
        # AWS Transform custom (the ATX CLI: `atx custom def list` / `def exec`). transform:* (core
        # AWS Transform) was removed: CloudTrail shows no calls to it. transform-custom keeps "*":
        # the CLI's agent loop calls actions with no resource type (ConverseStream,
        # ListTransformationPackageMetadata, SendTelemetryEvent, GetFindingGroups), and AWS-managed
        # transformation definitions (AWS/...) are packages owned by AWS, not by this account, so
        # an account-scoped ARN would block them. Sharing, resource-policy and delete actions are
        # denied below.
        Sid      = "TransformCustomCli"
        Effect   = "Allow"
        Action   = ["transform-custom:*"]
        Resource = "*"
      },
      {
        Sid    = "TransformCustomNoShareOrDelete"
        Effect = "Deny"
        Action = [
          "transform-custom:PutResourcePolicy", "transform-custom:DeleteResourcePolicy",
          "transform-custom:ShareTransformationPackage", "transform-custom:UnshareTransformationPackage",
          "transform-custom:Delete*",
        ]
        Resource = "*"
      },
      {
        # ATX clones/pushes migration branches to CodeCommit-backed app repos
        # (git-over-HTTPS via the AWS credential helper). Read-only actions plus
        # GitPull/GitPush; scoped to repos in this account/region.
        Sid    = "CodeCommitGitAccess"
        Effect = "Allow"
        Action = [
          "codecommit:GitPull",
          "codecommit:GitPush",
          "codecommit:GetRepository",
          "codecommit:GetBranch",
          "codecommit:CreateBranch",
          "codecommit:ListBranches",
          # Deploy reads infra/ at the human-approved commit.
          "codecommit:GetFolder",
          "codecommit:GetFile",
          "codecommit:GetCommit",
        ]
        Resource = "arn:${local.partition}:codecommit:${var.aws_region}:${var.aws_account_id}:*"
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.assessment.arn}:*"
      },
      {
        Sid    = "ECSLaunchWorkerTasks"
        Effect = "Allow"
        Action = ["ecs:RunTask", "ecs:DescribeTasks", "ecs:StopTask", "ecs:ListTasks", "ecs:TagResource"]
        Resource = [
          "arn:${local.partition}:ecs:${var.aws_region}:${var.aws_account_id}:task-definition/${var.project_prefix}-*",
          "arn:${local.partition}:ecs:${var.aws_region}:${var.aws_account_id}:task/*"
        ]
      },
      {
        # Launching worker tasks passes exactly the task definition's two roles.
        Sid      = "PassRoleToECSTasks"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = ["arn:${local.partition}:iam::${var.aws_account_id}:role/${var.project_prefix}-worker-task-role", var.execution_role_arn]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        # The worker assumes the deploy role (deploy.tf); it must never hand it to a task or function.
        Sid      = "DenyPassDeployRole"
        Effect   = "Deny"
        Action   = ["iam:PassRole"]
        Resource = "arn:${local.partition}:iam::${var.aws_account_id}:role/${var.project_prefix}-deploy-role"
      },
      {
        Sid      = "SecretsManagerRead"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:${local.partition}:secretsmanager:${var.aws_region}:*:secret:${var.project_prefix}/*"
      },
      {
        Sid    = "StepFunctionsControlPlane"
        Effect = "Allow"
        Action = [
          "states:StartExecution",
          "states:StopExecution",
          "states:DescribeExecution",
          "states:GetExecutionHistory",
          "states:ListExecutions",
        ]
        Resource = [
          "arn:${local.partition}:states:${var.aws_region}:${var.aws_account_id}:stateMachine:${var.project_prefix}-*",
          "arn:${local.partition}:states:${var.aws_region}:${var.aws_account_id}:execution:${var.project_prefix}-*:*"
        ]
      },
      {
        # The hosted API (same role) reaches the Harness through the invoke-runtime
        # bridge for chat. Without this /chat/message reports "AgentCore runtime
        # not deployed" (AccessDenied) even though it is.
        Sid    = "InvokeAgentCoreBridge"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:${local.partition}:lambda:${var.aws_region}:${var.aws_account_id}:function:${var.project_prefix}-invoke-runtime",
          # Explicit "Tear down" (POST /workflow/teardown → destroy_infrastructure).
          "arn:${local.partition}:lambda:${var.aws_region}:${var.aws_account_id}:function:${var.project_prefix}-tool-execution",
        ]
      },
      {
        # Task-token callbacks target an opaque token, NOT a resource ARN, so
        # IAM requires "*" here. Scoping this to a stateMachine ARN would break
        # the waitForTaskToken callback (regressing the fix in commit d2e7fcc).
        Sid    = "StepFunctionsTaskTokenCallbacks"
        Effect = "Allow"
        Action = [
          "states:SendTaskSuccess",
          "states:SendTaskFailure",
          "states:SendTaskHeartbeat",
        ]
        Resource = "*"
      },
    ]
  })
}

# Fargate task definition
resource "aws_ecs_task_definition" "assessment_worker" {
  family                   = "${var.project_prefix}-worker-task"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([{
    name      = var.worker_container_name
    image     = var.worker_image
    essential = true

    environment = concat([
      { name = "AWS_REGION", value = var.aws_region },
      { name = "PROJECT_PREFIX", value = var.project_prefix },
      { name = "DYNAMODB_TABLE", value = var.dynamodb_table_name },
      { name = "BEDROCK_MODEL_ID", value = var.bedrock_model_id },
      # S3_BUCKET is the env the backend config actually reads; set it explicitly
      # so get_s3_bucket() never has to derive via STS at startup (a startup-time
      # STS race previously produced an account-less bucket and 500'd /design).
      { name = "S3_BUCKET", value = var.atx_s3_bucket },
    ])


    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.assessment.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "assessment"
      }
    }
  }])

  tags = { project = var.project_prefix, managed_by = "terraform" }
}

output "task_definition_arn" {
  description = "Task definition ARN"
  value       = aws_ecs_task_definition.assessment_worker.arn
}
output "task_family" {
  description = "Task family"
  value       = aws_ecs_task_definition.assessment_worker.family
}
output "assessment_sg_id" {
  description = "Assessment security group ID"
  value       = aws_security_group.worker_task.id
}
output "worker_task_role_arn" {
  description = "Worker task role ARN"
  value       = aws_iam_role.worker_task.arn
}

# ── API Task Definition (same image, different entrypoint) ─────────────

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project_prefix}-api-task"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([{
    name      = var.worker_container_name
    image     = var.worker_image
    essential = true

    entryPoint = ["/home/atxuser/app/.venv/bin/python", "-m", "uvicorn", "backend.src.main:app", "--host", "0.0.0.0", "--port", "8000"]

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = concat([
      { name = "AWS_REGION", value = var.aws_region },
      # Prefix drives every derived resource name (state machine, cluster, etc.);
      # without it the API falls back to the config default and looks up the wrong env.
      { name = "PROJECT_PREFIX", value = var.project_prefix },
      { name = "SFN_STATE_MACHINE_NAME", value = "${var.project_prefix}-agentcore-workflow" },
      { name = "DYNAMODB_TABLE", value = var.dynamodb_table_name },
      { name = "BEDROCK_MODEL_ID", value = var.bedrock_model_id },
      # S3_BUCKET is the env the backend config actually reads; set it explicitly
      # so get_s3_bucket() never has to derive via STS at startup (a startup-time
      # STS race previously produced an account-less bucket and 500'd /design).
      { name = "S3_BUCKET", value = var.atx_s3_bucket },
      { name = "DEV_AUTH", value = var.dev_auth },
      { name = "COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
      { name = "COGNITO_APP_CLIENT_ID", value = var.cognito_app_client_id },
      { name = "MODE", value = "api" },
      { name = "ATX_ECS_CLUSTER", value = "${var.project_prefix}-cluster" },
      { name = "ASSESSMENT_ECS_TASK_DEFINITION", value = "${var.project_prefix}-worker-task" },
      { name = "AGENTCORE_HARNESS_ARN", value = var.agentcore_harness_arn },
      { name = "AGENTCORE_GATEWAY_ID", value = var.agentcore_gateway_id },
      { name = "AGENTCORE_GATEWAY_URL", value = var.agentcore_gateway_url },
      { name = "CORS_ORIGINS", value = "http://localhost:3000,http://${var.project_prefix}-frontend-${var.aws_account_id}-${var.aws_region}.s3-website-${var.aws_region}.amazonaws.com" },
    ], [for k, v in var.api_extra_environment : { name = k, value = v }])
    secrets = [for k, v in var.api_secrets : { name = k, valueFrom = v }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.assessment.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])

  tags = { project = var.project_prefix, managed_by = "terraform" }
}

output "api_task_definition_arn" {
  description = "API task definition ARN"
  value       = aws_ecs_task_definition.api.arn
}
