# kics-scan disable=1b6799eb-4a7a-4b04-9001-8cceb9999326,3ef8696c-e4ae-4872-92c7-520bb44dfe77,720f44cf-285e-4b69-8f72-835e6bc1dceb,c5b31ab9-0f26-4a49-b8aa-4cc064392f4d,e39f87f5-0abf-488b-864c-63ee1f588140,e592a0c5-5bdb-414c-9066-5dba7cdea370,f861041c-8c9f-4156-acfc-5e6e524f5884
# Accepted kics findings for this file (scanner suppressions; reasons):
#   1b6799eb: API access logging needs a log group the adopting team sizes
#   3ef8696c: the HTTP API is the app's entry point; it is internal unless expose_publicly (core/exposure_policy.py)
#   720f44cf: invoked synchronously; errors return to the caller
#   c5b31ab9: MFA delete can only be set by the root user via CLI
#   e39f87f5: no consumer for S3 event notifications
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   f861041c: access logging needs a log bucket; deferred / adopting team's choice
################################################################################
# Lambda Module — Function + API Gateway + CloudWatch + artifact bucket
# Naming: {app_id}-function, {app_id}-api,
#         lower("{app_id}-artifacts-{account}-{region}") (same as the ec2 module)
#
# Terraform creates the function with a placeholder package; CI uploads the real
# package to the artifact bucket and CD runs `aws lambda update-function-code
# --publish` (cicd-role / codepipeline deploy_target = "lambda"). The function's
# code attributes are ignored after creation so a later apply never reverts it.
# Encryption: a per-app KMS key (alias/{app_id}-lambda) for the function env vars
# and the log groups. X-Ray active tracing is on.
################################################################################

variable "app_id" {
  description = "App ID"
  type        = string
}
variable "runtime" {
  description = "Runtime"
  type        = string
  default     = "python3.12"
}
variable "memory_size" {
  description = "Memory size"
  type        = number
  default     = 512
}
variable "timeout" {
  description = "Timeout"
  type        = number
  default     = 30
}
variable "handler" {
  description = "Handler"
  type        = string
  default     = "app.handler"
}
variable "vpc_id" {
  description = "VPC ID"
  type        = string
  default     = ""
}
variable "private_subnet_ids" {
  description = "Private subnet IDs"
  type        = list(string)
  default     = []
}
# Must be known at plan time: vpc_id / subnet ids come from module.vpc and are
# unknown on a fresh plan, so they can't drive `count` (APP-CLI 2026-10-13:
# "Invalid count argument" on lambda_vpc / aws_security_group.lambda).
variable "attach_vpc" {
  description = "Attach VPC"
  type        = bool
  default     = false
}
# Several functions (from the repo's SAM/CDK template). Empty = one
# function using handler/memory_size/timeout below.
variable "functions" {
  description = "Functions"
  type = map(object({
    handler        = string
    memory_size    = number
    timeout        = number
    env            = map(string)
    routes         = list(string)
    payload_format = string
  }))
  default = {}
}
# App configuration + data access (e.g. the generated DynamoDB table).
variable "environment" {
  description = "Environment"
  type        = map(string)
  default     = {}
}
variable "dynamodb_table_arns" {
  description = "DynamoDB table ARNs"
  type        = list(string)
  default     = []
}
variable "permissions_boundary_arn" {
  description = "AMF app permissions boundary; the deploy role refuses roles without it; required (fail closed)"
  type        = string
}
variable "log_retention_days" {
  description = "CloudWatch log retention (days)"
  type        = number
  default     = 14
}
variable "tracing_mode" {
  description = "X-Ray tracing mode (Active | PassThrough)"
  type        = string
  default     = "Active"
  validation {
    condition     = contains(["Active", "PassThrough"], var.tracing_mode)
    error_message = "tracing_mode must be Active or PassThrough."
  }
}
variable "tags" {
  description = "Tags"
  type        = map(string)
  default     = {}
}
# API auth. Routes are never anonymous (core/exposure_policy.py): AWS_IAM (SigV4)
# by default; set both to use a JWT authorizer instead (e.g. a Cognito user pool:
# issuer https://cognito-idp.<region>.amazonaws.com/<pool-id>, audience = client id).
variable "jwt_issuer" {
  description = "JWT issuer"
  type        = string
  default     = ""
}
variable "jwt_audience" {
  description = "JWT audience"
  type        = list(string)
  default     = []
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  name_prefix = var.app_id
  # Must match the CI templates' artifact_bucket ({app_id lowercased}-artifacts-{account}-{region}).
  artifact_bucket = lower("${var.app_id}-artifacts-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}")
  common_tags = merge(var.tags, {
    Application = var.app_id
    ManagedBy   = "amf-terraform"
  })
  use_vpc    = var.attach_vpc
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.name
}

# ── Per-app KMS key (function env vars + CloudWatch logs) ──────────────────
# Account root administers the key (Lambda uses it through a grant the deploying
# principal creates); CloudWatch Logs may use it only for this app's
# /aws/lambda/{app_id}-* log groups (encryption-context condition).
data "aws_iam_policy_document" "kms" {
  #checkov:skip=CKV_AWS_109:KMS key policy: root-account statement required by KMS
  #checkov:skip=CKV_AWS_111:KMS key policy: root-account statement required by KMS
  #checkov:skip=CKV_AWS_356:KMS key policy: Resource "*" means this key
  statement {
    sid    = "AccountAdministration"
    effect = "Allow"
    actions = [
      # Exact key-administration + use actions for the account root (IAM delegation); no kms:* / wildcards.
      # kms:PutKeyPolicy keeps the account able to repair this policy (no lockout).
      "kms:CreateAlias", "kms:UpdateAlias", "kms:DeleteAlias", "kms:CreateGrant", "kms:ListGrants", "kms:RevokeGrant",
      "kms:RetireGrant", "kms:DescribeKey", "kms:EnableKey", "kms:DisableKey", "kms:EnableKeyRotation",
      "kms:DisableKeyRotation", "kms:GetKeyRotationStatus", "kms:RotateKeyOnDemand", "kms:ListKeyRotations",
      "kms:GetKeyPolicy", "kms:PutKeyPolicy", "kms:ListKeyPolicies", "kms:ListAliases", "kms:ListResourceTags",
      "kms:TagResource", "kms:UntagResource", "kms:UpdateKeyDescription", "kms:ScheduleKeyDeletion",
      "kms:CancelKeyDeletion", "kms:Encrypt", "kms:Decrypt", "kms:ReEncryptFrom", "kms:ReEncryptTo",
      "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext",
    ]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }
  statement {
    sid       = "CloudWatchLogsForThisApp"
    effect    = "Allow"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.name_prefix}-*"]
    }
  }
}

resource "aws_kms_key" "app" {
  description         = "${var.app_id} Lambda encryption (env vars + logs)"
  enable_key_rotation = true
  policy              = data.aws_iam_policy_document.kms.json
  tags                = local.common_tags
}

resource "aws_kms_alias" "app" {
  name          = "alias/${lower(var.app_id)}-lambda"
  target_key_id = aws_kms_key.app.key_id
}

# ── Artifact bucket (CI uploads the function package; CD deploys it) ──────
resource "aws_s3_bucket" "artifacts" {
  #checkov:skip=CKV2_AWS_61:artifact retention is the adopting team's choice
  #checkov:skip=CKV2_AWS_62:no consumer for S3 event notifications
  #checkov:skip=CKV_AWS_144:cross-region replication is the adopting team's DR choice
  #checkov:skip=CKV_AWS_18:access logging needs a log bucket the adopting team owns
  bucket = local.artifact_bucket
  # Tear down must not fail on CI-built packages (versioned); CI rebuilds them.
  force_destroy = true
  tags          = local.common_tags
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
  }
}

# Lambda execution role
resource "aws_iam_role" "lambda" {
  name                 = "${local.name_prefix}-lambda-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  count      = local.use_vpc ? 1 : 0
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# X-Ray active tracing (xray:* writes are within the app permissions boundary).
resource "aws_iam_role_policy_attachment" "lambda_xray" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "lambda_dynamodb" {
  count = length(var.dynamodb_table_arns) > 0 ? 1 : 0
  name  = "${local.name_prefix}-lambda-dynamodb"
  role  = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
        "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:DescribeTable"]
        Resource = concat(var.dynamodb_table_arns, [for a in var.dynamodb_table_arns : "${a}/index/*"])
      },
      # The table is encrypted with its own KMS key (dynamodb module); data access
      # decrypts the table key through DynamoDB only.
      {
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource  = "arn:${data.aws_partition.current.partition}:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:key/*"
        Condition = { StringEquals = { "kms:ViaService" = "dynamodb.${data.aws_region.current.name}.amazonaws.com" } }
      },
    ]
  })
}

# CloudWatch Log Group
locals {
  # One entry per function. No `functions` → the single
  # `{app_id}-function` behind the `$default` route (unchanged naming, so CD and
  # existing state keep working). Several → `{app_id}-{key}`, one HTTP API route
  # per "METHOD /path"; payload_format "1.0" keeps REST-API-v1 handlers
  # (APIGatewayProxyRequestEvent) working.
  functions = length(var.functions) > 0 ? var.functions : {
    default = {
      handler = var.handler, memory_size = var.memory_size, timeout = var.timeout,
      env     = {}, routes = ["$default"], payload_format = "1.0"
    }
  }
  function_names = { for k, _ in local.functions : k => (length(var.functions) > 0 ? "${local.name_prefix}-${k}" : "${local.name_prefix}-function") }
  routes         = merge([for k, f in local.functions : { for r in f.routes : "${k}|${r}" => { fn = k, route = r } }]...)
}

resource "aws_cloudwatch_log_group" "lambda" {
  #checkov:skip=CKV_AWS_338:retention is a variable (log_retention_days)
  for_each          = local.functions
  name              = "/aws/lambda/${local.function_names[each.key]}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.app.arn
  tags              = local.common_tags
}

# Lambda Functions (placeholder package — the real code is deployed by CI/CD)
resource "aws_lambda_function" "app" {
  #checkov:skip=CKV_AWS_115:concurrency limits are sized by the adopting team
  #checkov:skip=CKV_AWS_116:invoked synchronously via API Gateway; errors return to the caller
  #checkov:skip=CKV_AWS_272:code is deployed by the pipeline from this repo
  for_each      = local.functions
  function_name = local.function_names[each.key]
  role          = aws_iam_role.lambda.arn
  runtime       = var.runtime
  handler       = each.value.handler
  memory_size   = each.value.memory_size
  timeout       = each.value.timeout
  filename      = data.archive_file.placeholder.output_path
  kms_key_arn   = aws_kms_key.app.arn

  tracing_config {
    mode = var.tracing_mode
  }

  environment {
    variables = merge({
      APP_ID      = var.app_id
      ENVIRONMENT = "production"
    }, var.environment, each.value.env)
  }

  dynamic "vpc_config" {
    for_each = local.use_vpc ? [1] : []
    content {
      subnet_ids         = var.private_subnet_ids
      security_group_ids = [aws_security_group.lambda[0].id]
    }
  }

  tags       = local.common_tags
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy_attachment.lambda_xray]

  # CD owns the code from here on (update-function-code); don't revert it.
  lifecycle {
    ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, s3_object_version]
  }
}

# Single-function stacks applied before for_each keep their resources.
moved {
  from = aws_lambda_function.app
  to   = aws_lambda_function.app["default"]
}
moved {
  from = aws_cloudwatch_log_group.lambda
  to   = aws_cloudwatch_log_group.lambda["default"]
}

# Placeholder deployment package
data "archive_file" "placeholder" {
  type        = "zip"
  output_path = "${path.module}/placeholder.zip"
  source {
    content  = "def handler(event, context): return {'statusCode': 200, 'body': 'AMF placeholder'}"
    filename = "app.py"
  }
}

# Security Group (if VPC)
resource "aws_security_group" "lambda" {
  count       = local.use_vpc ? 1 : 0
  name        = "${local.name_prefix}-lambda-sg"
  description = "Lambda ENIs: outbound only"
  vpc_id      = var.vpc_id
  egress {
    description = "Outbound (AWS APIs, database)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.common_tags, { Name = "${local.name_prefix}-lambda-sg" })
}

# API Gateway (HTTP API)
resource "aws_apigatewayv2_api" "main" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"
  tags          = local.common_tags
}

resource "aws_apigatewayv2_integration" "lambda" {
  for_each               = local.functions
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.app[each.key].invoke_arn
  integration_method     = "POST"
  payload_format_version = each.value.payload_format
}

moved {
  from = aws_apigatewayv2_integration.lambda
  to   = aws_apigatewayv2_integration.lambda["default"]
}

resource "aws_apigatewayv2_authorizer" "jwt" {
  count            = var.jwt_issuer != "" ? 1 : 0
  api_id           = aws_apigatewayv2_api.main.id
  name             = "${local.name_prefix}-jwt"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    issuer   = var.jwt_issuer
    audience = var.jwt_audience
  }
}

# An anonymous $default route failed the exposure policy (APP-CLI 2026-10-19:
# "API route $default has no authorizer"); every route is SigV4 or JWT.
resource "aws_apigatewayv2_route" "app" {
  for_each           = local.routes
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = each.value.route
  target             = "integrations/${aws_apigatewayv2_integration.lambda[each.value.fn].id}"
  authorization_type = var.jwt_issuer != "" ? "JWT" : "AWS_IAM"
  authorizer_id      = var.jwt_issuer != "" ? aws_apigatewayv2_authorizer.jwt[0].id : null
}

moved {
  from = aws_apigatewayv2_route.default
  to   = aws_apigatewayv2_route.app["default|$default"]
}

resource "aws_apigatewayv2_stage" "default" {
  #checkov:skip=CKV_AWS_76:access logging needs a log group the adopting team sizes
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  for_each      = local.functions
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app[each.key].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

moved {
  from = aws_lambda_permission.apigw
  to   = aws_lambda_permission.apigw["default"]
}

# Outputs
output "function_arn" {
  description = "Function ARN"
  value       = values(aws_lambda_function.app)[0].arn
}
output "function_name" {
  description = "Function name"
  value       = values(aws_lambda_function.app)[0].function_name
}
output "function_names" {
  description = "Function names"
  value       = [for f in aws_lambda_function.app : f.function_name]
}
output "api_endpoint" {
  description = "API endpoint"
  value       = aws_apigatewayv2_stage.default.invoke_url
}
output "lambda_role_arn" {
  description = "Lambda role ARN"
  value       = aws_iam_role.lambda.arn
}
output "kms_key_arn" {
  description = "App KMS key ARN (env vars + logs)"
  value       = aws_kms_key.app.arn
}
output "log_group" {
  description = "Log group"
  value       = values(aws_cloudwatch_log_group.lambda)[0].name
}
output "artifact_bucket" {
  description = "Artifact bucket"
  value       = aws_s3_bucket.artifacts.bucket
}
