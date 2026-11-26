# kics-scan disable=720f44cf-285e-4b69-8f72-835e6bc1dceb,8152e0cf-d2f0-47ad-96d5-d003a76eabd1,e39f87f5-0abf-488b-864c-63ee1f588140,e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   720f44cf: invoked synchronously; errors return to the caller
#   8152e0cf: X-Ray deferred / adopting team's choice
#   e39f87f5: no consumer for S3 event notifications
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
# ══════════════════════════════════════════════════════════════════════
# Lambda Functions — Gateway Tool Implementations
#
# Each Lambda handles a group of related tools.
# All share the same IAM role (tool_lambda) with DynamoDB + S3 access.
# ══════════════════════════════════════════════════════════════════════

# ── Tunables ────────────────────────────────────────
# Every tool-Lambda tunable is an env var set here from a variable whose default
# is the value the code used before (behaviour is unchanged unless overridden).
# Grouped per Lambda as objects with optional() defaults, so overriding one key
# keeps the others, e.g. -var 'tool_strategy_tunables={STRATEGY_CONFIDENCE_FLOOR="45"}'.

variable "app_tfstate_bucket_pattern" {
  description = "Name of a migrated app's Terraform state bucket in its TARGET account (str.format: {prefix}, {account}, {region}). Keep in sync with backend.tf.tpl and the worker's deploy_runner."
  type        = string
  default     = "{prefix}-tfstate-{account}-{region}"
}

variable "app_tfstate_lock_pattern" {
  description = "Name of a migrated app's Terraform state lock table in its TARGET account (str.format: {prefix}, {account}, {region})."
  type        = string
  default     = "{prefix}-tfstate-lock"
}

variable "tool_execution_tunables" {
  description = <<-EOT
    tool_execution env tunables (strings). Keys: TEMPLATES_PREFIX (S3 key prefix of the template library),
    ECS_ASSIGN_PUBLIC_IP (ENABLED: workers run in public subnets without NAT),
    ATX_CODE_ANALYSIS_TD_NAME, ATX_SHELL_TIMEOUT (s), GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL (commits to the
    migration branch), DOCKERHUB_MIRROR (buildspec base-image mirror for Docker Official Images; "" disables), RUNTASK_MAX_ATTEMPTS / RUNTASK_BACKOFF_BASE_SECONDS
    (ECS RunTask retry, base x 2^n), JAVA/PYTHON/DOTNET_UPGRADE_TARGET_DEFAULT (derived transform targets),
    DEFAULT_JAVA/PYTHON/NODE_VERSION (runtime fallbacks), COVERAGE_THRESHOLD_DEFAULT (%),
    MULTI_AZ_SLA_THRESHOLD (uptime_sla % that builds a Multi-AZ DB), GENERATED_LAMBDA_MEMORY_DEFAULT (MB) /
    GENERATED_LAMBDA_TIMEOUT_DEFAULT (s), DEFAULT_CONTAINER_PORT, DEFAULT_HEALTH_CHECK_PATH (also the
    validation probe's), IAC_MANAGED_BY_TAG (ManagedBy tag; matches the module templates),
    ECS_ROLLOUT_WAIT_SECONDS / ECS_ROLLOUT_POLL_SECONDS (validation waits for an in-progress ECS rollout; must
    leave room for the probe inside the 900 s tool timeout).
  EOT
  type = object({
    TEMPLATES_PREFIX                 = optional(string, "config/templates")
    ECS_ASSIGN_PUBLIC_IP             = optional(string, "ENABLED")
    ATX_CODE_ANALYSIS_TD_NAME        = optional(string, "AWS/comprehensive-codebase-analysis")
    ATX_SHELL_TIMEOUT                = optional(string, "36000")
    GIT_AUTHOR_NAME                  = optional(string, "AMF Execution Agent")
    GIT_AUTHOR_EMAIL                 = optional(string, "amf-agent@migration.local")
    DOCKERHUB_MIRROR                 = optional(string, "public.ecr.aws/docker/library")
    RUNTASK_MAX_ATTEMPTS             = optional(string, "4")
    RUNTASK_BACKOFF_BASE_SECONDS     = optional(string, "3")
    JAVA_UPGRADE_TARGET_DEFAULT      = optional(string, "21")
    PYTHON_UPGRADE_TARGET_DEFAULT    = optional(string, "3.12")
    DOTNET_UPGRADE_TARGET_DEFAULT    = optional(string, "8")
    DEFAULT_JAVA_VERSION             = optional(string, "17")
    DEFAULT_PYTHON_VERSION           = optional(string, "3.12")
    DEFAULT_NODE_VERSION             = optional(string, "20")
    COVERAGE_THRESHOLD_DEFAULT       = optional(string, "70")
    MULTI_AZ_SLA_THRESHOLD           = optional(string, "99.95")
    GENERATED_LAMBDA_MEMORY_DEFAULT  = optional(string, "512")
    GENERATED_LAMBDA_TIMEOUT_DEFAULT = optional(string, "30")
    DEFAULT_CONTAINER_PORT           = optional(string, "8080")
    DEFAULT_HEALTH_CHECK_PATH        = optional(string, "/health")
    IAC_MANAGED_BY_TAG               = optional(string, "amf-terraform")
    ECS_ROLLOUT_WAIT_SECONDS         = optional(string, "240")
    ECS_ROLLOUT_POLL_SECONDS         = optional(string, "15")
  })
  default = {}
}

variable "validation_probe_tunables" {
  description = <<-EOT
    In-VPC validation probe (validation_probe.py, runs inside tool_execution). Keys:
    VALIDATION_PROBE_ACTIVE_ATTEMPTS x VALIDATION_PROBE_ACTIVE_DELAY_SECONDS (wait for the probe's ENI; must fit
    the 900 s tool timeout), VALIDATION_PROBE_CREATE_ATTEMPTS / VALIDATION_PROBE_CREATE_RETRY_SECONDS (role not yet
    assumable), VALIDATION_PROBE_RUNTIME / _TIMEOUT_SECONDS / _MEMORY_MB (the probe function),
    VALIDATION_PROBE_INVOKE_READ_TIMEOUT_SECONDS (> the probe timeout) / _INVOKE_MAX_ATTEMPTS,
    PROBE_REQUEST_TIMEOUT_SECONDS (per L1/L2 HTTP request).
  EOT
  type = object({
    VALIDATION_PROBE_ACTIVE_ATTEMPTS             = optional(string, "96")
    VALIDATION_PROBE_ACTIVE_DELAY_SECONDS        = optional(string, "5")
    VALIDATION_PROBE_CREATE_ATTEMPTS             = optional(string, "5")
    VALIDATION_PROBE_CREATE_RETRY_SECONDS        = optional(string, "5")
    VALIDATION_PROBE_RUNTIME                     = optional(string, "python3.12")
    VALIDATION_PROBE_TIMEOUT_SECONDS             = optional(string, "120")
    VALIDATION_PROBE_MEMORY_MB                   = optional(string, "256")
    VALIDATION_PROBE_INVOKE_READ_TIMEOUT_SECONDS = optional(string, "150")
    VALIDATION_PROBE_INVOKE_MAX_ATTEMPTS         = optional(string, "1")
    PROBE_REQUEST_TIMEOUT_SECONDS                = optional(string, "10")
  })
  default = {}
}

variable "tool_strategy_tunables" {
  description = <<-EOT
    7R strategy engine (tool_strategy). Keys: STRATEGY_DEFAULT_CONFIDENCE (no-evidence default),
    STRATEGY_CONFIDENCE_FLOOR, STRATEGY_LICENSE_COST_THRESHOLD (USD/yr replatform signal),
    STRATEGY_REPLATFORM_BASE_CONFIDENCE + _PER_SIGNAL x signals capped at _MAX_CONFIDENCE,
    STRATEGY_COMPLETENESS_HIGH/_LOW (%) with _BONUS/_PENALTY and _MAX_CONFIDENCE.
  EOT
  type = object({
    STRATEGY_DEFAULT_CONFIDENCE          = optional(string, "60")
    STRATEGY_CONFIDENCE_FLOOR            = optional(string, "40")
    STRATEGY_LICENSE_COST_THRESHOLD      = optional(string, "30000")
    STRATEGY_REPLATFORM_BASE_CONFIDENCE  = optional(string, "60")
    STRATEGY_REPLATFORM_PER_SIGNAL       = optional(string, "8")
    STRATEGY_REPLATFORM_MAX_CONFIDENCE   = optional(string, "85")
    STRATEGY_COMPLETENESS_HIGH           = optional(string, "80")
    STRATEGY_COMPLETENESS_LOW            = optional(string, "40")
    STRATEGY_COMPLETENESS_BONUS          = optional(string, "10")
    STRATEGY_COMPLETENESS_PENALTY        = optional(string, "15")
    STRATEGY_COMPLETENESS_MAX_CONFIDENCE = optional(string, "95")
  })
  default = {}
}

variable "tool_planning_tunables" {
  description = "Wave planner (tool_planning, core/wave_planner per planning.md): WAVE_PILOT_SIZE, WAVE_BAND_LIMITS (max apps in Pilot,Foundation,Core,Customer-Facing), WAVE_SPILL_LIMIT, WAVE_TIER1_MAX_SHARE, WAVE_MAX_CYCLE_COWAVE (same defaults as backend Settings), WAVE_CONN_QUERY_CONCURRENCY (parallel per-machine CONN# reads)."
  type = object({
    WAVE_PILOT_SIZE             = optional(string, "3")
    WAVE_BAND_LIMITS            = optional(string, "3,8,12,10")
    WAVE_SPILL_LIMIT            = optional(string, "10")
    WAVE_TIER1_MAX_SHARE        = optional(string, "0.30")
    WAVE_MAX_CYCLE_COWAVE       = optional(string, "3")
    WAVE_CONN_QUERY_CONCURRENCY = optional(string, "8")
  })
  default = {}
}

variable "tool_portfolio_tunables" {
  description = "Portfolio read limits (tool_portfolio): APP_CONTEXT_CONN_READ_LIMIT / APP_CONTEXT_MAX_CONNECTIONS (get_app_context CONN# read / returned), SEARCH_MACHINES_MAX_SCANNED (machines read, paginated) / SEARCH_MACHINES_MAX_RESULTS."
  type = object({
    APP_CONTEXT_CONN_READ_LIMIT = optional(string, "50")
    APP_CONTEXT_MAX_CONNECTIONS = optional(string, "20")
    SEARCH_MACHINES_MAX_SCANNED = optional(string, "200")
    SEARCH_MACHINES_MAX_RESULTS = optional(string, "20")
  })
  default = {}
}

variable "tool_documents_tunables" {
  description = "Documents limits (tool_documents): DESIGN_CONTENT_MAX_CHARS — design-document characters returned inline (the rest stays in S3)."
  type = object({
    DESIGN_CONTENT_MAX_CHARS = optional(string, "8000")
  })
  default = {}
}

variable "invoke_runtime_tunables" {
  description = "SFN bridge (invoke_runtime): HARNESS_READ_TIMEOUT_SECONDS (boto3 read timeout; < the 900 s Lambda timeout), HARNESS_RESPONSE_MAX_CHARS (response returned to Step Functions; 256 KB state limit), APP_FACTS_MAX_CHARS (facts embedded in phase prompts)."
  type = object({
    HARNESS_READ_TIMEOUT_SECONDS = optional(string, "600")
    HARNESS_RESPONSE_MAX_CHARS   = optional(string, "10000")
    APP_FACTS_MAX_CHARS          = optional(string, "6000")
  })
  default = {}
}

locals {
  # AWS_REGION is set by the Lambda runtime (reserved); the code reads it directly.
  lambda_env = {
    DYNAMODB_TABLE             = module.dynamodb.table_name
    S3_BUCKET                  = local.data_bucket
    PROJECT_PREFIX             = var.project_prefix
    APP_TFSTATE_BUCKET_PATTERN = var.app_tfstate_bucket_pattern
    APP_TFSTATE_LOCK_PATTERN   = var.app_tfstate_lock_pattern
  }
}

# ── Portfolio Tools Lambda ────────────────────────────────────────────

data "archive_file" "tool_portfolio_zip" {
  type        = "zip"
  output_path = "${path.module}/.build/tool_portfolio.zip"
  source {
    content  = file("${path.module}/lambda-src/tool_portfolio.py")
    filename = "tool_portfolio.py"
  }
  source {
    content  = file("${path.module}/lambda-src/agent_tools.py")
    filename = "agent_tools.py"
  }
}

resource "aws_lambda_function" "tool_portfolio" {
  #checkov:skip=CKV_AWS_115:low-volume tool Lambdas share the account pool; reserved concurrency deferred
  #checkov:skip=CKV_AWS_116:invoked synchronously (Gateway / Step Functions); errors return to the caller
  #checkov:skip=CKV_AWS_117:tool Lambdas call public AWS endpoints; VPC + endpoints deferred (no NAT)
  #checkov:skip=CKV_AWS_272:code is built and deployed by Terraform from this repo; no external publishers
  function_name    = "${var.project_prefix}-tool-portfolio"
  runtime          = "python3.12"
  handler          = "tool_portfolio.handler"
  role             = aws_iam_role.tool_lambda.arn
  filename         = data.archive_file.tool_portfolio_zip.output_path
  source_code_hash = data.archive_file.tool_portfolio_zip.output_base64sha256
  timeout          = 30
  memory_size      = 512
  kms_key_arn      = aws_kms_key.platform.arn
  tracing_config {
    mode = "Active"
  }
  environment { variables = merge(local.lambda_env, var.tool_portfolio_tunables) }
}

# ── Strategy Tools Lambda ─────────────────────────────────────────────

data "archive_file" "tool_strategy_zip" {
  type        = "zip"
  output_path = "${path.module}/.build/tool_strategy.zip"
  source {
    content  = file("${path.module}/lambda-src/tool_strategy.py")
    filename = "tool_strategy.py"
  }
  source {
    content  = file("${path.module}/lambda-src/agent_tools.py")
    filename = "agent_tools.py"
  }
}

resource "aws_lambda_function" "tool_strategy" {
  #checkov:skip=CKV_AWS_115:low-volume tool Lambdas share the account pool; reserved concurrency deferred
  #checkov:skip=CKV_AWS_116:invoked synchronously (Gateway / Step Functions); errors return to the caller
  #checkov:skip=CKV_AWS_117:tool Lambdas call public AWS endpoints; VPC + endpoints deferred (no NAT)
  #checkov:skip=CKV_AWS_272:code is built and deployed by Terraform from this repo; no external publishers
  function_name    = "${var.project_prefix}-tool-strategy"
  runtime          = "python3.12"
  handler          = "tool_strategy.handler"
  role             = aws_iam_role.tool_lambda.arn
  filename         = data.archive_file.tool_strategy_zip.output_path
  source_code_hash = data.archive_file.tool_strategy_zip.output_base64sha256
  timeout          = 60
  memory_size      = 512
  kms_key_arn      = aws_kms_key.platform.arn
  tracing_config {
    mode = "Active"
  }
  environment { variables = merge(local.lambda_env, var.tool_strategy_tunables) }
}

# ── Planning Tools Lambda ─────────────────────────────────────────────

data "archive_file" "tool_planning_zip" {
  type        = "zip"
  output_path = "${path.module}/.build/tool_planning.zip"
  source {
    content  = file("${path.module}/lambda-src/tool_planning.py")
    filename = "tool_planning.py"
  }
  # The same planner the API runs (pure module).
  source {
    content  = file("${path.module}/../../../backend/src/core/wave_planner.py")
    filename = "wave_planner.py"
  }
  source {
    content  = file("${path.module}/lambda-src/agent_tools.py")
    filename = "agent_tools.py"
  }
}

resource "aws_lambda_function" "tool_planning" {
  #checkov:skip=CKV_AWS_115:low-volume tool Lambdas share the account pool; reserved concurrency deferred
  #checkov:skip=CKV_AWS_116:invoked synchronously (Gateway / Step Functions); errors return to the caller
  #checkov:skip=CKV_AWS_117:tool Lambdas call public AWS endpoints; VPC + endpoints deferred (no NAT)
  #checkov:skip=CKV_AWS_272:code is built and deployed by Terraform from this repo; no external publishers
  function_name    = "${var.project_prefix}-tool-planning"
  runtime          = "python3.12"
  handler          = "tool_planning.handler"
  role             = aws_iam_role.tool_lambda.arn
  filename         = data.archive_file.tool_planning_zip.output_path
  source_code_hash = data.archive_file.tool_planning_zip.output_base64sha256
  timeout          = 60
  memory_size      = 512
  kms_key_arn      = aws_kms_key.platform.arn
  tracing_config {
    mode = "Active"
  }
  environment { variables = merge(local.lambda_env, var.tool_planning_tunables) }
}

# ── Documents Tools Lambda ────────────────────────────────────────────

data "archive_file" "tool_documents_zip" {
  type        = "zip"
  output_path = "${path.module}/.build/tool_documents.zip"
  source {
    content  = file("${path.module}/lambda-src/tool_documents.py")
    filename = "tool_documents.py"
  }
  source {
    content  = file("${path.module}/lambda-src/agent_tools.py")
    filename = "agent_tools.py"
  }
  # Single source of truth for risk scoring — same module the backend imports.
  source {
    content  = file("${path.module}/../../../backend/src/core/risk_scoring.py")
    filename = "risk_scoring.py"
  }
}

resource "aws_lambda_function" "tool_documents" {
  #checkov:skip=CKV_AWS_115:low-volume tool Lambdas share the account pool; reserved concurrency deferred
  #checkov:skip=CKV_AWS_116:invoked synchronously (Gateway / Step Functions); errors return to the caller
  #checkov:skip=CKV_AWS_117:tool Lambdas call public AWS endpoints; VPC + endpoints deferred (no NAT)
  #checkov:skip=CKV_AWS_272:code is built and deployed by Terraform from this repo; no external publishers
  function_name    = "${var.project_prefix}-tool-documents"
  runtime          = "python3.12"
  handler          = "tool_documents.handler"
  role             = aws_iam_role.tool_lambda.arn
  filename         = data.archive_file.tool_documents_zip.output_path
  source_code_hash = data.archive_file.tool_documents_zip.output_base64sha256
  timeout          = 60
  memory_size      = 512
  kms_key_arn      = aws_kms_key.platform.arn
  tracing_config {
    mode = "Active"
  }
  environment { variables = merge(local.lambda_env, var.tool_documents_tunables) }
}

# ── Execution Tools Lambda (needs ECS permissions) ────────────────────

data "archive_file" "tool_execution_zip" {
  type        = "zip"
  output_path = "${path.module}/.build/tool_execution.zip"
  source {
    content  = file("${path.module}/lambda-src/tool_execution.py")
    filename = "tool_execution.py"
  }
  source {
    content  = file("${path.module}/lambda-src/agent_tools.py")
    filename = "agent_tools.py"
  }
  # Validation L1–L3: tier logic + the in-VPC probe's handler source.
  source {
    content  = file("${path.module}/lambda-src/validation_probe.py")
    filename = "validation_probe.py"
  }
  source {
    content  = file("${path.module}/lambda-src/probe_handler.py")
    filename = "probe_handler.py"
  }
}

resource "aws_lambda_function" "tool_execution" {
  #checkov:skip=CKV_AWS_115:low-volume tool Lambdas share the account pool; reserved concurrency deferred
  #checkov:skip=CKV_AWS_116:invoked synchronously (Gateway / Step Functions); errors return to the caller
  #checkov:skip=CKV_AWS_117:tool Lambdas call public AWS endpoints; VPC + endpoints deferred (no NAT)
  #checkov:skip=CKV_AWS_272:code is built and deployed by Terraform from this repo; no external publishers
  function_name    = "${var.project_prefix}-tool-execution"
  runtime          = "python3.12"
  handler          = "tool_execution.handler"
  role             = aws_iam_role.tool_lambda.arn
  filename         = data.archive_file.tool_execution_zip.output_path
  source_code_hash = data.archive_file.tool_execution_zip.output_base64sha256
  # validate_deployment's in-VPC probe waits for a Lambda ENI.
  timeout     = 900
  memory_size = 512
  kms_key_arn = aws_kms_key.platform.arn
  tracing_config {
    mode = "Active"
  }
  environment {
    variables = merge(local.lambda_env, var.tool_execution_tunables, var.validation_probe_tunables, {
      # Single source with the task definitions (ecs.tf var.worker_container_name).
      WORKER_CONTAINER_NAME = var.worker_container_name
      ECS_CLUSTER           = module.ecs.cluster_name
      ECS_TASK_DEF          = module.worker_task.task_family
      ECS_SUBNETS           = join(",", aws_subnet.public[*].id)
      # Deploy gate (deploy.tf).
      DEPLOY_ENABLED          = tostring(var.enable_deploy)
      DEPLOY_ROLE_ARN         = var.enable_deploy ? aws_iam_role.deploy[0].arn : ""
      DEPLOY_ALLOWED_ACCOUNTS = var.deploy_allowed_accounts
      # Every generated app role carries this boundary (deploy.tf).
      APP_PERMISSIONS_BOUNDARY_NAME = aws_iam_policy.app_permissions_boundary.name
      AWS_PARTITION                 = local.partition
      # Validation probe (validation.tf).
      VALIDATION_PROBE_ROLE_ARN = aws_iam_role.validation_probe.arn
    })
  }
}

# ── Update Gateway role to invoke all tool Lambdas ────────────────────

resource "aws_iam_role_policy" "gateway_invoke_all_tools" {
  name = "${var.project_prefix}-gateway-invoke-all-tools"
  role = aws_iam_role.gateway_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["lambda:InvokeFunction"]
      Resource = [
        aws_lambda_function.tool_portfolio.arn,
        aws_lambda_function.tool_strategy.arn,
        aws_lambda_function.tool_planning.arn,
        aws_lambda_function.tool_documents.arn,
        aws_lambda_function.tool_execution.arn,
      ]
    }]
  })
}

# ── Update tool Lambda role for S3 + ECS ──────────────────────────────

resource "aws_iam_role_policy" "tool_lambda_s3_ecs" {
  #checkov:skip=CKV_AWS_355:only Describe actions with no resource type (ValidationDescribeNoResourceType) keep Resource "*"
  name = "${var.project_prefix}-tool-lambda-s3-ecs"
  role = aws_iam_role.tool_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          "arn:${local.partition}:s3:::${local.data_bucket}",
          "arn:${local.partition}:s3:::${local.data_bucket}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = ["ecs:RunTask", "ecs:DescribeTasks"]
        Resource = [
          "arn:${local.partition}:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${var.project_prefix}-*",
          "arn:${local.partition}:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/*"
        ]
      },
      {
        # RunTask with propagateTags tags the new task; only as part of RunTask.
        Effect    = "Allow"
        Action    = ["ecs:TagResource"]
        Resource  = "arn:${local.partition}:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/*"
        Condition = { StringEquals = { "ecs:CreateAction" = "RunTask" } }
      },
      {
        # RunTask of the worker task definition passes exactly its two roles.
        # The probe role is passed to Lambda in validation.tf.
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [module.worker_task.worker_task_role_arn, module.ecs.execution_role_arn]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        Effect   = "Deny"
        Action   = ["iam:PassRole"]
        Resource = local.deploy_role_named_arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:${local.partition}:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${var.project_prefix}/*"]
      },
      {
        # commit_cicd_to_repo commits generated CI/CD files into the app's
        # CodeCommit repo via the CodeCommit API (no git binary in Lambda).
        Effect = "Allow"
        Action = [
          "codecommit:GetRepository",
          "codecommit:GetBranch",
          "codecommit:GetFile",
          "codecommit:GetFolder",
          "codecommit:CreateCommit",
          "codecommit:CreateBranch",
        ]
        Resource = ["arn:${local.partition}:codecommit:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
      },
      {
        # validate_deployment (Validation phase): READ-ONLY checks of a migrated
        # app's live stack. Actions with a resource type are scoped to this
        # account/region; only the ones without one (below) keep Resource "*".
        Sid    = "ValidationReadsScoped"
        Effect = "Allow"
        Action = [
          "codepipeline:ListPipelineExecutions",
          "ecs:DescribeServices",
          "lambda:GetFunction",
          "lambda:ListVersionsByFunction",
          "rds:DescribeDBInstances",
          "rds:DescribeDBClusters",
          "dynamodb:DescribeTable",
          "apigateway:GET",
        ]
        Resource = [
          "arn:${local.partition}:codepipeline:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*",
          "arn:${local.partition}:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/*",
          "arn:${local.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:*",
          "arn:${local.partition}:rds:${var.aws_region}:${data.aws_caller_identity.current.account_id}:db:*",
          "arn:${local.partition}:rds:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster:*",
          "arn:${local.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/*",
          "arn:${local.partition}:apigateway:${var.aws_region}::/apis",
          "arn:${local.partition}:apigateway:${var.aws_region}::/apis/*",
        ]
      },
      {
        # Describe* calls with no resource-level permissions (Service Authorization Reference).
        Sid    = "ValidationDescribeNoResourceType"
        Effect = "Allow"
        Action = [
          "ecs:DescribeTaskDefinition",
          "autoscaling:DescribeAutoScalingGroups",
          "ec2:DescribeInstances",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeNetworkInterfaces", # an internet-facing ALB's private IP for the in-VPC probe
          "elasticloadbalancing:DescribeLoadBalancers",
          "elasticloadbalancing:DescribeTargetGroups",
          "elasticloadbalancing:DescribeTargetHealth",
        ]
        Resource = "*"
      }
    ]
  })
}

# The execution tool runs the ATX / infra workers as waitForTaskToken tasks and
# MUST be able to deliver the callback. Without SendTaskFailure, a failed launch
# (e.g. an ECS RunTask ServerException) can never report back, so the task hangs
# until its timeout instead of failing fast. Task tokens are not ARN-addressable,
# so these actions have no resource type.
resource "aws_iam_role_policy" "tool_lambda_sfn_callbacks" {
  #checkov:skip=CKV_AWS_355:SendTask* actions have no resource type (Service Authorization Reference)
  name = "${var.project_prefix}-tool-lambda-sfn-callbacks"
  role = aws_iam_role.tool_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DeliverTaskTokenCallbacks"
        Effect = "Allow"
        Action = [
          "states:SendTaskSuccess",
          "states:SendTaskFailure",
          "states:SendTaskHeartbeat"
        ]
        Resource = ["*"]
      }
    ]
  })
}

# ── SFN Bridge Lambda (invoke-runtime) ───────────────────────────────
# Bridges Step Functions to Bedrock (AgentCore fallback).
# Each SFN state calls this with {app_id, mode, prompt}.

data "archive_file" "invoke_runtime_zip" {
  type        = "zip"
  output_path = "${path.module}/.build/invoke_runtime.zip"
  source {
    content  = file("${path.module}/lambda-src/invoke_runtime.py")
    filename = "invoke_runtime.py"
  }
  # Vendor the single-source-of-truth risk scorer so the discovery post-hook can
  # deterministically compute + persist a risk score if the phase didn't produce one.
  source {
    content  = file("${path.module}/../../../backend/src/core/risk_scoring.py")
    filename = "risk_scoring.py"
  }
}

resource "aws_iam_role" "invoke_runtime_role" {
  name        = "${var.project_prefix}-invoke-runtime-role"
  description = "AMF invoke_runtime Lambda: phase pre-hooks, post-checks and approval-gate task tokens for the Step Functions workflow."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "invoke_runtime_perms" {
  name = "${var.project_prefix}-invoke-runtime-perms"
  role = aws_iam_role.invoke_runtime_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        # Cross-region inference profiles route to any US region's foundation model.
        Resource = [
          "arn:${local.partition}:bedrock:*::foundation-model/*",
          "arn:${local.partition}:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        ]
      },
      {
        # Harness ARNs live under the bedrock-agentcore service prefix — the old
        # "arn:aws:bedrock:…:*" never matched, so chat (mode=chat) got AccessDenied.
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime"]
        Resource = ["arn:${local.partition}:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [module.dynamodb.table_arn, "${module.dynamodb.table_arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:${local.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_prefix}-*:*"]
      }
    ]
  })
}

resource "aws_lambda_function" "invoke_runtime" {
  #checkov:skip=CKV_AWS_115:low-volume tool Lambdas share the account pool; reserved concurrency deferred
  #checkov:skip=CKV_AWS_116:invoked synchronously (Gateway / Step Functions); errors return to the caller
  #checkov:skip=CKV_AWS_117:tool Lambdas call public AWS endpoints; VPC + endpoints deferred (no NAT)
  #checkov:skip=CKV_AWS_272:code is built and deployed by Terraform from this repo; no external publishers
  function_name    = "${var.project_prefix}-invoke-runtime"
  runtime          = "python3.12"
  handler          = "invoke_runtime.handler"
  role             = aws_iam_role.invoke_runtime_role.arn
  filename         = data.archive_file.invoke_runtime_zip.output_path
  source_code_hash = data.archive_file.invoke_runtime_zip.output_base64sha256
  timeout          = 900
  memory_size      = 512
  kms_key_arn      = aws_kms_key.platform.arn
  tracing_config {
    mode = "Active"
  }

  environment {
    # No BEDROCK_MODEL_ID: invoke_runtime only calls the Harness, which carries the model.
    variables = merge(local.lambda_env, var.invoke_runtime_tunables, {
      HARNESS_ARN = aws_bedrockagentcore_harness.orchestrator.arn
    })
  }
}

output "invoke_runtime_arn" {
  description = "Invoke runtime ARN"
  value       = aws_lambda_function.invoke_runtime.arn
}

# ── X-Ray active tracing (tracing_config.mode = Active on every Lambda above) ──
resource "aws_iam_role_policy_attachment" "tool_lambda_xray" {
  role       = aws_iam_role.tool_lambda.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy_attachment" "invoke_runtime_xray" {
  role       = aws_iam_role.invoke_runtime_role.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
}
