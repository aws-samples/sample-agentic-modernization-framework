# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
################################################################################
# RDS Module — RDS instance or Aurora cluster, KMS-encrypted, Secrets Manager creds
# Naming: {app_id}-db, {app_id}-db-subnets, {app_id}-db-sg, alias/{app_id}-db
#
# engine (AMF target_database value):
#   rds_postgresql | rds_mysql                   → aws_db_instance
#   aurora_postgresql | aurora_mysql             → aws_rds_cluster (+ 1 or 2 instances)
# Credentials: RDS-managed master password in Secrets Manager
# (manage_master_user_password) — no password in state or tfvars.
################################################################################

variable "app_id" {
  description = "App ID"
  type        = string
}
variable "engine" {
  description = "Engine"
  type        = string
  validation {
    condition     = contains(["rds_postgresql", "rds_mysql", "aurora_postgresql", "aurora_mysql"], var.engine)
    error_message = "engine must be one of rds_postgresql, rds_mysql, aurora_postgresql, aurora_mysql."
  }
}
variable "engine_version" {
  type        = string
  default     = ""
  description = "Empty = the engine's current default version."
}
variable "instance_class" {
  type        = string
  description = "DB instance class (e.g. db.r6g.large). Not derivable from discovery — set before apply."
  validation {
    condition     = can(regex("^db\\.", var.instance_class))
    error_message = "Set db_instance_class in terraform.tfvars (e.g. db.r6g.large) — sizing is a design decision, not a discovered value."
  }
}
variable "multi_az" {
  description = "Multi AZ"
  type        = bool
  default     = true
}
variable "allocated_storage" {
  description = "Allocated storage"
  type        = number
  default     = 100
}
variable "backup_retention_days" {
  description = "Backup retention days"
  type        = number
  default     = 14
}
variable "deletion_protection" {
  description = "Deletion protection"
  type        = bool
  default     = true
}
# Initial database created on the instance/cluster ("" = none, the engine default only).
variable "db_name" {
  description = "DB name"
  type        = string
  default     = ""
}
variable "vpc_id" {
  description = "VPC ID"
  type        = string
}
variable "db_subnet_ids" {
  description = "DB subnet IDs"
  type        = list(string)
}
variable "allowed_cidr_blocks" {
  description = "Allowed CIDR blocks"
  type        = list(string)
  default     = []
}
variable "allowed_security_group_ids" {
  description = "Allowed security group IDs"
  type        = list(string)
  default     = []
}
variable "tags" {
  description = "Tags"
  type        = map(string)
  default     = {}
}

locals {
  name_prefix = var.app_id
  common_tags = merge(var.tags, {
    Application = var.app_id
    ManagedBy   = "amf-terraform"
  })
  is_aurora = startswith(var.engine, "aurora_")
  # Engine logs exported to CloudWatch Logs.
  log_exports = (endswith(var.engine, "postgresql")
    ? (local.is_aurora ? ["postgresql"] : ["postgresql", "upgrade"])
  : ["error", "general", "slowquery"])
  engine_name = {
    rds_postgresql    = "postgres"
    rds_mysql         = "mysql"
    aurora_postgresql = "aurora-postgresql"
    aurora_mysql      = "aurora-mysql"
  }[var.engine]
  port = {
    rds_postgresql    = 5432
    rds_mysql         = 3306
    aurora_postgresql = 5432
    aurora_mysql      = 3306
  }[var.engine]
  identifier = lower("${var.app_id}-db")
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

# Explicit key policy (no wildcard principal): account root administers the key;
# use is limited to this account's RDS + Secrets Manager (master user secret).
data "aws_iam_policy_document" "db_kms" {
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
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
  statement {
    sid    = "AllowRdsAndSecretsManagerUse"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:CreateGrant",
      "kms:ListGrants",
      "kms:DescribeKey",
    ]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values = [
        "rds.${data.aws_region.current.name}.amazonaws.com",
        "secretsmanager.${data.aws_region.current.name}.amazonaws.com",
      ]
    }
  }
}

resource "aws_kms_key" "db" {
  description         = "${var.app_id} database encryption"
  enable_key_rotation = true
  policy              = data.aws_iam_policy_document.db_kms.json
  tags                = local.common_tags
}

resource "aws_kms_alias" "db" {
  name          = "alias/${lower(var.app_id)}-db"
  target_key_id = aws_kms_key.db.key_id
}

resource "aws_db_subnet_group" "db" {
  name       = "${local.identifier}-subnets"
  subnet_ids = var.db_subnet_ids
  tags       = local.common_tags
}

resource "aws_security_group" "db" {
  #checkov:skip=CKV_AWS_382:outbound to AWS APIs / package repos; no NAT-level filtering in the template
  name        = "${local.name_prefix}-db-sg"
  description = "Database port from the app CIDRs / security groups only"
  vpc_id      = var.vpc_id
  ingress {
    description     = "Database port from the app"
    from_port       = local.port
    to_port         = local.port
    protocol        = "tcp"
    cidr_blocks     = var.allowed_cidr_blocks
    security_groups = var.allowed_security_group_ids
  }
  egress {
    description = "Outbound (AWS APIs for managed features)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.common_tags, { Name = "${local.name_prefix}-db-sg" })
}

# ── RDS instance (non-Aurora engines) ───────────────────────────────────────
# Logging IS on: enabled_cloudwatch_logs_exports = local.log_exports (engine-dependent:
# postgresql+upgrade | error+general+slowquery). The rule only recognises a literal list.
# nosemgrep: terraform.aws.security.aws-db-instance-no-logging.aws-db-instance-no-logging
resource "aws_db_instance" "db" {
  #checkov:skip=CKV_AWS_118:enhanced monitoring needs a monitoring role; Performance Insights is on
  #checkov:skip=CKV_AWS_354:Performance Insights keeps the aws/rds key: its KMS key cannot change once enabled (existing instances)
  count                           = local.is_aurora ? 0 : 1
  identifier                      = local.identifier
  engine                          = local.engine_name
  engine_version                  = var.engine_version != "" ? var.engine_version : null
  instance_class                  = var.instance_class
  allocated_storage               = var.allocated_storage
  max_allocated_storage           = var.allocated_storage * 2
  storage_type                    = "gp3"
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.db.arn
  db_name                         = var.db_name != "" ? var.db_name : null
  username                        = "dbadmin"
  manage_master_user_password     = true
  master_user_secret_kms_key_id   = aws_kms_key.db.key_id
  multi_az                        = var.multi_az
  db_subnet_group_name            = aws_db_subnet_group.db.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  port                            = local.port
  publicly_accessible             = false
  backup_retention_period         = var.backup_retention_days
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${local.identifier}-final"
  auto_minor_version_upgrade      = true
  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = local.log_exports
  tags                            = local.common_tags
}

# ── Aurora cluster (aurora_* engines) ───────────────────────────────────────
resource "aws_rds_cluster" "db" {
  #checkov:skip=CKV2_AWS_8:automated RDS backups (backup_retention_days) cover it; AWS Backup plans are org-level
  count                               = local.is_aurora ? 1 : 0
  cluster_identifier                  = local.identifier
  engine                              = local.engine_name
  engine_version                      = var.engine_version != "" ? var.engine_version : null
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.db.arn
  database_name                       = var.db_name != "" ? var.db_name : null
  master_username                     = "dbadmin"
  manage_master_user_password         = true
  master_user_secret_kms_key_id       = aws_kms_key.db.key_id
  db_subnet_group_name                = aws_db_subnet_group.db.name
  vpc_security_group_ids              = [aws_security_group.db.id]
  port                                = local.port
  backup_retention_period             = var.backup_retention_days
  copy_tags_to_snapshot               = true
  deletion_protection                 = var.deletion_protection
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.identifier}-final"
  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = local.log_exports
  tags                                = local.common_tags
}

# Multi-AZ for Aurora = a reader instance in a second AZ (failover target).
resource "aws_rds_cluster_instance" "db" {
  #checkov:skip=CKV_AWS_118:enhanced monitoring needs a monitoring role; Performance Insights is on
  #checkov:skip=CKV_AWS_354:Performance Insights keeps the aws/rds key: its KMS key cannot change once enabled (existing instances)
  count                        = local.is_aurora ? (var.multi_az ? 2 : 1) : 0
  identifier                   = "${local.identifier}-${count.index + 1}"
  cluster_identifier           = aws_rds_cluster.db[0].id
  engine                       = local.engine_name
  instance_class               = var.instance_class
  db_subnet_group_name         = aws_db_subnet_group.db.name
  publicly_accessible          = false
  performance_insights_enabled = true
  auto_minor_version_upgrade   = true
  tags                         = local.common_tags
}

# Outputs
output "endpoint" {
  description = "Endpoint"
  value       = local.is_aurora ? aws_rds_cluster.db[0].endpoint : aws_db_instance.db[0].address
}
output "port" {
  description = "Port"
  value       = local.port
}
output "db_name" {
  description = "DB name"
  value       = var.db_name
}
output "username" {
  description = "Username"
  value       = "dbadmin"
}
output "master_user_secret_arn" {
  description = "Master user secret ARN"
  value       = local.is_aurora ? aws_rds_cluster.db[0].master_user_secret[0].secret_arn : aws_db_instance.db[0].master_user_secret[0].secret_arn
}
output "kms_key_arn" {
  description = "KMS key ARN"
  value       = aws_kms_key.db.arn
}
output "security_group_id" {
  description = "Security group ID"
  value       = aws_security_group.db.id
}
