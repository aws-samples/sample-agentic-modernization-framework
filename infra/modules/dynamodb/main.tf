# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
# DynamoDB single-table for the migration portfolio
# TTL enabled for automatic cleanup of assessment records

variable "project_prefix" {
  description = "Project prefix"
  type        = string
}
variable "table_name" {
  description = "Table name"
  type        = string
  default     = ""
}
variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for encryption at rest."
  type        = string
}
variable "ttl_attribute" {
  description = "TTL attribute"
  type        = string
  default     = "ttl"
}

resource "aws_dynamodb_table" "portfolio" {
  name         = var.table_name != "" ? var.table_name : "${var.project_prefix}_portfolio"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  global_secondary_index {
    name            = "GSI1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = var.ttl_attribute
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  tags = {
    project    = var.project_prefix
    managed_by = "terraform"
  }
}

output "table_name" {
  description = "Table name"
  value       = aws_dynamodb_table.portfolio.name
}
output "table_arn" {
  description = "Table ARN"
  value       = aws_dynamodb_table.portfolio.arn
}
