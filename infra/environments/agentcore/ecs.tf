# ══════════════════════════════════════════════════════════════════════
# ECS — cluster + ATX worker task definition (this environment owns them).
#
# The Step Functions LaunchATX / LaunchTransformation states run the ATX worker
# on Fargate via the execution tool Lambda (Option C callback pattern). The
# worker task role is scoped to THIS environment's DynamoDB table + S3 bucket
# and carries states:SendTask* for the callback — no cross-environment coupling.
# ══════════════════════════════════════════════════════════════════════

variable "worker_image_tag" {
  description = "Tag of the worker image in ECR used by the worker + API task definitions (setup.sh pushes :latest)."
  type        = string
  default     = "latest"
}

variable "worker_container_name" {
  description = "Container name in the worker/API task definitions. tool_execution (WORKER_CONTAINER_NAME env) and the ALB target group read it from here."
  type        = string
  default     = "migration-worker"
}

variable "worker_cpu" {
  description = "ATX worker task CPU units (ATX + Bedrock calls)."
  type        = string
  default     = "4096"
}

variable "worker_memory" {
  description = "ATX worker task memory (MiB)."
  type        = string
  default     = "8192"
}

variable "api_cpu" {
  description = "Hosted API task CPU units."
  type        = string
  default     = "1024"
}

variable "api_memory" {
  description = "Hosted API task memory (MiB)."
  type        = string
  default     = "2048"
}

module "ecs" {
  source             = "../../modules/ecs"
  project_prefix     = var.project_prefix
  aws_region         = var.aws_region
  log_retention_days = local.ecs_log_retention_days
  container_insights = var.ecs_container_insights
  kms_key_arn        = aws_kms_key.platform.arn
}

module "worker_task" {
  source                = "../../modules/worker-task"
  project_prefix        = var.project_prefix
  aws_region            = var.aws_region
  aws_account_id        = data.aws_caller_identity.current.account_id
  kms_key_arn           = aws_kms_key.platform.arn
  execution_role_arn    = module.ecs.execution_role_arn
  worker_image          = "${module.ecr.assessment_worker_repo_url}:${var.worker_image_tag}"
  worker_container_name = var.worker_container_name
  worker_cpu            = var.worker_cpu
  worker_memory         = var.worker_memory
  api_cpu               = var.api_cpu
  api_memory            = var.api_memory
  log_retention_days    = local.worker_log_retention_days
  vpc_id                = aws_vpc.main.id
  vpc_cidr              = aws_vpc.main.cidr_block
  dynamodb_table_arn    = module.dynamodb.table_arn
  dynamodb_table_name   = module.dynamodb.table_name
  atx_s3_bucket         = local.data_bucket
  bedrock_model_id      = var.bedrock_model_id
  dev_auth              = var.dev_auth

  # Surfaced by /system/capabilities + /infrastructure (console links, Settings tab).
  agentcore_harness_arn = aws_bedrockagentcore_harness.orchestrator.arn
  agentcore_gateway_id  = aws_bedrockagentcore_gateway.main.gateway_id
  agentcore_gateway_url = "https://${aws_bedrockagentcore_gateway.main.gateway_id}.gateway.bedrock-agentcore.${var.aws_region}.amazonaws.com/mcp"

  # The hosted (ECS) API validates Cognito JWTs; pass the pool/client so its task
  # definition is configured. Empty when Cognito is disabled (local dev only).
  cognito_user_pool_id  = local.cognito > 0 ? aws_cognito_user_pool.portal[0].id : ""
  cognito_app_client_id = local.cognito > 0 ? aws_cognito_user_pool_client.spa[0].id : ""

  # Graph store (neptune.tf graph_backend): the hosted API serves the graph API from it.
  api_extra_environment = merge(
    { GRAPH_BACKEND = local.graph_backend, DEPLOY_ENABLED = tostring(var.enable_deploy) },
    local.neptune > 0 ? { NEPTUNE_ENDPOINT = aws_neptune_cluster.graph[0].endpoint } : {},
  )
}
