# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
################################################################################
# DynamoDB Module — Table with optional GSI + autoscaling
# Naming: {app_id}-table, alias/{app_id}-dynamodb
# Encryption: a per-app customer-managed KMS key (rotation on). App roles get
# kms:Decrypt/Encrypt/GenerateDataKey via DynamoDB (kms:ViaService) in the compute
# modules' table policies; the key policy delegates to the account's IAM.
################################################################################

variable "app_id" {
  description = "App ID"
  type        = string
}
variable "table_name" {
  description = "Table name"
  type        = string
  default     = ""
}
variable "hash_key" {
  description = "Hash key"
  type        = string
  default     = "PK"
}
variable "range_key" {
  description = "Range key"
  type        = string
  default     = "SK"
}
variable "billing_mode" {
  description = "Billing mode"
  type        = string
  default     = "PAY_PER_REQUEST"
}
variable "enable_pitr" {
  description = "Enable pitr"
  type        = bool
  default     = true
}
variable "ttl_attribute" {
  description = "TTL attribute"
  type        = string
  default     = ""
}
variable "gsi_name" {
  description = "Gsi name"
  type        = string
  default     = ""
}
variable "gsi_hash_key" {
  description = "Gsi hash key"
  type        = string
  default     = ""
}
variable "gsi_range_key" {
  description = "Gsi range key"
  type        = string
  default     = ""
}
variable "tags" {
  description = "Tags"
  type        = map(string)
  default     = {}
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  name_prefix       = var.app_id
  actual_table_name = var.table_name != "" ? var.table_name : "${var.app_id}-table"
  common_tags = merge(var.tags, {
    Application = var.app_id
    ManagedBy   = "amf-terraform"
  })
}

# Account root administers the key; use is limited to this account via DynamoDB.
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
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_kms_key" "table" {
  description         = "${var.app_id} DynamoDB encryption"
  enable_key_rotation = true
  policy              = data.aws_iam_policy_document.kms.json
  tags                = local.common_tags
}

resource "aws_kms_alias" "table" {
  name          = "alias/${lower(var.app_id)}-dynamodb"
  target_key_id = aws_kms_key.table.key_id
}

resource "aws_dynamodb_table" "main" {
  name         = local.actual_table_name
  billing_mode = var.billing_mode
  hash_key     = var.hash_key
  range_key    = var.range_key != "" ? var.range_key : null

  attribute {
    name = var.hash_key
    type = "S"
  }

  dynamic "attribute" {
    for_each = var.range_key != "" ? [var.range_key] : []
    content {
      name = attribute.value
      type = "S"
    }
  }

  dynamic "attribute" {
    for_each = var.gsi_hash_key != "" ? [var.gsi_hash_key] : []
    content {
      name = attribute.value
      type = "S"
    }
  }

  dynamic "attribute" {
    for_each = var.gsi_range_key != "" && var.gsi_range_key != var.hash_key && var.gsi_range_key != var.range_key ? [var.gsi_range_key] : []
    content {
      name = attribute.value
      type = "S"
    }
  }

  dynamic "global_secondary_index" {
    for_each = var.gsi_name != "" ? [1] : []
    content {
      name            = var.gsi_name
      hash_key        = var.gsi_hash_key
      range_key       = var.gsi_range_key != "" ? var.gsi_range_key : null
      projection_type = "ALL"
    }
  }

  dynamic "ttl" {
    for_each = var.ttl_attribute != "" ? [1] : []
    content {
      attribute_name = var.ttl_attribute
      enabled        = true
    }
  }

  point_in_time_recovery { enabled = var.enable_pitr }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.table.arn
  }

  tags = merge(local.common_tags, { Name = local.actual_table_name })
}

# Outputs
output "table_name" {
  description = "Table name"
  value       = aws_dynamodb_table.main.name
}
output "kms_key_arn" {
  description = "Table KMS key ARN"
  value       = aws_kms_key.table.arn
}
output "table_arn" {
  description = "Table ARN"
  value       = aws_dynamodb_table.main.arn
}
