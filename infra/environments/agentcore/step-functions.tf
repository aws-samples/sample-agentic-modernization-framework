# kics-scan disable=0afbcfe9-d341-4b92-a64c-7e6de0543879,e592a0c5-5bdb-414c-9066-5dba7cdea370,ef0b316a-211e-42f1-888e-64efe172b755
# Accepted kics findings for this file (scanner suppressions; reasons):
#   0afbcfe9: AWS-managed log encryption; customer-managed KMS deferred / adopting team's choice
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
#   ef0b316a: false positive: retention is set via a local (log_retention_days)
# ══════════════════════════════════════════════════════════════════════
# Step Functions — the canonical migration journey state machine.
#
# Terraform owns the state machine (previously deployed out-of-band by a CLI
# step in setup.sh). The definition is the v2 ASL, with its harness/lambda ARNs
# injected via templatefile so nothing is hardcoded.
# ══════════════════════════════════════════════════════════════════════

variable "approval_timeout_seconds" {
  description = "TimeoutSeconds of every human approval gate (waitForTaskToken) in both ASLs. Default 7 days."
  type        = number
  default     = 604800
  validation {
    # Step Functions caps a task timeout at 1 year (the execution maximum).
    condition     = var.approval_timeout_seconds >= 60 && var.approval_timeout_seconds <= 31536000 && floor(var.approval_timeout_seconds) == var.approval_timeout_seconds
    error_message = "approval_timeout_seconds must be a whole number between 60 and 31536000."
  }
}

# ── Execution role ────────────────────────────────────────────────────

resource "aws_iam_role" "sfn" {
  name        = "${var.project_prefix}-sfn-role"
  description = "AMF migration workflow state machine: invokes the Harness and the AMF Lambdas, writes its logs."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sfn" {
  name = "${var.project_prefix}-sfn-perms"
  role = aws_iam_role.sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # A Harness runs as a managed AgentCore agent-runtime, so the
        # states:::bedrockagentcore:invokeHarness task requires InvokeAgentRuntime
        # (InvokeHarness alone yields AccessDeniedException).
        Sid    = "InvokeHarness"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeHarness",
          "bedrock-agentcore:InvokeAgentRuntime",
        ]
        Resource = [
          aws_bedrockagentcore_harness.orchestrator.arn,
          "${aws_bedrockagentcore_harness.orchestrator.arn}/*",
        ]
      },
      {
        Sid    = "InvokeBridgeAndToolLambdas"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          aws_lambda_function.invoke_runtime.arn,
          aws_lambda_function.tool_execution.arn,
          aws_lambda_function.tool_documents.arn,
        ]
      },
      {
        Sid    = "WorkflowLogging"
        Effect = "Allow"
        # Step Functions vended-log delivery needs these exact actions on "*" (they have no resource
        # type); logs:PutResourcePolicy is the CloudWatch Logs delivery policy, not a public-sharing grant.
        Action = [ # nosemgrep: terraform.lang.security.iam.no-iam-resource-exposure.no-iam-resource-exposure
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies",
          "logs:DescribeLogGroups",
        ]
        Resource = ["*"]
      },
    ]
  })
}

# ── Log group ─────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${var.project_prefix}-agentcore-workflow"
  retention_in_days = local.sfn_log_retention_days
  kms_key_id        = aws_kms_key.platform.arn
}

# ── State machine ─────────────────────────────────────────────────────

resource "aws_sfn_state_machine" "workflow" {
  #checkov:skip=CKV_AWS_284:X-Ray on the state machines deferred; execution history + Transaction Search cover tracing
  name     = "${var.project_prefix}-agentcore-workflow"
  role_arn = aws_iam_role.sfn.arn

  definition = templatefile("${path.module}/../../step-functions/migration-workflow.v2.asl.json", {
    HarnessArn      = aws_bedrockagentcore_harness.orchestrator.arn
    InvokeRuntimeFn = aws_lambda_function.invoke_runtime.arn
    ToolExecutionFn = aws_lambda_function.tool_execution.arn
    ToolDocumentsFn = aws_lambda_function.tool_documents.arn
    # Rendered as a bare JSON number (the ASL has it unquoted).
    ApprovalTimeoutSeconds = var.approval_timeout_seconds
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }
}

output "sfn_state_machine_arn" {
  description = "Step Functions state machine ARN"
  value       = aws_sfn_state_machine.workflow.arn
}
output "sfn_state_machine_name" {
  description = "Step Functions state machine name"
  value       = aws_sfn_state_machine.workflow.name
}
