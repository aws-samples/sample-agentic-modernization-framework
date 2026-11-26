# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
# AgentCore Harness — Declarative Orchestrator (no custom code)
#
# The system prompt is COMPOSED from modular skill files in
# backend/resources/skills/*.md (base rules + per-phase playbooks). This keeps
# agent behavior in versioned, editable skill modules — the single Harness
# applies the playbook matching the phase named in each invocation message.

locals {
  gateway_arn = "arn:${local.partition}:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:gateway/${aws_bedrockagentcore_gateway.main.gateway_id}"

  # Skill modules live in the backend package (three levels up from this env dir).
  skills_dir = "${path.module}/../../../backend/resources/skills"

  harness_base_prompt = <<-EOT
You are the AMF Migration Orchestrator Agent for enterprise AWS migrations.

Available tools (via MCP Gateway targets):
- portfolio: get_app_context, update_app_field, search_machines, assign_machines_to_app, get_portfolio_summary, add_owner
- strategy: recommend_strategy, calculate_completeness
- planning: generate_wave_plan, store_wave_plan, get_wave_plan, store_migration_plan
- documents: store_design_document, get_design_document, store_analysis_result, get_analysis_result
- execution: start_code_analysis, start_code_transformation, get_job_status, provision_infrastructure (generate+stage Terraform), generate_cicd (generate+stage CI/CD), commit_cicd_to_repo (commit staged CI/CD files to CodeCommit)

Rules:
1. Always call portfolio___get_app_context first to load current state.
2. Reference actual values from the context (server counts, DB versions, costs).
3. Persist changes via portfolio___update_app_field.
4. For code analysis: execution___start_code_analysis then execution___get_job_status.
5. When storing analysis results, use analysis_type="comprehensive-codebase-analysis".
6. When storing design documents, include app_id, version (v1-v5 or final), and full markdown.
7. Be specific and structured — cite real data.

Below are the PHASE PLAYBOOKS. Each invocation names the current phase — follow the
matching playbook (its procedure, quality bar, and anti-patterns).
EOT

  # Compose base rules + every skill module into one system prompt.
  harness_skill_files = ["discovery", "risk_scoring", "grouping", "assessment", "planning", "design", "execution", "validation", "handover"]
  harness_system_prompt = join("\n\n---\n\n", concat(
    [local.harness_base_prompt],
    [for s in local.harness_skill_files : "# PLAYBOOK: ${s}\n\n${file("${local.skills_dir}/${s}.md")}"]
  ))
}

resource "aws_bedrockagentcore_harness" "orchestrator" {
  harness_name       = "${replace(var.project_prefix, "-", "_")}_harness"
  execution_role_arn = aws_iam_role.agent_runtime_role.arn

  model {
    bedrock_model_config {
      model_id = var.bedrock_model_id
    }
  }

  system_prompt {
    text = local.harness_system_prompt
  }

  # No long-term memory. Left unset, AgentCore attaches a managed memory with a
  # SEMANTIC strategy under one shared actor ("default"), so facts from every
  # earlier run — and every other app — are recalled as current: APP-CLI's
  # 2026-10-19 Assessment/Design reported the 2026-10-13 run's Terraform commit
  # and execution steps as done. Run scoping (workflow._reset_app_for_new_run)
  # can't reach that store; phases get their facts from the prompt and tools.
  memory {
    disabled {}
  }

  # Single tool block for the entire Gateway (loads all targets' tools once)
  tool {
    name = "amf_gateway"
    type = "agentcore_gateway"
    config {
      agentcore_gateway {
        gateway_arn = local.gateway_arn
        # Sign tool calls with the runtime role when the gateway requires IAM.
        outbound_auth {
          aws_iam = var.gateway_authorizer_type == "AWS_IAM" ? true : null
          none    = var.gateway_authorizer_type == "NONE" ? true : null
        }
      }
    }
  }

  # Workaround for AWS provider bug (>=6.60,<=6.66): on in-place UPDATE the
  # provider returns an inconsistent value for the computed, sensitive
  # `environment_variables` attribute, failing apply with "Provider produced
  # inconsistent result after apply". We don't manage that attribute, so ignore
  # it. See harness update failures during skill/system-prompt changes.
  lifecycle {
    ignore_changes = [environment_variables]
  }
}

output "harness_id" {
  description = "Harness ID"
  value       = aws_bedrockagentcore_harness.orchestrator.harness_id
}
output "harness_arn" {
  description = "Harness ARN"
  value       = aws_bedrockagentcore_harness.orchestrator.arn
}
