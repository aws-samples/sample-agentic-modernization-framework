# kics-scan disable=0ca1017d-3b80-423e-bb9c-6cd5898d34bd,ba2ed23b-52d3-45ca-be25-f6c358d45abd,e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   0ca1017d: permissions boundary: the ceiling for app roles, not a grant
#   ba2ed23b: permissions boundary: the ceiling for app roles, not a grant
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
################################################################################
# Deploy role — the identity that terraform-applies an app's
# generated IaC after the human approves the exact migration-branch commit.
#
# Only the ECS worker task role may assume it, and only the worker's
# INFRA_ACTION=apply|destroy path does (terraform_runner). Separate from the tool
# Lambda role and from the worker's own (plan-only) permissions.
#
# Scope: PowerUserAccess (no IAM, no Organizations) + IAM write limited to the
# names the generated modules create — every module names resources
# "{app_id}-..." and AMF app ids are "APP-..." (keep in sync with
# templates/terraform/modules/*) — and only for roles that carry
# the app permissions boundary defined below. Accounts AMF may deploy into are allow-listed
# separately on the tool Lambda (DEPLOY_ALLOWED_ACCOUNTS).
################################################################################

variable "enable_deploy" {
  description = <<-EOT
    Off by default: AMF stops at a reviewed migration branch (IaC + CI/CD committed, terraform plan clean) and
    applies nothing. true creates the deploy role (PowerUserAccess + bounded APP-* IAM) and lets an Admin's
    execution_review approval terraform-apply the reviewed commit into deploy_allowed_accounts.
  EOT
  type        = bool
  default     = false
}

locals {
  deploy = var.enable_deploy ? 1 : 0
  # The prefix appears with "-" in most names but "_" in DynamoDB tables ({prefix_us}_portfolio,
  # {prefix}_tf_lock) and the Harness name, so prefix-based denies must match both forms.
  prefix_us = replace(var.project_prefix, "-", "_")
  # Named ARN for the Deny statements below (valid whether or not the role exists).
  deploy_role_named_arn = "arn:${local.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.project_prefix}-deploy-role"
}

variable "deploy_allowed_accounts" {
  description = "Comma-separated target accounts AMF may terraform-apply into. Empty = only this account."
  type        = string
  default     = ""
}

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [module.worker_task.worker_task_role_arn]
    }
  }
}

resource "aws_iam_role" "deploy" {
  count                = local.deploy
  name                 = "${var.project_prefix}-deploy-role"
  description          = "AMF deploy role: the worker assumes it to terraform-apply a human-approved app commit (only when enable_deploy is true)."
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 7200 # apply + first pipeline run
}

resource "aws_iam_role_policy_attachment" "deploy_power_user" {
  count      = local.deploy
  role       = aws_iam_role.deploy[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/PowerUserAccess"
}

################################################################################
# App permissions boundary — the most any generated app role
# (APP-*) may ever do: exactly the services backend/resources/templates/terraform/
# modules/* use (ECS/ECR, Lambda, DynamoDB data plane, S3 app buckets, logs/metrics,
# X-Ray, the app's RDS-managed secret, SSM agent, CodeBuild/CodePipeline/CodeDeploy/
# CodeCommit for the app pipeline). No IAM write, no sts:AssumeRole, no
# Organizations/Account, and never AMF's own control-plane data. Every template role
# sets permissions_boundary = var.permissions_boundary_arn (rendered by
# tool_execution from APP_PERMISSIONS_BOUNDARY_NAME), and the deploy role below
# can't create or grow an APP-* role without it.
################################################################################

locals {
  account_id                    = data.aws_caller_identity.current.account_id
  app_permissions_boundary_name = "${var.project_prefix}-app-permissions-boundary"
  app_permissions_boundary_arn  = "arn:${local.partition}:iam::${data.aws_caller_identity.current.account_id}:policy/${local.app_permissions_boundary_name}"
  # AWS-managed policies the deploy role may never attach (ArnLike on iam:PolicyARN).
  deploy_denied_managed_policies = [
    "arn:${local.partition}:iam::aws:policy/AdministratorAccess",
    "arn:${local.partition}:iam::aws:policy/PowerUserAccess",
    "arn:${local.partition}:iam::aws:policy/IAMFullAccess",
    "arn:${local.partition}:iam::aws:policy/*FullAccess",
  ]
}

data "aws_iam_policy_document" "app_permissions_boundary" {
  #checkov:skip=CKV_AWS_108:permissions boundary: the ceiling for app roles, not a grant
  #checkov:skip=CKV_AWS_111:permissions boundary: the ceiling for app roles, not a grant
  #checkov:skip=CKV_AWS_356:permissions boundary / actions without resource-level permissions
  statement {
    sid = "Logs" # this account's log groups/streams only
    actions = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams",
    "logs:PutRetentionPolicy"]
    resources = ["arn:${local.partition}:logs:*:${local.account_id}:log-group:*"]
  }
  statement {
    # Actions with no resource-level permissions (Service Authorization Reference), plus the SSM
    # agent set AmazonSSMManagedInstanceCore also grants on "*":
    # logs/metric/X-Ray/tag reads, the SSM agent (incl. its S3 encryption check), ECR auth,
    # EC2 + ASG/ELB describes, ECS task definitions. One statement keeps the boundary under
    # IAM's 6,144-character managed-policy limit. Everything else is account-scoped.
    sid = "NoResourceLevelPermissions"
    actions = [
      "logs:DescribeLogGroups", "cloudwatch:PutMetricData", "cloudwatch:GetMetricData", "xray:PutTraceSegments", "xray:PutTelemetryRecords",
      "xray:GetSamplingRules", "xray:GetSamplingTargets", "xray:GetSamplingStatisticSummaries", "tag:GetResources", "s3:GetEncryptionConfiguration",
      "ecr:GetAuthorizationToken", "ec2:Describe*", "ssm:DescribeAssociation", "ssm:GetDeployablePatchSnapshotForInstance", "ssm:GetDocument",
      "ssm:DescribeDocument", "ssm:GetManifest", "ssm:ListAssociations", "ssm:ListInstanceAssociations", "ssm:PutInventory",
      "ssm:PutComplianceItems", "ssm:PutConfigurePackageResult", "ssm:UpdateAssociationStatus", "ssm:UpdateInstanceAssociationStatus", "ssm:UpdateInstanceInformation",
      "ssm:GetParameter", "ssm:GetParameters", "ssmmessages:*", "ec2messages:*", "ecs:RegisterTaskDefinition",
      "ecs:DescribeTaskDefinition", "autoscaling:Describe*", "elasticloadbalancing:Describe*",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "AlarmsThisAccount"
    actions   = ["cloudwatch:DescribeAlarms"]
    resources = ["arn:${local.partition}:cloudwatch:*:${local.account_id}:alarm:*"]
  }
  statement {
    # Data plane only, this account only. The generated dynamodb module names the table
    # "{app_id}-table" (APP-*) by default ...
    sid = "DynamoDbDataPlaneAppTables"
    actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
      "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
    "dynamodb:DescribeTable", "dynamodb:ConditionCheckItem"]
    resources = ["arn:${local.partition}:dynamodb:*:${local.account_id}:table/APP-*"]
  }
  statement {
    # ... but META dynamodb_table_name / the repo-detected name can override it, so also any
    # table in this account the generated IaC tagged as an AMF app (backend.tf default_tags:
    # Application = app id, ManagedBy = amf-terraform). AMF's own table carries neither tag.
    sid = "DynamoDbDataPlaneTaggedAppTables"
    actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
      "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
    "dynamodb:DescribeTable", "dynamodb:ConditionCheckItem"]
    resources = ["arn:${local.partition}:dynamodb:*:${local.account_id}:table/*"]
    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/Application"
      values   = ["APP-*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/ManagedBy"
      values   = ["amf-terraform"]
    }
  }
  statement {
    sid = "AppBuckets" # every generated bucket is "{app_id lowercased}-..."
    actions = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation",
    "s3:GetBucketVersioning"]
    resources = ["arn:${local.partition}:s3:::app-*", "arn:${local.partition}:s3:::app-*/*"]
    # S3 ARNs carry no account ID: without this, app-* would match buckets in any account.
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [local.account_id]
    }
  }
  statement {
    sid = "EcrAppRepos" # pull (task execution role) + push (CodeBuild); repos are "{app_id lowercased}"
    actions = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
    "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"]
    resources = ["arn:${local.partition}:ecr:*:${local.account_id}:repository/app-*"]
  }
  statement {
    sid       = "AppSecretsRead" # the RDS-managed master secret (rds!...) or an APP-* secret
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:${local.partition}:secretsmanager:*:${local.account_id}:secret:rds!*", "arn:${local.partition}:secretsmanager:*:${local.account_id}:secret:APP-*"]
  }
  statement {
    sid = "Kms" # this account's keys; key policies still gate use (the RDS key: kms:ViaService + kms:CallerAccount)
    actions = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext",
    "kms:Encrypt"]
    resources = ["arn:${local.partition}:kms:*:${local.account_id}:key/*"]
  }
  statement {
    sid = "VpcNetworking" # Lambda VPC ENIs, this account only
    actions = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface",
    "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses", "ec2:CreateTags"]
    resources = ["arn:${local.partition}:ec2:*:${local.account_id}:*"]
  }
  statement {
    sid = "LambdaAppFunctions" # CodeBuild update-function-code --publish; functions are "{app_id}-..."
    actions = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode",
    "lambda:PublishVersion", "lambda:InvokeFunction"]
    resources = ["arn:${local.partition}:lambda:*:${local.account_id}:function:APP-*"]
  }
  statement {
    sid       = "EcsDeploy" # CodePipeline ECS deploy action, this account's clusters/services/tasks only
    actions   = ["ecs:DescribeServices", "ecs:DescribeTasks", "ecs:ListTasks", "ecs:UpdateService", "ecs:TagResource"]
    resources = ["arn:${local.partition}:ecs:*:${local.account_id}:*"]
  }
  statement {
    # The ECS deploy action re-registers the task definition with its (bounded) APP-* roles.
    sid       = "PassAppRolesToEcs"
    actions   = ["iam:PassRole"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/APP-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com", "ec2.amazonaws.com"]
    }
  }
  statement {
    sid = "AppPipeline" # CodeBuild reports, CodePipeline -> CodeBuild, EventBridge -> pipeline
    actions = ["codebuild:CreateReportGroup", "codebuild:CreateReport", "codebuild:UpdateReport",
      "codebuild:BatchPutTestCases", "codebuild:BatchPutCodeCoverages", "codebuild:BatchGetBuilds",
    "codebuild:StartBuild", "codepipeline:StartPipelineExecution"]
    resources = [
      "arn:${local.partition}:codebuild:*:${local.account_id}:report-group/APP-*",
      "arn:${local.partition}:codebuild:*:${local.account_id}:project/APP-*",
      "arn:${local.partition}:codepipeline:*:${local.account_id}:APP-*",
    ]
  }
  statement {
    sid = "CodeCommitSource" # repo names come from the linked repo, not the app id
    actions = ["codecommit:GetBranch", "codecommit:GetCommit", "codecommit:UploadArchive",
    "codecommit:GetUploadArchiveStatus", "codecommit:CancelUploadArchive"]
    resources = ["arn:${local.partition}:codecommit:*:${local.account_id}:*"]
  }
  statement {
    sid = "CodeDeployInPlace" # pipeline -> CodeDeploy, and AWSCodeDeployRole (ASG + ALB in-place deploys), this account only
    actions = ["codedeploy:CreateDeployment", "codedeploy:Get*", "codedeploy:RegisterApplicationRevision",
    "autoscaling:*", "elasticloadbalancing:*"]
    resources = ["arn:${local.partition}:codedeploy:*:${local.account_id}:*", "arn:${local.partition}:autoscaling:*:${local.account_id}:*", "arn:${local.partition}:elasticloadbalancing:*:${local.account_id}:*"]
  }
  statement {
    sid       = "CodeDeployNotifications"
    actions   = ["sns:Publish"]
    resources = ["arn:${local.partition}:sns:*:${local.account_id}:APP-*"]
  }
  statement {
    sid    = "NoPrivilegeEscalation" # defense in depth: a boundary Deny wins over any identity policy
    effect = "Deny"
    actions = [
      "iam:Add*", "iam:Attach*", "iam:Change*", "iam:Create*", "iam:Deactivate*", "iam:Delete*", "iam:Detach*",
      "iam:Enable*", "iam:Put*", "iam:Remove*", "iam:Reset*", "iam:Set*", "iam:Tag*", "iam:Untag*", "iam:Update*",
      "iam:Upload*", "sts:AssumeRole", "sts:AssumeRoleWithWebIdentity", "sts:AssumeRoleWithSAML",
      "organizations:*", "account:*",
    ]
    resources = ["*"]
  }
  statement {
    sid     = "NoAmfControlPlane" # AMF's own resources are never app resources
    effect  = "Deny"
    actions = ["dynamodb:*", "s3:*", "secretsmanager:*", "states:*", "ecs:*", "ecr:*", "codecommit:*", "kms:*"]
    # "*" for the account keeps this under IAM's 6,144-char limit; a broader Deny is safe.
    resources = distinct([
      # Tables {prefix_us}_portfolio and {prefix}_tf_lock (setup.sh keeps the raw prefix)
      "arn:${local.partition}:dynamodb:*:*:table/${local.prefix_us}_*",
      "arn:${local.partition}:dynamodb:*:*:table/${var.project_prefix}_*",
      "arn:${local.partition}:s3:::${var.project_prefix}-*", # data, frontend, tf-state, ALB logs
      "arn:${local.partition}:secretsmanager:*:*:secret:${var.project_prefix}/*",
      "arn:${local.partition}:states:*:*:*:${var.project_prefix}-*",
      # ECS cluster/service/task/task-definition ARNs all read "<type>/{prefix}-..."
      "arn:${local.partition}:ecs:*:*:*/${var.project_prefix}-*",
      "arn:${local.partition}:ecr:*:*:repository/${var.project_prefix}-*", # api, worker, atx-runner
      "arn:${local.partition}:codecommit:*:*:${var.project_prefix}-*",     # AMF owns none; reserved
      aws_kms_key.platform.arn,                                            # the platform CMK
    ])
  }
}

resource "aws_iam_policy" "app_permissions_boundary" {
  name        = local.app_permissions_boundary_name
  description = "AMF: maximum permissions of any generated APP-* role; required by the deploy role."
  policy      = data.aws_iam_policy_document.app_permissions_boundary.json
}

data "aws_iam_policy_document" "deploy_iam" {
  #checkov:skip=CKV_AWS_356:permissions boundary / actions without resource-level permissions
  statement {
    sid = "AppScopedIam"
    actions = [
      "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole", "iam:UpdateRole",
      "iam:UpdateAssumeRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole", "iam:GetRolePolicy", "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy", "iam:PassRole",
      "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile",
      "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile",
    ]
    resources = [
      "arn:${local.partition}:iam::${local.account_id}:role/APP-*",
      "arn:${local.partition}:iam::${local.account_id}:instance-profile/APP-*",
    ]
  }
  statement {
    # Creating or growing an APP-* role requires the app boundary on it (R4/H4). Before,
    # CreateRole + AttachRolePolicy(AdministratorAccess) + sts:AssumeRole was admin.
    sid       = "BoundedAppRoleWrite"
    actions   = ["iam:CreateRole", "iam:PutRolePermissionsBoundary", "iam:AttachRolePolicy", "iam:PutRolePolicy"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/APP-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.app_permissions_boundary_arn]
    }
  }
  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/aws-service-role/*"]
  }
  statement {
    sid     = "ReadIamForPlans"
    actions = ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions", "iam:GetOpenIDConnectProvider"]
    resources = [
      "arn:${local.partition}:iam::${local.account_id}:policy/*",
      "arn:${local.partition}:iam::aws:policy/*", # AWS-managed policies the app IaC references
      "arn:${local.partition}:iam::${local.account_id}:oidc-provider/*",
    ]
  }
  statement {
    sid       = "DenyBoundaryRemoval"
    effect    = "Deny"
    actions   = ["iam:DeleteRolePermissionsBoundary", "iam:DeleteUserPermissionsBoundary"]
    resources = ["*"]
  }
  statement {
    sid    = "DenyBoundaryPolicyTamper"
    effect = "Deny"
    actions = ["iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:DeletePolicyVersion",
    "iam:SetDefaultPolicyVersion", "iam:TagPolicy", "iam:UntagPolicy"]
    resources = [local.app_permissions_boundary_arn]
  }
  statement {
    sid       = "DenyPrivilegedManagedPolicies"
    effect    = "Deny"
    actions   = ["iam:AttachRolePolicy", "iam:AttachUserPolicy", "iam:AttachGroupPolicy"]
    resources = ["*"]
    condition {
      test     = "ArnLike"
      variable = "iam:PolicyARN"
      values   = local.deploy_denied_managed_policies
    }
  }
  statement {
    # The deploy role builds app roles; it must never become one (PowerUser allows sts:*).
    sid       = "DenyAssumeAppRoles"
    effect    = "Deny"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/APP-*"]
  }
  statement {
    sid       = "DenyPassDeployRole"
    effect    = "Deny"
    actions   = ["iam:PassRole"]
    resources = [local.deploy_role_named_arn]
  }
}

resource "aws_iam_role_policy" "deploy_iam" {
  count  = local.deploy
  name   = "${var.project_prefix}-deploy-app-iam"
  role   = aws_iam_role.deploy[0].id
  policy = data.aws_iam_policy_document.deploy_iam.json
}

# The worker may assume the deploy role (its own policy, beside the module's).
resource "aws_iam_role_policy" "worker_assume_deploy" {
  count = local.deploy
  name  = "${var.project_prefix}-worker-assume-deploy"
  role  = element(split("/", module.worker_task.worker_task_role_arn), length(split("/", module.worker_task.worker_task_role_arn)) - 1)
  policy = jsonencode({
    Version = "2012-10-17"
    # nosemgrep: terraform.lang.security.iam.no-iam-creds-exposure.no-iam-creds-exposure
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.deploy[0].arn }] # the one deploy role; trust policy admits only this task role
  })
}

output "deploy_role_arn" {
  description = "Deploy role ARN (empty when enable_deploy = false)"
  value       = var.enable_deploy ? aws_iam_role.deploy[0].arn : ""
}
output "app_permissions_boundary_arn" {
  description = "App permissions boundary ARN"
  value       = aws_iam_policy.app_permissions_boundary.arn
}

# enable_deploy put the deploy resources behind count: keep existing deployments' state (no recreate).
moved {
  from = aws_iam_role.deploy
  to   = aws_iam_role.deploy[0]
}
moved {
  from = aws_iam_role_policy_attachment.deploy_power_user
  to   = aws_iam_role_policy_attachment.deploy_power_user[0]
}
moved {
  from = aws_iam_role_policy.deploy_iam
  to   = aws_iam_role_policy.deploy_iam[0]
}
moved {
  from = aws_iam_role_policy.worker_assume_deploy
  to   = aws_iam_role_policy.worker_assume_deploy[0]
}
