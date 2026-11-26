# kics-scan disable=ba2ed23b-52d3-45ca-be25-f6c358d45abd,e592a0c5-5bdb-414c-9066-5dba7cdea370,f861041c-8c9f-4156-acfc-5e6e524f5884
# Accepted kics findings for this file (scanner suppressions; reasons):
#   ba2ed23b: s3:GetObject* on this module's own artifact bucket only — the CodeDeploy agent fetches the revision
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   f861041c: access logging needs a log bucket; deferred / adopting team's choice
################################################################################
# CodePipeline Module — CodeCommit → CodeBuild → ECS | EC2 (CodeDeploy) | Lambda deploy
# Naming: {app_id}-pipeline, {app_id}-build, {app_id}-pipeline-artifacts,
#         {app_id}-codedeploy / {app_id}-dg (EC2)
#
# Source : AWS CodeCommit repo `repo_name` @ `branch` (native, EventBridge-triggered)
# Build  : CodeBuild runs the committed buildspec (var.buildspec, default
#          cicd/buildspec.yml): tests + coverage gate, then
#            ecs: builds/pushes the image to ECR, emits imagedefinitions.json
#            ec2: packages a CodeDeploy revision (appspec.yml + scripts/ + app/)
#            lambda: uploads the function package to `app_artifact_bucket`, then
#                    deploys it itself (update-function-code --publish + smoke
#                    invoke) — the pipeline has no separate Deploy stage
# Deploy : ecs: ECS "Standard" deploy action on `ecs_service_name`
#          ec2: CodeDeploy in-place, one instance at a time, behind the ALB target
#               group (traffic drained per instance), auto-rollback on failure.
#               The CodeDeploy agent is installed on the ASG's instances by an SSM
#               association (works for any MGN-launched / golden AMI with the SSM agent).
# Encryption: a per-app KMS key (alias/{app_id}-pipeline) for the pipeline artifact
#             store, the artifact bucket default SSE and CodeBuild's output artifacts.
################################################################################

variable "app_id" {
  description = "App ID"
  type        = string
}
variable "repo_name" {
  description = "CodeCommit repository name"
  type        = string
}
variable "branch" {
  description = "Branch"
  type        = string
  default     = "main"
}
# Start the pipeline on every push to `branch`. Off when `branch` is the migration
# branch AMF deploys from: only the human deploy gate may start a run there.
variable "trigger_on_push" {
  description = "Trigger on push"
  type        = bool
  default     = true
}
variable "build_arch" {
  description = "X86_64 | ARM64 — must match the ECS task cpu_architecture (docker builds natively)"
  type        = string
  default     = "X86_64"
  validation {
    condition     = contains(["X86_64", "ARM64"], var.build_arch)
    error_message = "build_arch must be X86_64 or ARM64."
  }
}
variable "deploy_target" {
  description = "ecs | ec2 | lambda"
  type        = string
  default     = "ecs"
  validation {
    condition     = contains(["ecs", "ec2", "lambda"], var.deploy_target)
    error_message = "deploy_target must be ecs, ec2 or lambda."
  }
}

# ── lambda ──
variable "function_name" {
  description = "Function name"
  type        = string
  default     = ""
}
variable "function_names" {
  description = "Every function of a multi-function app; [] = function_name only"
  type        = list(string)
  default     = []
}

# ── ecs ──
variable "ecr_repo_url" {
  description = "E.g. 123.dkr.ecr.us-east-1.amazonaws.com/app-he"
  type        = string
  default     = ""
}
variable "ecs_cluster_name" {
  description = "ECS cluster name"
  type        = string
  default     = ""
}
variable "ecs_service_name" {
  description = "ECS service name"
  type        = string
  default     = ""
}
variable "container_name" {
  description = "Container name"
  type        = string
  default     = "app"
}

# ── ec2 (outputs of the ec2 module) ──
variable "asg_name" {
  description = "ASG name"
  type        = string
  default     = ""
}
variable "target_group_name" {
  description = "Target group name"
  type        = string
  default     = ""
}
variable "instance_role_name" {
  description = "EC2 instance role — granted read on the pipeline artifact bucket (CodeDeploy agent downloads the revision)."
  type        = string
  default     = ""
}
variable "app_artifact_bucket" {
  description = "The ec2 / lambda module's artifact bucket — CodeBuild also publishes the build there."
  type        = string
  default     = ""
}
# Path (relative to the source root) of the buildspec committed by generate_cicd.
# Single source of truth: the CodeBuild project runs the COMMITTED buildspec
# (unit tests + coverage gate + image build), not an inline copy.
variable "buildspec" {
  description = "Buildspec"
  type        = string
  default     = "cicd/buildspec.yml"
}
variable "permissions_boundary_arn" {
  description = "AMF app permissions boundary; required: the deploy role refuses roles without it"
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

# ── Per-app KMS key (pipeline artifacts) ───────────────────────────────
# Account root administers the key; the CodeBuild / CodePipeline / EC2 instance
# roles use it through their own IAM policies below (capped by the app
# permissions boundary).
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

resource "aws_kms_key" "artifacts" {
  description         = "${var.app_id} pipeline artifact encryption"
  enable_key_rotation = true
  policy              = data.aws_iam_policy_document.kms.json
  tags                = local.common_tags
}

resource "aws_kms_alias" "artifacts" {
  name          = "alias/${lower(var.app_id)}-pipeline"
  target_key_id = aws_kms_key.artifacts.key_id
}

locals {
  name        = var.app_id
  ecs         = var.deploy_target == "ecs"
  ec2         = var.deploy_target == "ec2"
  lambda      = var.deploy_target == "lambda"
  fn_names    = length(var.function_names) > 0 ? var.function_names : [var.function_name]
  fn_arns     = flatten([for n in local.fn_names : ["arn:aws:lambda:${local.region}:${local.account}:function:${n}", "arn:aws:lambda:${local.region}:${local.account}:function:${n}:*"]])
  region      = data.aws_region.current.name
  account     = data.aws_caller_identity.current.account_id
  cc_arn      = "arn:aws:codecommit:${local.region}:${local.account}:${var.repo_name}"
  ecr_arn     = local.ecs ? "arn:aws:ecr:${local.region}:${local.account}:repository/${element(split("/", var.ecr_repo_url), 1)}" : ""
  common_tags = merge(var.tags, { Application = var.app_id, ManagedBy = "amf-terraform" })
  kms_use     = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]

  codebuild_statements = concat(
    [
      # CodeBuild writes to /aws/codebuild/<project name>; the log stream ARNs sit under the group (":*").
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = [
        "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/codebuild/${local.name}-build",
        "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/codebuild/${local.name}-build:*",
      ] },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:GetBucketLocation"], Resource = ["${aws_s3_bucket.artifacts.arn}", "${aws_s3_bucket.artifacts.arn}/*"] },
      { Effect = "Allow", Action = local.kms_use, Resource = aws_kms_key.artifacts.arn },
      # The buildspec's `reports:` section (JUnit tests / Cobertura coverage) needs its report group.
      { Effect = "Allow", Action = ["codebuild:CreateReportGroup", "codebuild:CreateReport", "codebuild:UpdateReport", "codebuild:BatchPutTestCases", "codebuild:BatchPutCodeCoverages"], Resource = "arn:aws:codebuild:${local.region}:${local.account}:report-group/${local.name}-build-*" },
    ],
    local.ecs ? [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], Resource = local.ecr_arn },
    ] : [],
    !local.ecs && var.app_artifact_bucket != "" ? [
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "arn:aws:s3:::${var.app_artifact_bucket}/*" },
    ] : [],
    local.lambda ? [
      { Effect = "Allow", Action = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:PublishVersion", "lambda:InvokeFunction"], Resource = local.fn_arns },
    ] : [],
  )

  # Statements are built as JSON strings so both branches are list(string): a
  # conditional over differently-shaped objects (one carries a Condition block)
  # fails `terraform validate` with "Inconsistent conditional result types".
  deploy_statements_json = local.lambda ? [] : local.ecs ? [
    # Service actions on this app's cluster/service only.
    jsonencode({ Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:UpdateService"], Resource = "arn:aws:ecs:${local.region}:${local.account}:service/${var.ecs_cluster_name}/${var.ecs_service_name}" }),
    jsonencode({ Effect = "Allow", Action = ["ecs:DescribeTasks", "ecs:ListTasks"], Resource = ["arn:aws:ecs:${local.region}:${local.account}:task/${var.ecs_cluster_name}/*", "arn:aws:ecs:${local.region}:${local.account}:container-instance/${var.ecs_cluster_name}/*"] }),
    # The deploy action re-registers (and tags) this app's task definition family.
    jsonencode({ Effect = "Allow", Action = ["ecs:TagResource"], Resource = "arn:aws:ecs:${local.region}:${local.account}:task-definition/${local.name}-task:*" }),
    # RegisterTaskDefinition / DescribeTaskDefinition have no resource-level permissions (Service Authorization Reference).
    jsonencode({ Effect = "Allow", Action = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"], Resource = "*" }),
    # Only the app's own task + execution roles (ecs-fargate module names), only to ECS tasks.
    jsonencode({ Effect = "Allow", Action = ["iam:PassRole"], Resource = ["arn:aws:iam::${local.account}:role/${local.name}-ecs-task-role", "arn:aws:iam::${local.account}:role/${local.name}-ecs-exec-role"], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } }),
    ] : [
    jsonencode({ Effect = "Allow", Action = ["codedeploy:CreateDeployment", "codedeploy:GetDeployment"], Resource = "arn:aws:codedeploy:${local.region}:${local.account}:deploymentgroup:${local.name}-codedeploy/${local.name}-dg" }),
    jsonencode({ Effect = "Allow", Action = ["codedeploy:GetApplication", "codedeploy:GetApplicationRevision", "codedeploy:RegisterApplicationRevision"], Resource = "arn:aws:codedeploy:${local.region}:${local.account}:application:${local.name}-codedeploy" }),
    jsonencode({ Effect = "Allow", Action = ["codedeploy:GetDeploymentConfig"], Resource = "arn:aws:codedeploy:${local.region}:${local.account}:deploymentconfig:*" }),
  ]
  deploy_statements = [for st in local.deploy_statements_json : jsondecode(st)]
}

# ── Artifact bucket ────────────────────────────────────────────────────
resource "aws_s3_bucket" "artifacts" {
  #checkov:skip=CKV2_AWS_61:artifact retention is the adopting team's choice
  #checkov:skip=CKV2_AWS_62:no consumer for S3 event notifications
  #checkov:skip=CKV_AWS_144:cross-region replication is the adopting team's DR choice
  #checkov:skip=CKV_AWS_18:access logging needs a log bucket the adopting team owns
  #checkov:skip=CKV_AWS_21:artifact bucket: objects are rebuilt each pipeline run
  bucket        = lower("${local.name}-pipeline-artifacts-${data.aws_caller_identity.current.account_id}") # S3 names must be lowercase
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

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.artifacts.arn
    }
    bucket_key_enabled = true
  }
}

# ── CodeBuild ──────────────────────────────────────────────────────────
resource "aws_iam_role" "codebuild" {
  name                 = "${local.name}-codebuild-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "codebuild" {
  name = "${local.name}-codebuild-policy"
  role = aws_iam_role.codebuild.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.codebuild_statements
  })
}

resource "aws_codebuild_project" "build" {
  #checkov:skip=CKV_AWS_314:CodeBuild logs to CloudWatch by default
  name           = "${local.name}-build"
  service_role   = aws_iam_role.codebuild.arn
  encryption_key = aws_kms_key.artifacts.arn

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = var.build_arch == "ARM64" ? "aws/codebuild/amazonlinux2-aarch64-standard:3.0" : "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = var.build_arch == "ARM64" ? "ARM_CONTAINER" : "LINUX_CONTAINER"
    privileged_mode = local.ecs # docker build (ecs only)
    environment_variable {
      name  = "AWS_DEFAULT_REGION"
      value = data.aws_region.current.name
    }
    environment_variable {
      name  = "ECR_REPO_URL"
      value = var.ecr_repo_url
    }
    environment_variable {
      name  = "CONTAINER_NAME"
      value = var.container_name
    }
    environment_variable {
      name  = "FUNCTION_NAME"
      value = var.function_name
    }
    environment_variable {
      name  = "FUNCTION_NAMES" # space-separated; the buildspec deploys each
      value = join(" ", local.fn_names)
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = var.buildspec
  }

  tags = local.common_tags
}

# ── CodePipeline ───────────────────────────────────────────────────────
resource "aws_iam_role" "pipeline" {
  name                 = "${local.name}-pipeline-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "codepipeline.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "pipeline" {
  name = "${local.name}-pipeline-policy"
  role = aws_iam_role.pipeline.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:GetBucketLocation", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.artifacts.arn}", "${aws_s3_bucket.artifacts.arn}/*"] },
      { Effect = "Allow", Action = local.kms_use, Resource = aws_kms_key.artifacts.arn },
      { Effect = "Allow", Action = ["codecommit:GetBranch", "codecommit:GetCommit", "codecommit:UploadArchive", "codecommit:GetUploadArchiveStatus", "codecommit:CancelUploadArchive"], Resource = local.cc_arn },
      { Effect = "Allow", Action = ["codebuild:BatchGetBuilds", "codebuild:StartBuild"], Resource = aws_codebuild_project.build.arn },
    ], local.deploy_statements)
  })
}

resource "aws_codepipeline" "this" {
  name     = "${local.name}-pipeline"
  role_arn = aws_iam_role.pipeline.arn
  # CodePipeline starts a run on creation: without this the first run races the
  # inline policies and fails Source with codecommit:GetBranch denied (APP-CLI).
  depends_on = [aws_iam_role_policy.pipeline, aws_iam_role_policy.codebuild]

  artifact_store {
    type     = "S3"
    location = aws_s3_bucket.artifacts.bucket
    encryption_key {
      id   = aws_kms_key.artifacts.arn
      type = "KMS"
    }
  }

  stage {
    name = "Source"
    action {
      name             = "Source"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeCommit"
      version          = "1"
      output_artifacts = ["source_output"]
      configuration = {
        RepositoryName       = var.repo_name
        BranchName           = var.branch
        PollForSourceChanges = "false" # EventBridge triggers instead
      }
    }
  }

  stage {
    name = "Build"
    action {
      name             = "Build"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["source_output"]
      output_artifacts = ["build_output"]
      configuration    = { ProjectName = aws_codebuild_project.build.name }
    }
  }

  # Lambda deploys from the Build stage (update-function-code), so no Deploy stage.
  dynamic "stage" {
    for_each = local.lambda ? [] : [1]
    content {
      name = "Deploy"
      dynamic "action" {
        for_each = local.ecs ? [1] : []
        content {
          name            = "Deploy"
          category        = "Deploy"
          owner           = "AWS"
          provider        = "ECS"
          version         = "1"
          input_artifacts = ["build_output"]
          configuration = {
            ClusterName = var.ecs_cluster_name
            ServiceName = var.ecs_service_name
            FileName    = "imagedefinitions.json"
          }
        }
      }
      dynamic "action" {
        for_each = local.ec2 ? [1] : []
        content {
          name            = "Deploy"
          category        = "Deploy"
          owner           = "AWS"
          provider        = "CodeDeploy"
          version         = "1"
          input_artifacts = ["build_output"]
          configuration = {
            ApplicationName     = aws_codedeploy_app.app[0].name
            DeploymentGroupName = aws_codedeploy_deployment_group.app[0].deployment_group_name
          }
        }
      }
    }
  }

  tags = local.common_tags
}

# ── EventBridge trigger (CodeCommit push → start pipeline) ──────────────
resource "aws_iam_role" "events" {
  name                 = "${local.name}-pipeline-events-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "events.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "events" {
  name = "${local.name}-pipeline-events-policy"
  role = aws_iam_role.events.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["codepipeline:StartPipelineExecution"], Resource = aws_codepipeline.this.arn }]
  })
}

resource "aws_cloudwatch_event_rule" "cc_push" {
  name        = "${local.name}-cc-push"
  description = "Trigger ${local.name}-pipeline on push to ${var.repo_name}/${var.branch}"
  state       = var.trigger_on_push ? "ENABLED" : "DISABLED"
  event_pattern = jsonencode({
    source      = ["aws.codecommit"]
    detail-type = ["CodeCommit Repository State Change"]
    resources   = [local.cc_arn]
    detail = {
      event         = ["referenceCreated", "referenceUpdated"]
      referenceType = ["branch"]
      referenceName = [var.branch]
    }
  })
  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "pipeline" {
  rule     = aws_cloudwatch_event_rule.cc_push.name
  arn      = aws_codepipeline.this.arn
  role_arn = aws_iam_role.events.arn
}

# ── EC2: CodeDeploy (in-place, rolling, behind the ALB) ─────────────────
resource "aws_iam_role" "codedeploy" {
  count                = local.ec2 ? 1 : 0
  name                 = "${local.name}-codedeploy-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "codedeploy.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "codedeploy" {
  count      = local.ec2 ? 1 : 0
  role       = aws_iam_role.codedeploy[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSCodeDeployRole"
}

resource "aws_codedeploy_app" "app" {
  count            = local.ec2 ? 1 : 0
  name             = "${local.name}-codedeploy"
  compute_platform = "Server"
  tags             = local.common_tags
}

resource "aws_codedeploy_deployment_group" "app" {
  count                  = local.ec2 ? 1 : 0
  app_name               = aws_codedeploy_app.app[0].name
  deployment_group_name  = "${local.name}-dg"
  service_role_arn       = aws_iam_role.codedeploy[0].arn
  deployment_config_name = "CodeDeployDefault.OneAtATime"
  autoscaling_groups     = [var.asg_name]

  deployment_style {
    deployment_option = "WITH_TRAFFIC_CONTROL"
    deployment_type   = "IN_PLACE"
  }
  load_balancer_info {
    target_group_info { name = var.target_group_name }
  }
  auto_rollback_configuration {
    enabled = true
    events  = ["DEPLOYMENT_FAILURE"]
  }
  tags = local.common_tags

  lifecycle {
    precondition {
      condition     = var.asg_name != "" && var.target_group_name != "" && var.instance_role_name != ""
      error_message = "deploy_target=ec2 needs asg_name, target_group_name and instance_role_name (ec2 module outputs)."
    }
  }
}

# CodeDeploy agent on every ASG instance (incl. ones launched later) — the SSM
# Distributor package handles Amazon Linux / RHEL / Ubuntu / Windows.
resource "aws_ssm_association" "codedeploy_agent" {
  count            = local.ec2 ? 1 : 0
  name             = "AWS-ConfigureAWSPackage"
  association_name = "${local.name}-codedeploy-agent"
  parameters = {
    action = "Install"
    name   = "AWSCodeDeployAgent"
  }
  targets {
    key    = "tag:aws:autoscaling:groupName"
    values = [var.asg_name]
  }
  schedule_expression = "rate(1 day)"
}

# The agent downloads the revision from the pipeline artifact bucket.
resource "aws_iam_role_policy" "instance_revision_read" {
  count = local.ec2 ? 1 : 0
  name  = "${local.name}-codedeploy-revision-read"
  role  = var.instance_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.artifacts.arn}/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.artifacts.arn },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:DescribeKey"], Resource = aws_kms_key.artifacts.arn },
    ]
  })
}

output "pipeline_name" {
  description = "Pipeline name"
  value       = aws_codepipeline.this.name
}
output "pipeline_arn" {
  description = "Pipeline ARN"
  value       = aws_codepipeline.this.arn
}
output "artifact_bucket" {
  description = "Artifact bucket"
  value       = aws_s3_bucket.artifacts.bucket
}
output "kms_key_arn" {
  description = "Pipeline artifact KMS key ARN"
  value       = aws_kms_key.artifacts.arn
}
output "codebuild_project" {
  description = "CodeBuild project"
  value       = aws_codebuild_project.build.name
}
output "codedeploy_app" {
  description = "CodeDeploy app"
  value       = local.ec2 ? aws_codedeploy_app.app[0].name : ""
}
