# kics-scan disable=084c6686-2a70-4710-91b1-000393e54c12,69e7c320-b65d-41bb-be02-d63ecc0bcc9d,afecd1f1-6378-4f7e-bb3b-60c35801fdd4,d1846b12-20c5-4d45-8798-fc35b79268eb,de7f5e83-da88-4046-871f-ea18504b1d43,e592a0c5-5bdb-414c-9066-5dba7cdea370,f861041c-8c9f-4156-acfc-5e6e524f5884
# Accepted kics findings for this file (scanner suppressions; reasons):
#   f861041c: the only bucket without server access logging is the ALB access-log bucket itself
#   084c6686: Shield Advanced is an org-level subscription; Shield Standard applies
#   69e7c320: access is granted by IAM in the same account; no cross-account repository policy needed
#   afecd1f1: deletion protection would block terraform destroy / teardown
#   d1846b12: the pipeline deploys the immutable commit-SHA tag; digest pinning deferred
#   de7f5e83: HTTP listener only when no certificate_arn is set (internal ALB); with one, HTTP redirects to HTTPS
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
################################################################################
# ECS Fargate Module — Cluster + Service + ALB + ECR + CloudWatch
# Encryption: a per-app KMS key (alias/{app_id}-ecs) for the ECR repo + log group.
# ALB: access logs to {app_id}-alb-logs-{account}-{region} (SSE-S3, as ELB requires);
#      HTTPS (TLS 1.3/1.2 policy) + HTTP->HTTPS redirect when certificate_arn is set.
# Naming: {app_id}-cluster, {app_id}-service, {app_id}-alb, {app_id}-ecr
################################################################################

variable "app_id" {
  description = "App ID"
  type        = string
}
variable "vpc_id" {
  description = "VPC ID"
  type        = string
}
variable "public_subnet_ids" {
  description = "Public subnet IDs"
  type        = list(string)
}
variable "private_subnet_ids" {
  description = "Private subnet IDs"
  type        = list(string)
}
variable "container_port" {
  description = "Container port"
  type        = number
  default     = 8080
}
variable "health_check_path" {
  description = "Health check path"
  type        = string
  default     = "/health"
}
variable "cpu" {
  description = "CPU"
  type        = number
  default     = 1024
}
variable "memory" {
  description = "Memory"
  type        = number
  default     = 2048
}
variable "desired_count" {
  description = "Desired count"
  type        = number
  default     = 2
}
variable "permissions_boundary_arn" {
  description = "AMF app permissions boundary; the deploy role refuses roles without it; required (fail closed)"
  type        = string
}
variable "tags" {
  description = "Tags"
  type        = map(string)
  default     = {}
}
# Not exposed by default: the ALB is INTERNAL (private subnets) and accepts
# traffic only from allowed_ingress_cidrs (the generator passes the VPC CIDR).
# Exposing the service publicly is an explicit, reviewed opt-in.
variable "internal_alb" {
  description = "Internal ALB"
  type        = bool
  default     = true
}
variable "allowed_ingress_cidrs" {
  description = "Allowed ingress cidrs"
  type        = list(string)
  default     = []
  validation {
    condition     = !contains(var.allowed_ingress_cidrs, "0.0.0.0/0") && !contains(var.allowed_ingress_cidrs, "::/0")
    error_message = "allowed_ingress_cidrs must not contain 0.0.0.0/0 or ::/0 — the service must not be open to the internet."
  }
}
# App configuration + data access (e.g. the generated DynamoDB table): plain env
# vars on the container and CRUD on the given table ARNs for the task role.
variable "environment" {
  description = "Environment"
  type        = map(string)
  default     = {}
}
# Env vars injected from Secrets Manager: NAME -> valueFrom (secret ARN, optionally
# ":json-key::"). The execution role may read exactly these secrets (+ their KMS keys).
variable "secrets" {
  description = "Secrets"
  type        = map(string)
  default     = {}
}
variable "secret_kms_key_arns" {
  description = "Secret KMS key ARNs"
  type        = list(string)
  default     = []
}
variable "dynamodb_table_arns" {
  description = "DynamoDB table ARNs"
  type        = list(string)
  default     = []
}
variable "db_egress_port" {
  description = "RDS/Aurora port; 0 = no database egress"
  type        = number
  default     = 0
}
variable "db_egress_cidr_blocks" {
  description = "Where the database lives (the app VPC CIDR); never 0.0.0.0/0"
  type        = list(string)
  default     = []
  validation {
    condition     = !contains(var.db_egress_cidr_blocks, "0.0.0.0/0")
    error_message = "db_egress_cidr_blocks must be the VPC CIDR, never 0.0.0.0/0."
  }
}
variable "log_retention_days" {
  description = "CloudWatch log retention (days)"
  type        = number
  default     = 30
}
variable "image_tag" {
  description = "Image tag of the Terraform-registered task definition. The pipeline deploys the immutable commit-SHA tag over it (imagedefinitions.json)."
  type        = string
  default     = "latest"
}
# TLS: with an ACM certificate the ALB serves HTTPS (TLS 1.3/1.2 only) and port 80
# redirects to it. Empty = plain HTTP on the INTERNAL ALB (no domain/cert is known
# at generation).
variable "certificate_arn" {
  description = "ACM certificate ARN for the HTTPS listener (empty = HTTP only)"
  type        = string
  default     = ""
}
variable "https_port" {
  description = "HTTPS listener port"
  type        = number
  default     = 443
}
variable "alb_log_retention_days" {
  description = "ALB access-log retention in the log bucket (days)"
  type        = number
  default     = 90
}
variable "alb_log_abort_multipart_days" {
  description = "Abort incomplete multipart uploads to the ALB log bucket after (days)"
  type        = number
  default     = 7
}
variable "cpu_architecture" {
  description = "X86_64 | ARM64 (Graviton)"
  type        = string
  default     = "X86_64"
  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

locals {
  name_prefix = var.app_id
  common_tags = merge(var.tags, {
    Application = var.app_id
    ManagedBy   = "amf-terraform"
  })
  https          = var.certificate_arn != ""
  account_id     = data.aws_caller_identity.current.account_id
  partition      = data.aws_partition.current.partition
  region         = data.aws_region.current.name
  log_group_name = "/ecs/${var.app_id}"
  # Must start with the lowercase app id: the app permissions boundary scopes S3 to app-*.
  alb_log_bucket = lower("${var.app_id}-alb-logs-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}")
  alb_log_prefix = "alb"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_elb_service_account" "current" {}

# ── Per-app KMS key (ECR images + CloudWatch logs) ──────────────────────────
# Account root administers the key; app roles use it through their own IAM
# policies (capped by the app permissions boundary). CloudWatch Logs may use it
# only for this app's log group (encryption-context condition).
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
      values   = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.log_group_name}"]
    }
  }
}

resource "aws_kms_key" "app" {
  description         = "${var.app_id} ECS encryption (ECR + logs)"
  enable_key_rotation = true
  policy              = data.aws_iam_policy_document.kms.json
  tags                = local.common_tags
}

resource "aws_kms_alias" "app" {
  name          = "alias/${lower(var.app_id)}-ecs"
  target_key_id = aws_kms_key.app.key_id
}

# ECR Repository
resource "aws_ecr_repository" "app" {
  name = lower(var.app_id)
  # The pipeline pushes one tag per commit (CODEBUILD_RESOLVED_SOURCE_VERSION) and
  # never overwrites it; a re-run of the same commit skips the push.
  image_tag_mutability = "IMMUTABLE"
  # Tear down must not fail on CI-built images; CI rebuilds them.
  force_delete = true
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.app.arn
  }
  tags = local.common_tags
}

# ECS Cluster
resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = local.common_tags
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "app" {
  #checkov:skip=CKV_AWS_338:retention is a variable (log_retention_days)
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.app.arn
  tags              = local.common_tags
}

# Task Execution Role
resource "aws_iam_role" "ecs_execution" {
  name                 = "${local.name_prefix}-ecs-exec-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  count = length(var.secrets) > 0 ? 1 : 0
  name  = "${local.name_prefix}-ecs-exec-secrets"
  role  = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"],
      Resource = distinct([for v in values(var.secrets) : regex("^arn:[^:]+:secretsmanager:[^:]+:[^:]+:secret:[^:]+", v)]) }],
      length(var.secret_kms_key_arns) > 0 ? [{ Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.secret_kms_key_arns }] : []
    )
  })
}

# Task Role
resource "aws_iam_role" "ecs_task" {
  name                 = "${local.name_prefix}-ecs-task-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "ecs_task_dynamodb" {
  count = length(var.dynamodb_table_arns) > 0 ? 1 : 0
  name  = "${local.name_prefix}-ecs-task-dynamodb"
  role  = aws_iam_role.ecs_task.id
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

# Task Definition
resource "aws_ecs_task_definition" "app" {
  family                   = "${local.name_prefix}-task"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name         = var.app_id
    image        = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = var.container_port, protocol = "tcp" }]
    environment  = [for k, v in var.environment : { name = k, value = v }]
    secrets      = [for k, v in var.secrets : { name = k, valueFrom = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = { "awslogs-group" = aws_cloudwatch_log_group.app.name, "awslogs-region" = data.aws_region.current.name, "awslogs-stream-prefix" = "ecs" }
    }
    # Slim images often lack curl: fall back to wget, then python, before failing.
    healthCheck = { command = ["CMD-SHELL", "U=http://localhost:${var.container_port}${var.health_check_path}; curl -fs $U >/dev/null 2>&1 || wget -q -O /dev/null $U 2>/dev/null || python3 -c \"import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=4)\" $U || exit 1"], interval = 30, timeout = 5, retries = 3, startPeriod = 30 }
  }])

  tags = local.common_tags
}

data "aws_region" "current" {}

# Security Groups
resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb-sg"
  description = "ALB ingress from allowed_ingress_cidrs only (never 0.0.0.0/0)"
  vpc_id      = var.vpc_id
  dynamic "ingress" {
    for_each = length(var.allowed_ingress_cidrs) > 0 ? [1] : []
    content {
      description = "HTTP from allowed CIDRs (redirects to HTTPS when certificate_arn is set)"
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      cidr_blocks = var.allowed_ingress_cidrs
    }
  }
  dynamic "ingress" {
    for_each = length(var.allowed_ingress_cidrs) > 0 && local.https ? [1] : []
    content {
      description = "HTTPS from allowed CIDRs"
      from_port   = var.https_port
      to_port     = var.https_port
      protocol    = "tcp"
      cidr_blocks = var.allowed_ingress_cidrs
    }
  }
  egress {
    description     = "To the ECS tasks only"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }
  tags = merge(local.common_tags, { Name = "${local.name_prefix}-alb-sg" })
}

resource "aws_security_group" "ecs" {
  name        = "${local.name_prefix}-ecs-sg"
  description = "ECS tasks: ingress from the ALB only; egress for ECR/AWS APIs via NAT"
  vpc_id      = var.vpc_id
  egress {
    description = "AWS APIs / ECR / DynamoDB (HTTPS only)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  # The app's managed database (RDS/Aurora) inside the VPC. Without it JDBC times out and the
  # tasks never pass ALB health checks (APP-PETCLINIC-EC2 2026-11-04).
  dynamic "egress" {
    for_each = var.db_egress_port > 0 && length(var.db_egress_cidr_blocks) > 0 ? [1] : []
    content {
      description = "App database (VPC only)"
      from_port   = var.db_egress_port
      to_port     = var.db_egress_port
      protocol    = "tcp"
      cidr_blocks = var.db_egress_cidr_blocks
    }
  }
  tags = merge(local.common_tags, { Name = "${local.name_prefix}-ecs-sg" })
}

# Separate rule: the ALB and ECS groups reference each other.
resource "aws_security_group_rule" "ecs_from_alb" {
  type                     = "ingress"
  description              = "App port from the ALB only"
  from_port                = var.container_port
  to_port                  = var.container_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.ecs.id
  source_security_group_id = aws_security_group.alb.id
}

# ── ALB access logs (S3, SSE-S3 — the only encryption ELB log delivery supports) ──
resource "aws_s3_bucket" "alb_logs" {
  #checkov:skip=CKV_AWS_18:this is the access-log bucket; logging it would need another log bucket
  #checkov:skip=CKV_AWS_144:cross-region replication of access logs is the adopting team's DR choice
  #checkov:skip=CKV_AWS_145:ELB access-log delivery supports SSE-S3 only, not SSE-KMS
  #checkov:skip=CKV2_AWS_62:no consumer for S3 event notifications
  bucket = local.alb_log_bucket
  # Tear down must not fail on delivered log objects.
  force_destroy = true
  tags          = local.common_tags
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket                  = aws_s3_bucket.alb_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter {}
    expiration { days = var.alb_log_retention_days }
    noncurrent_version_expiration { noncurrent_days = var.alb_log_retention_days }
    abort_incomplete_multipart_upload { days_after_initiation = var.alb_log_abort_multipart_days }
  }
  depends_on = [aws_s3_bucket_versioning.alb_logs]
}

# ELB log delivery may only write this app's prefix; TLS-only access.
data "aws_iam_policy_document" "alb_logs" {
  statement {
    sid       = "ElbLogDeliveryRegionalAccount"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.alb_logs.arn}/${local.alb_log_prefix}/AWSLogs/${local.account_id}/*"]
    principals {
      type        = "AWS"
      identifiers = [data.aws_elb_service_account.current.arn]
    }
  }
  statement {
    sid       = "ElbLogDeliveryService"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.alb_logs.arn}/${local.alb_log_prefix}/AWSLogs/${local.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.alb_logs.arn, "${aws_s3_bucket.alb_logs.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket     = aws_s3_bucket.alb_logs.id
  policy     = data.aws_iam_policy_document.alb_logs.json
  depends_on = [aws_s3_bucket_public_access_block.alb_logs]
}

# ALB
resource "aws_lb" "main" {
  #checkov:skip=CKV2_AWS_20:HTTP->HTTPS redirect is on when certificate_arn is set; no domain/cert is known at generation (internal ALB)
  #checkov:skip=CKV_AWS_150:deletion protection would block terraform destroy of a fresh app stack
  name                       = "${local.name_prefix}-alb"
  internal                   = var.internal_alb
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.internal_alb ? var.private_subnet_ids : var.public_subnet_ids
  drop_invalid_header_fields = true
  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = local.alb_log_prefix
    enabled = true
  }
  tags       = local.common_tags
  depends_on = [aws_s3_bucket_policy.alb_logs]
}

resource "aws_lb_target_group" "app" {
  #checkov:skip=CKV_AWS_378:ALB to target inside the VPC
  name        = "${local.name_prefix}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check {
    path                = var.health_check_path
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  tags = local.common_tags
}

# Port 80: forwards only while no certificate is configured (INTERNAL ALB by default);
# with certificate_arn it only redirects to HTTPS.
resource "aws_lb_listener" "http" {
  #checkov:skip=CKV_AWS_103:TLS applies to the HTTPS listener; port 80 forwards only without a certificate_arn (internal ALB), else redirects
  #checkov:skip=CKV_AWS_2:port 80 forwards only without a certificate_arn (internal ALB), else redirects to HTTPS
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  # nosemgrep: terraform.aws.security.insecure-load-balancer-tls-version.insecure-load-balancer-tls-version
  protocol = "HTTP" # no domain/ACM cert is known at generation; redirects to HTTPS once certificate_arn is set
  dynamic "default_action" {
    for_each = local.https ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = tostring(var.https_port)
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
  dynamic "default_action" {
    for_each = local.https ? [] : [1]
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.app.arn
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = var.https_port
  protocol          = "HTTPS"
  # TLS 1.3 + 1.2 only. A security contract, not a tunable: weakening it is a reviewed change.
  ssl_policy      = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# ECS Service
resource "aws_ecs_service" "app" {
  name            = "${local.name_prefix}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = var.app_id
    container_port   = var.container_port
  }

  tags = local.common_tags
}

# Outputs
output "cluster_arn" {
  description = "Cluster ARN"
  value       = aws_ecs_cluster.main.arn
}
output "service_name" {
  description = "Service name"
  value       = aws_ecs_service.app.name
}
output "alb_dns" {
  description = "ALB DNS"
  value       = aws_lb.main.dns_name
}
output "alb_internal" {
  description = "ALB internal"
  value       = aws_lb.main.internal
}
output "ecr_url" {
  description = "ECR URL"
  value       = aws_ecr_repository.app.repository_url
}
output "kms_key_arn" {
  description = "App KMS key ARN (ECR + logs)"
  value       = aws_kms_key.app.arn
}
output "alb_log_bucket" {
  description = "ALB access-log bucket"
  value       = aws_s3_bucket.alb_logs.bucket
}
output "log_group" {
  description = "Log group"
  value       = aws_cloudwatch_log_group.app.name
}
output "task_role_arn" {
  description = "Task role ARN"
  value       = aws_iam_role.ecs_task.arn
}
output "app_security_group_id" {
  description = "App security group ID"
  value       = aws_security_group.ecs.id
}
