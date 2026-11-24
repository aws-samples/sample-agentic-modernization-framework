# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370,f861041c-8c9f-4156-acfc-5e6e524f5884
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   f861041c: access logging needs a log bucket; deferred / adopting team's choice
################################################################################
# S3 Module — Bucket with encryption, versioning, lifecycle
# Naming: {app_id}-{purpose}-{account_id}-{region}
################################################################################

variable "app_id" {
  description = "App ID"
  type        = string
}
variable "purpose" {
  description = "Purpose"
  type        = string
  default     = "data"
}
variable "enable_versioning" {
  description = "Enable versioning"
  type        = bool
  default     = true
}
variable "enable_lifecycle" {
  description = "Enable lifecycle"
  type        = bool
  default     = true
}
variable "lifecycle_days" {
  description = "Lifecycle days"
  type        = number
  default     = 90
}
variable "abort_incomplete_upload_days" {
  description = "Days before an unfinished multipart upload is aborted"
  type        = number
  default     = 7
}
variable "tags" {
  description = "Tags"
  type        = map(string)
  default     = {}
}

locals {
  common_tags = merge(var.tags, {
    Application = var.app_id
    ManagedBy   = "amf-terraform"
  })
  bucket_name = "${lower(var.app_id)}-${var.purpose}-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_s3_bucket" "main" {
  #checkov:skip=CKV2_AWS_62:no consumer for S3 event notifications
  #checkov:skip=CKV_AWS_144:cross-region replication is the adopting team's DR choice
  #checkov:skip=CKV_AWS_145:AWS-managed encryption; a customer-managed KMS key is the adopting team's choice
  #checkov:skip=CKV_AWS_18:access logging needs a log bucket the adopting team owns
  bucket = local.bucket_name
  tags   = merge(local.common_tags, { Name = local.bucket_name })
}

resource "aws_s3_bucket_versioning" "main" {
  bucket = aws_s3_bucket.main.id
  versioning_configuration { status = var.enable_versioning ? "Enabled" : "Suspended" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "main" {
  bucket = aws_s3_bucket.main.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "main" {
  bucket                  = aws_s3_bucket.main.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "main" {
  count  = var.enable_lifecycle ? 1 : 0
  bucket = aws_s3_bucket.main.id

  rule {
    id     = "transition-to-ia"
    status = "Enabled"
    transition {
      days          = var.lifecycle_days
      storage_class = "STANDARD_IA"
    }
    noncurrent_version_expiration { noncurrent_days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = var.abort_incomplete_upload_days }
  }
}

# Outputs
output "bucket_name" {
  description = "Bucket name"
  value       = aws_s3_bucket.main.id
}
output "bucket_arn" {
  description = "Bucket ARN"
  value       = aws_s3_bucket.main.arn
}
output "bucket_domain" {
  description = "Bucket domain"
  value       = aws_s3_bucket.main.bucket_regional_domain_name
}
