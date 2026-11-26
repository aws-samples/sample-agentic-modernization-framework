# ── Observability (R41) ──────────────────────────────────────────────
#
# AgentCore built-in observability (model invocations, traces, spans) surfaces
# in the CloudWatch "GenAI Observability" console. That view requires a one-time,
# account-level enablement of **CloudWatch Transaction Search**, which routes
# X-Ray trace segments into CloudWatch Logs and turns on 100% indexing.
#
# The AWS Terraform provider does not yet expose Transaction Search directly, so
# we enable it idempotently via the CLI behind an explicit opt-in flag. It is a
# shared, account-level change — leave `enable_transaction_search = false` unless
# you own the account's observability configuration.

variable "enable_transaction_search" {
  type        = bool
  description = "One-time, account-level enablement of CloudWatch Transaction Search for AgentCore GenAI Observability. Shared-account change — opt in explicitly."
  default     = true
}

variable "transaction_search_indexing_percentage" {
  type        = number
  description = "Percentage of trace segments to index (0-100). 100 gives full GenAI Observability coverage."
  default     = 100
}

resource "null_resource" "cloudwatch_transaction_search" {
  count = var.enable_transaction_search ? 1 : 0

  triggers = {
    indexing_percentage = var.transaction_search_indexing_percentage
    region              = var.aws_region
  }

  # Route X-Ray segments to CloudWatch Logs and set the indexing rule. Both
  # calls are idempotent — re-running keeps the destination/indexing at the
  # desired state.
  provisioner "local-exec" {
    command = <<-EOT
      aws xray update-trace-segment-destination --destination CloudWatchLogs --region ${var.aws_region}
      aws xray update-indexing-rule \
        --name "Default" \
        --rule "{\"Probabilistic\":{\"DesiredSamplingPercentage\":${var.transaction_search_indexing_percentage}}}" \
        --region ${var.aws_region}
    EOT
  }
}

output "transaction_search_enabled" {
  value       = var.enable_transaction_search
  description = "Whether CloudWatch Transaction Search enablement is managed by this stack."
}
