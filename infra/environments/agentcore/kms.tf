# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
################################################################################
# Platform customer-managed KMS key (PCSR security review).
#
# One CMK, with annual rotation, encrypts AMF's own data at rest:
#   • the DynamoDB portfolio table (modules/dynamodb)
#   • the three ECR repositories (modules/ecr)
#   • the ECS, worker and Step Functions CloudWatch log groups
#   • the tool / invoke_runtime Lambda environment variables
#
# The key policy follows the AWS-managed-key pattern: the account root administers
# the key (IAM delegation), CloudWatch Logs may use it only for this account's log
# groups, and any principal in THIS account may use it only through DynamoDB, ECR
# or Lambda in this region (kms:ViaService + kms:CallerAccount). No principal
# outside the account can use it, and no role needs a separate kms:* grant.
################################################################################

variable "kms_deletion_window_days" {
  description = "Waiting period before the platform CMK is deleted after a destroy (7-30 days)."
  type        = number
  default     = 30
}

data "aws_iam_policy_document" "platform_kms" {
  #checkov:skip=CKV_AWS_109:key policies are attached to exactly this key; "*" in a key policy means this key
  #checkov:skip=CKV_AWS_111:key policies are attached to exactly this key; "*" in a key policy means this key
  #checkov:skip=CKV_AWS_356:key policies are attached to exactly this key; "*" in a key policy means this key
  statement {
    sid = "AccountRootAdministers"
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
      identifiers = ["arn:${local.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid       = "CloudWatchLogsThisAccountOnly"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.aws_region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
    }
  }

  statement {
    sid = "AccountPrincipalsViaPlatformServices"
    actions = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey",
    "kms:CreateGrant", "kms:ListGrants", "kms:RetireGrant"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
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
        "dynamodb.${var.aws_region}.amazonaws.com",
        "ecr.${var.aws_region}.amazonaws.com",
        "lambda.${var.aws_region}.amazonaws.com",
      ]
    }
  }
}

resource "aws_kms_key" "platform" {
  description             = "${var.project_prefix} AMF platform data at rest (DynamoDB, ECR, logs, Lambda env)"
  enable_key_rotation     = true
  deletion_window_in_days = var.kms_deletion_window_days
  policy                  = data.aws_iam_policy_document.platform_kms.json
  tags                    = { project = var.project_prefix, managed_by = "terraform" }
}

resource "aws_kms_alias" "platform" {
  name          = "alias/${var.project_prefix}-platform"
  target_key_id = aws_kms_key.platform.key_id
}

output "platform_kms_key_arn" {
  description = "Platform CMK ARN"
  value       = aws_kms_key.platform.arn
}
