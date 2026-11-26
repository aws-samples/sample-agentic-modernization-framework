# kics-scan disable=084c6686-2a70-4710-91b1-000393e54c12,afecd1f1-6378-4f7e-bb3b-60c35801fdd4,ba2ed23b-52d3-45ca-be25-f6c358d45abd,ba48df05-eaa1-4d64-905e-4a4b051e7587,c5b31ab9-0f26-4a49-b8aa-4cc064392f4d,de7f5e83-da88-4046-871f-ea18504b1d43,e592a0c5-5bdb-414c-9066-5dba7cdea370,f861041c-8c9f-4156-acfc-5e6e524f5884
# Accepted kics findings for this file (scanner suppressions; reasons):
#   084c6686: Shield Advanced is an org-level subscription; Shield Standard applies
#   afecd1f1: deletion protection would block terraform destroy / teardown
#   ba2ed23b: s3:GetObject on this module's own artifact bucket only — the instance pulls the CI-built artifact
#   ba48df05: false positive: tags are set with a dynamic tag block (propagate_at_launch)
#   c5b31ab9: MFA delete can only be set by the root user via CLI
#   de7f5e83: HTTP listener only when no certificate_arn is set (internal ALB); with one, HTTP redirects to HTTPS
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   f861041c: access logging needs a log bucket; deferred / adopting team's choice
################################################################################
# EC2 Module — ASG + Launch Template (AMI-based) + ALB + CloudWatch
# Naming: {app_id}-lt, {app_id}-asg, {app_id}-alb, {app_id}-tg, {app_id}-ec2-role
#
# For rehost / relocate / retain: the workload keeps running on EC2 from an AMI
# (an AWS MGN-launched or golden AMI) — no containers. Instances are reached via
# SSM Session Manager (no SSH, no key pair), sit in private subnets behind an ALB,
# and pull build artifacts from the app's artifact bucket (written by CI).
# Encryption: a per-app KMS key (alias/{app_id}-ec2) for the CloudWatch log group.
# ALB: access logs to {app_id}-alb-logs-{account}-{region} (SSE-S3, as ELB requires);
#      HTTPS (TLS 1.3/1.2 policy) + HTTP->HTTPS redirect when certificate_arn is set.
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
variable "ami_id" {
  type        = string
  description = "AMI to launch (AWS MGN-launched / golden AMI of the migrated server), or \"al2023\" for a fresh Amazon Linux 2023 instance bootstrapped with Corretto + the app's systemd unit (no source image). Required before apply."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.ami_id)) || var.ami_id == "al2023"
    error_message = "Set ami_id in terraform.tfvars to the AWS MGN-launched / golden AMI of the migrated server (e.g. ami-0123456789abcdef0), or \"al2023\"."
  }
}
# Only for ami_id = "al2023": the Corretto major installed, and where CodeDeploy puts the jar.
variable "java_version" {
  description = "Java version"
  type        = string
  default     = "21"
}
variable "install_dir" {
  description = "Install dir"
  type        = string
  default     = ""
}
variable "instance_type" {
  description = "Instance type"
  type        = string
  default     = "m6i.large"
}
variable "app_port" {
  description = "App port"
  type        = number
  default     = 8080
}
variable "health_check_path" {
  description = "Health check path"
  type        = string
  default     = "/health"
}
variable "min_size" {
  description = "Min size"
  type        = number
  default     = 2
}
variable "max_size" {
  description = "Max size"
  type        = number
  default     = 4
}
variable "desired_capacity" {
  description = "Desired capacity"
  type        = number
  default     = 2
}
variable "root_volume_size" {
  description = "Root volume size"
  type        = number
  default     = 50
}
variable "user_data" {
  type        = string
  default     = ""
  description = "Optional cloud-init/user-data script (plain text; base64-encoded here)."
}
variable "log_retention_days" {
  description = "Log retention days"
  type        = number
  default     = 30
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
variable "alarm_actions" {
  type        = list(string)
  default     = []
  description = "SNS topic ARNs notified by the CloudWatch alarms."
}
# Not exposed by default (same contract as ecs-fargate): the ALB is INTERNAL
# (private subnets) and accepts traffic only from allowed_ingress_cidrs (the
# generator passes the VPC CIDR). APP-HE 2026-10-14 failed the exposure policy
# with a hardcoded internet-facing ALB open to 0.0.0.0/0.
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
# App env (e.g. TABLE_NAME) — written to /etc/<service_name>.env and loaded into the
# systemd unit via a drop-in, so the AMI's unit needs no change.
variable "environment" {
  description = "Environment"
  type        = map(string)
  default     = {}
}
# Secret env (e.g. the RDS-managed DB password): ENV_NAME => { arn, key } — read from Secrets
# Manager at boot into the same env file (mode 600), as ECS injects `secrets`.
variable "secrets" {
  description = "Secrets"
  type        = map(object({ arn = string, key = string }))
  default     = {}
}
variable "secret_kms_key_arns" {
  description = "Secret KMS key ARNs"
  type        = list(string)
  default     = []
}
variable "service_name" {
  description = "Service name"
  type        = string
  default     = ""
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
variable "tags" {
  description = "Tags"
  type        = map(string)
  default     = {}
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_elb_service_account" "current" {}

data "aws_ssm_parameter" "al2023" {
  count = var.ami_id == "al2023" ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  service_name = var.service_name != "" ? var.service_name : lower(var.app_id)
  install_dir  = var.install_dir != "" ? var.install_dir : "/opt/${lower(var.app_id)}"
  image_id     = var.ami_id == "al2023" ? nonsensitive(data.aws_ssm_parameter.al2023[0].value) : var.ami_id
  corretto     = var.java_version == "8" ? "java-1.8.0-amazon-corretto" : "java-${var.java_version}-amazon-corretto-headless"
  # Fresh AL2023: install the JDK and the unit the CodeDeploy hooks restart (runs the
  # largest (= executable) jar CodeDeploy copied into install_dir, on app_port).
  bootstrap = var.ami_id != "al2023" ? "" : <<-EOT
    dnf install -y ${local.corretto}
    mkdir -p ${local.install_dir}
    cat > /etc/systemd/system/${local.service_name}.service <<'AMF_UNIT'
    [Unit]
    Description=${var.app_id}
    After=network-online.target
    [Service]
    Environment=SERVER_PORT=${var.app_port}
    WorkingDirectory=${local.install_dir}
    ExecStart=/bin/bash -c 'exec java -jar "$(ls -S ${local.install_dir}/*.jar | grep -v -- -plain.jar | head -1)"'
    Restart=on-failure
    [Install]
    WantedBy=multi-user.target
    AMF_UNIT
    systemctl daemon-reload
    systemctl enable ${local.service_name}
  EOT
  env_lines = join("\n", [for k, v in var.environment : "${k}=${v}"])
  secret_lines = join("\n", [for k, s in var.secrets :
    "${k}=$(aws secretsmanager get-secret-value --region ${data.aws_region.current.name} --secret-id '${s.arn}' --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"${s.key}\"])')"
  ])
  env_script = length(var.environment) + length(var.secrets) == 0 ? "" : <<-EOT
    umask 077
    cat > /etc/${local.service_name}.env <<'AMF_ENV'
    ${local.env_lines}
    AMF_ENV
    cat >> /etc/${local.service_name}.env <<AMF_SECRETS
    ${local.secret_lines}
    AMF_SECRETS
    mkdir -p /etc/systemd/system/${local.service_name}.service.d
    printf '[Service]\nEnvironmentFile=/etc/${local.service_name}.env\n' > /etc/systemd/system/${local.service_name}.service.d/amf-env.conf
    systemctl daemon-reload || true
  EOT
  generated  = "${local.bootstrap}${local.env_script}"
  user_data = local.generated == "" ? var.user_data : (
    var.user_data == "" ? "#!/bin/bash\nset -e\n${local.generated}" : "${var.user_data}\n${local.generated}"
  )
}

locals {
  name_prefix = var.app_id
  common_tags = merge(var.tags, {
    Application = var.app_id
    ManagedBy   = "amf-terraform"
  })
  # Must match the CI templates' artifact_bucket ({app_id lowercased}-artifacts-{account}-{region}).
  artifact_bucket = lower("${var.app_id}-artifacts-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}")
  https           = var.certificate_arn != ""
  account_id      = data.aws_caller_identity.current.account_id
  partition       = data.aws_partition.current.partition
  region          = data.aws_region.current.name
  log_group_name  = "/ec2/${var.app_id}"
  # Must start with the lowercase app id: the app permissions boundary scopes S3 to app-*.
  alb_log_bucket = lower("${var.app_id}-alb-logs-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}")
  alb_log_prefix = "alb"
}

# ── Per-app KMS key (CloudWatch logs) ───────────────────────────────────────
# Account root administers the key; CloudWatch Logs may use it only for this
# app's log group (encryption-context condition).
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
  description         = "${var.app_id} EC2 encryption (logs)"
  enable_key_rotation = true
  policy              = data.aws_iam_policy_document.kms.json
  tags                = local.common_tags
}

resource "aws_kms_alias" "app" {
  name          = "alias/${lower(var.app_id)}-ec2"
  target_key_id = aws_kms_key.app.key_id
}

# ── Artifact bucket (CI uploads the built artifact; instances read it) ──────
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

# ── IAM instance profile (SSM + CloudWatch agent + artifact read) ──────────
resource "aws_iam_role" "instance" {
  name                 = "${local.name_prefix}-ec2-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy" "artifacts_read" {
  name = "${local.name_prefix}-ec2-artifacts-read"
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:ListBucket"], Resource = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"] },
    ]
  })
}

resource "aws_iam_role_policy" "secrets" {
  count = length(var.secrets) > 0 ? 1 : 0
  name  = "${local.name_prefix}-ec2-secrets"
  role  = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = distinct([for s in values(var.secrets) : s.arn]) }],
      length(var.secret_kms_key_arns) > 0 ? [{ Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.secret_kms_key_arns }] : []
    )
  })
}

resource "aws_iam_role_policy" "dynamodb" {
  count = length(var.dynamodb_table_arns) > 0 ? 1 : 0
  name  = "${local.name_prefix}-ec2-dynamodb"
  role  = aws_iam_role.instance.id
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

resource "aws_iam_instance_profile" "instance" {
  name = "${local.name_prefix}-ec2-profile"
  role = aws_iam_role.instance.name
  tags = local.common_tags
}

# ── Security groups ─────────────────────────────────────────────────────────
resource "aws_security_group" "alb" {
  #checkov:skip=CKV_AWS_382:outbound to AWS APIs / package repos; no NAT-level filtering in the template
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
    description = "ALB to targets (health checks + forwarded requests)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.common_tags, { Name = "${local.name_prefix}-alb-sg" })
}

resource "aws_security_group" "app" {
  #checkov:skip=CKV_AWS_382:outbound to AWS APIs / package repos; no NAT-level filtering in the template
  name        = "${local.name_prefix}-ec2-sg"
  description = "App instances: app port from the ALB only"
  vpc_id      = var.vpc_id
  ingress {
    description     = "App port from the ALB"
    from_port       = var.app_port
    to_port         = var.app_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    description = "Outbound (package repos, AWS APIs, database)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.common_tags, { Name = "${local.name_prefix}-ec2-sg" })
}

# ── Launch template + ASG ───────────────────────────────────────────────────
resource "aws_launch_template" "app" {
  name                   = "${local.name_prefix}-lt"
  image_id               = local.image_id
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.app.id]
  user_data              = local.user_data != "" ? base64encode(local.user_data) : null

  iam_instance_profile { arn = aws_iam_instance_profile.instance.arn }

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = var.root_volume_size
      volume_type = "gp3"
      encrypted   = true
    }
  }

  monitoring { enabled = true }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.common_tags, { Name = "${local.name_prefix}-ec2" })
  }

  tags = local.common_tags
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
  port        = var.app_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"
  health_check {
    path                = var.health_check_path
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200-399"
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

resource "aws_autoscaling_group" "app" {
  name                      = "${local.name_prefix}-asg"
  min_size                  = var.min_size
  max_size                  = var.max_size
  desired_capacity          = var.desired_capacity
  vpc_zone_identifier       = var.private_subnet_ids
  target_group_arns         = [aws_lb_target_group.app.arn]
  health_check_type         = "ELB"
  health_check_grace_period = 300

  launch_template {
    id      = aws_launch_template.app.id
    version = "$Latest"
  }

  instance_refresh {
    strategy = "Rolling"
    preferences { min_healthy_percentage = 50 }
  }

  dynamic "tag" {
    for_each = local.common_tags
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }
}

# ── CloudWatch: logs + basic alarms ─────────────────────────────────────────
resource "aws_cloudwatch_log_group" "app" {
  #checkov:skip=CKV_AWS_338:retention is a variable (log_retention_days)
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.app.arn
  tags              = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name          = "${local.name_prefix}-cpu-high"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { AutoScalingGroupName = aws_autoscaling_group.app.name }
  alarm_actions       = var.alarm_actions
  tags                = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  alarm_name          = "${local.name_prefix}-unhealthy-hosts"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
  alarm_actions = var.alarm_actions
  tags          = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "target_5xx" {
  alarm_name          = "${local.name_prefix}-target-5xx"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix }
  alarm_actions       = var.alarm_actions
  tags                = local.common_tags
}

# Outputs
output "asg_name" {
  description = "ASG name"
  value       = aws_autoscaling_group.app.name
}
output "launch_template_id" {
  description = "Launch template ID"
  value       = aws_launch_template.app.id
}
output "alb_dns" {
  description = "ALB DNS"
  value       = aws_lb.main.dns_name
}
output "target_group_arn" {
  description = "Target group ARN"
  value       = aws_lb_target_group.app.arn
}
output "target_group_name" {
  description = "Target group name"
  value       = aws_lb_target_group.app.name
}
output "instance_role_name" {
  description = "Instance role name"
  value       = aws_iam_role.instance.name
}
output "app_security_group_id" {
  description = "App security group ID"
  value       = aws_security_group.app.id
}
output "instance_role_arn" {
  description = "Instance role ARN"
  value       = aws_iam_role.instance.arn
}
output "artifact_bucket" {
  description = "Artifact bucket"
  value       = aws_s3_bucket.artifacts.bucket
}
output "kms_key_arn" {
  description = "App KMS key ARN (logs)"
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
