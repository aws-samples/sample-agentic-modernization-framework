# kics-scan disable=69e7c320-b65d-41bb-be02-d63ecc0bcc9d,d1846b12-20c5-4d45-8798-fc35b79268eb,e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   69e7c320: access is granted by IAM in the same account; no cross-account repository policy needed
#   d1846b12: only the moving :latest tag is excluded from immutability (setup.sh); every other tag is immutable
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
variable "project_prefix" {
  description = "Project prefix"
  type        = string
}
variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for encryption at rest."
  type        = string
}

# ECR for the main FastAPI app
# nosemgrep: terraform.aws.security.aws-ecr-mutable-image-tags.aws-ecr-mutable-image-tags
resource "aws_ecr_repository" "api" { # IMMUTABLE_WITH_EXCLUSION: only :latest moves
  #checkov:skip=CKV_AWS_51:every tag but the moving :latest (setup.sh) is immutable (IMMUTABLE_WITH_EXCLUSION)
  name                 = "${var.project_prefix}-api"
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"
  image_tag_mutability_exclusion_filter {
    filter      = "latest"
    filter_type = "WILDCARD"
  }
  force_delete = true
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = { project = var.project_prefix, managed_by = "terraform" }
}

# ECR for the ATX code analysis runner
# nosemgrep: terraform.aws.security.aws-ecr-mutable-image-tags.aws-ecr-mutable-image-tags
resource "aws_ecr_repository" "atx_runner" { # IMMUTABLE_WITH_EXCLUSION: only :latest moves
  #checkov:skip=CKV_AWS_51:every tag but the moving :latest (setup.sh) is immutable (IMMUTABLE_WITH_EXCLUSION)
  name                 = "${var.project_prefix}-atx-runner"
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"
  image_tag_mutability_exclusion_filter {
    filter      = "latest"
    filter_type = "WILDCARD"
  }
  force_delete = true
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = { project = var.project_prefix, managed_by = "terraform" }
}

# ECR for the migration worker (assessment + workbench + design)
# nosemgrep: terraform.aws.security.aws-ecr-mutable-image-tags.aws-ecr-mutable-image-tags
resource "aws_ecr_repository" "migration_worker" { # IMMUTABLE_WITH_EXCLUSION: only :latest moves
  #checkov:skip=CKV_AWS_51:every tag but the moving :latest (setup.sh) is immutable (IMMUTABLE_WITH_EXCLUSION)
  name                 = "${var.project_prefix}-worker"
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"
  image_tag_mutability_exclusion_filter {
    filter      = "latest"
    filter_type = "WILDCARD"
  }
  force_delete = true
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = { project = var.project_prefix, managed_by = "terraform" }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "atx_runner" {
  repository = aws_ecr_repository.atx_runner.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "migration_worker" {
  repository = aws_ecr_repository.migration_worker.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

output "api_repo_url" {
  description = "API repo URL"
  value       = aws_ecr_repository.api.repository_url
}
output "atx_runner_repo_url" {
  description = "ATX runner repo URL"
  value       = aws_ecr_repository.atx_runner.repository_url
}
output "assessment_worker_repo_url" {
  description = "Assessment worker repo URL"
  value       = aws_ecr_repository.migration_worker.repository_url
}
