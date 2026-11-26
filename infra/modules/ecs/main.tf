# kics-scan disable=0afbcfe9-d341-4b92-a64c-7e6de0543879,e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   0afbcfe9: AWS-managed log encryption; customer-managed KMS deferred / adopting team's choice
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
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

variable "container_insights" {
  description = "ECS cluster Container Insights setting (enabled | enhanced | disabled)."
  type        = string
  default     = "enabled"
}

variable "log_retention_days" {
  type        = number
  description = "Retention (days) of the /ecs/{prefix} cluster log group."
  default     = 7
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_ecs_cluster" "main" {
  name = "${var.project_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = var.container_insights
  }

  tags = { project = var.project_prefix, managed_by = "terraform" }
}

# CloudWatch log group (retention: var.log_retention_days)
resource "aws_cloudwatch_log_group" "ecs" {
  #checkov:skip=CKV_AWS_338:retention is a variable (log_retention_days); 1 year is not required for the demo
  name              = "/ecs/${var.project_prefix}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = { project = var.project_prefix, managed_by = "terraform" }
}

# IAM role for ECS task execution (pulling images, writing logs)
resource "aws_iam_role" "ecs_execution" {
  name        = "${var.project_prefix}-ecs-execution"
  description = "AMF ECS task execution role: pulls the worker/API image and writes container logs."
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

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow ECS to fetch this project's secrets ({prefix}/*) for container env injection
resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "${var.project_prefix}-ecs-execution-secrets"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:${data.aws_partition.current.partition}:secretsmanager:${var.aws_region}:*:secret:${var.project_prefix}/*"
    }]
  })
}

output "cluster_id" {
  description = "Cluster ID"
  value       = aws_ecs_cluster.main.id
}
output "cluster_arn" {
  description = "Cluster ARN"
  value       = aws_ecs_cluster.main.arn
}
output "cluster_name" {
  description = "Cluster name"
  value       = aws_ecs_cluster.main.name
}
output "execution_role_arn" {
  description = "Execution role ARN"
  value       = aws_iam_role.ecs_execution.arn
}
output "log_group_name" {
  description = "Log group name"
  value       = aws_cloudwatch_log_group.ecs.name
}
