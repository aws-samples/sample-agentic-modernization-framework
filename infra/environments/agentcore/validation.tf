# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
################################################################################
# Validation probe — application-level checks L1–L3
#
# validate_deployment (tool_execution) creates a short-lived probe Lambda
# `${prefix}-probe-<app>-<hex>` in the migrated app's private subnets so it can
# reach an internal ALB, invokes it once, and deletes it. The probe's role can
# only attach to the VPC and write its logs; it holds no data-plane access.
# Lambda targets are called directly with SigV4 (execute-api:Invoke) instead.
################################################################################

resource "aws_iam_role" "validation_probe" {
  name = "${var.project_prefix}-validation-probe-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "validation_probe_vpc" {
  role       = aws_iam_role.validation_probe.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# What the tool Lambda needs to run the tiers: manage the probe function, pass
# its role, find the app's subnets/SG (VPC Lambda ENIs need the ec2 network
# actions), call AWS_IAM HTTP APIs, and read/delete the L3 test record.
resource "aws_iam_role_policy" "tool_lambda_validation_probe" {
  #checkov:skip=CKV_AWS_355:ec2 Describe* have no resource type; ENI create/delete is done by the probe's own role (AWSLambdaVPCAccessExecutionRole), not this one
  name = "${var.project_prefix}-tool-lambda-validation-probe"
  role = aws_iam_role.tool_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["lambda:CreateFunction", "lambda:DeleteFunction", "lambda:InvokeFunction", "lambda:GetFunction",
        "lambda:GetFunctionConfiguration", "lambda:TagResource"]
        Resource = "arn:${local.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${var.project_prefix}-probe-*"
      },
      {
        # Exactly the probe role, only to Lambda (no wildcard pass).
        Effect   = "Allow"
        Action   = ["iam:PassRole"] # nosemgrep: terraform.lang.security.iam.no-iam-resource-exposure.no-iam-resource-exposure
        Resource = aws_iam_role.validation_probe.arn
        Condition = {
          StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" }
        }
      },
      {
        # CreateFunction with VpcConfig needs the caller to describe the subnets/SGs/VPC; these
        # Describe APIs have no resource type. The ENIs themselves are created and deleted by
        # Lambda with the PROBE's role, so this role holds no ENI write.
        Effect   = "Allow"
        Action   = ["ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs", "ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["execute-api:Invoke"]
        Resource = "arn:${local.partition}:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/*/*"
      },
      {
        Effect   = "Allow"
        Action   = ["apigateway:GET"]
        Resource = "arn:${local.partition}:apigateway:${var.aws_region}::/apis*"
      },
      {
        # L3 round-trip: read the record the app wrote, then delete it — app tables only
        # (H3; was table/*, i.e. AMF's own table too). The generated
        # dynamodb module names the table "{app_id}-table" (APP-*) by default ...
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:DeleteItem"]
        Resource = "arn:${local.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/APP-*"
      },
      {
        # ... but META dynamodb_table_name / the repo-detected name can override it, so also
        # any table the generated IaC tagged as an AMF app (backend.tf default_tags:
        # Application = app id, ManagedBy = amf-terraform). AMF's own tables carry neither.
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:DeleteItem"]
        Resource = "arn:${local.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/*"
        Condition = {
          StringLike   = { "aws:ResourceTag/Application" = "APP-*" }
          StringEquals = { "aws:ResourceTag/ManagedBy" = "amf-terraform" }
        }
      },
    ]
  })
}

output "validation_probe_role_arn" {
  description = "Validation probe role ARN"
  value       = aws_iam_role.validation_probe.arn
}
