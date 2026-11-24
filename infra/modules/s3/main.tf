# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370,f861041c-8c9f-4156-acfc-5e6e524f5884
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   f861041c: access logging needs a log bucket; deferred / adopting team's choice
variable "project_prefix" {
  description = "Project prefix"
  type        = string
}
variable "aws_account_id" {
  description = "Aws account ID"
  type        = string
}
variable "aws_region" {
  description = "Aws region"
  type        = string
}

locals {
  bucket_suffix = "${var.aws_account_id}-${var.aws_region}"
}

# Single data bucket (assessments, transformations, design docs, discovery uploads, notes)
resource "aws_s3_bucket" "atx_output" {
  #checkov:skip=CKV2_AWS_62:no consumer for S3 event notifications
  #checkov:skip=CKV_AWS_144:single-region demo; DR replication not in scope
  #checkov:skip=CKV_AWS_18:access logging deferred to a later hardening phase
  bucket        = "${var.project_prefix}-data-${local.bucket_suffix}"
  force_destroy = true
  tags          = { project = var.project_prefix, managed_by = "terraform" }
}

resource "aws_s3_bucket_versioning" "atx_output" {
  bucket = aws_s3_bucket.atx_output.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "atx_output" {
  bucket = aws_s3_bucket.atx_output.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
  }
}

resource "aws_s3_bucket_public_access_block" "atx_output" {
  bucket                  = aws_s3_bucket.atx_output.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# TLS only: deny any request that doesn't use HTTPS (aws:SecureTransport = false).
resource "aws_s3_bucket_policy" "atx_output" {
  bucket = aws_s3_bucket.atx_output.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.atx_output.arn, "${aws_s3_bucket.atx_output.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.atx_output]
}

resource "aws_s3_bucket_lifecycle_configuration" "atx_output" {
  bucket = aws_s3_bucket.atx_output.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {} # Apply to all objects

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

output "atx_output_bucket_name" {
  description = "ATX output bucket name"
  value       = aws_s3_bucket.atx_output.bucket
}
output "atx_output_bucket_arn" {
  description = "ATX output bucket ARN"
  value       = aws_s3_bucket.atx_output.arn
}
output "connector_uploads_bucket_name" {
  description = "Connector uploads bucket name"
  value       = aws_s3_bucket.atx_output.bucket
}
output "connector_uploads_bucket_arn" {
  description = "Connector uploads bucket ARN"
  value       = aws_s3_bucket.atx_output.arn
}
