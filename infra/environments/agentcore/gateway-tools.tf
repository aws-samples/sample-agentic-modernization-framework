# kics-scan disable=e592a0c5-5bdb-414c-9066-5dba7cdea370
# Accepted kics findings for this file (scanner suppressions; reasons):
#   e592a0c5: IAM Access Analyzer is enabled at account/org level, not per stack
# ══════════════════════════════════════════════════════════════════════
# AgentCore Gateway — Full Production Tool Set
#
# All stateless tools exposed via MCP Gateway for the Harness to call.
# Grouped by domain into Lambda targets.
# ══════════════════════════════════════════════════════════════════════

# ── Target 1: Portfolio (CRUD on app/machine data) ───────────────────

resource "aws_bedrockagentcore_gateway_target" "portfolio" {
  # Gateway role must be able to invoke the Lambda before the target validates.
  depends_on         = [aws_lambda_permission.portfolio_agentcore]
  name               = "portfolio"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Portfolio CRUD: get/update/search apps, machines, repos"

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tool_portfolio.arn
        tool_schema {
          inline_payload {
            name        = "get_app_context"
            description = "Get full application metadata including machines, repos, connections, and assessment history"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
            }
          }
          inline_payload {
            name        = "update_app_field"
            description = "Update one allowlisted app fact on an application record (detected stack, completeness fields, phase status). Refuses exposure, approval, target (account/compute/database/CI-CD/VPC) and strategy fields — those are server-only."
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name     = "field"
                type     = "string"
                required = true
              }
              property {
                name     = "value"
                type     = "string"
                required = true
              }
            }
          }
          inline_payload {
            name        = "search_machines"
            description = "Search unassigned machines by hostname pattern or IP range"
            input_schema {
              type = "object"
              property {
                name     = "query"
                type     = "string"
                required = true
              }
            }
          }
          inline_payload {
            name        = "assign_machines_to_app"
            description = "Link machines to an application by machine IDs"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name        = "machine_ids"
                type        = "string"
                required    = true
                description = "Comma-separated machine IDs"
              }
            }
          }
          inline_payload {
            name        = "get_portfolio_summary"
            description = "Get portfolio-wide summary: app count, strategy distribution, completeness stats"
            input_schema {
              type = "object"
            }
          }
          inline_payload {
            name        = "add_owner"
            description = "Add an owner to an application"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name     = "name"
                type     = "string"
                required = true
              }
              property {
                name     = "email"
                type     = "string"
                required = true
              }
              property {
                name     = "role"
                type     = "string"
                required = false
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}

# ── Target 2: Strategy (recommendation engine + completeness) ────────

resource "aws_bedrockagentcore_gateway_target" "strategy" {
  depends_on         = [aws_lambda_permission.strategy_agentcore]
  name               = "strategy"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Strategy recommendation engine and data completeness scoring"

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tool_strategy.arn
        tool_schema {
          inline_payload {
            name        = "recommend_strategy"
            description = "Recommend 7R migration strategy with confidence score based on app metadata, dependencies, and business rules"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
            }
          }
          inline_payload {
            name        = "calculate_completeness"
            description = "Calculate data completeness percentage across weighted categories and identify missing critical fields"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}

# ── Target 3: Planning (wave plans, dependencies, migration plan) ────

resource "aws_bedrockagentcore_gateway_target" "planning" {
  depends_on         = [aws_lambda_permission.planning_agentcore]
  name               = "planning"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Wave planning, dependency analysis, migration plan storage"

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tool_planning.arn
        tool_schema {
          inline_payload {
            name        = "generate_wave_plan"
            description = "Generate a wave plan per the planning playbook (Pilot, Foundation, Core, Customer-Facing, Long Tail; dependency order, shared-DB co-location, tier_1 spread). Read-only."
            input_schema {
              type = "object"
              property {
                name     = "pilot_size"
                type     = "string"
                required = false
              }
            }
          }
          inline_payload {
            name        = "store_wave_plan"
            description = "Persist a wave plan to DynamoDB and archive to S3"
            input_schema {
              type = "object"
              property {
                name     = "wave_plan"
                type     = "object"
                required = true
              }
            }
          }
          inline_payload {
            name        = "get_wave_plan"
            description = "Retrieve the current active wave plan"
            input_schema {
              type = "object"
            }
          }
          inline_payload {
            name        = "store_migration_plan"
            description = "Store a migration plan for an application (prerequisites, sequence, rollback, timeline)"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name     = "plan"
                type     = "object"
                required = true
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}

# ── Target 4: Documents (design docs, runbooks, reports) ─────────────

resource "aws_bedrockagentcore_gateway_target" "documents" {
  depends_on         = [aws_lambda_permission.documents_agentcore]
  name               = "documents"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Document generation and storage: design docs, runbooks, reports"

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tool_documents.arn
        tool_schema {
          inline_payload {
            name        = "store_design_document"
            description = "Store a design document version in S3 and record metadata in DynamoDB"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name     = "version"
                type     = "string"
                required = true
              }
              property {
                name     = "content"
                type     = "string"
                required = true
              }
            }
          }
          inline_payload {
            name        = "get_design_document"
            description = "Retrieve the latest or specific version of a design document"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name     = "version"
                type     = "string"
                required = false
              }
            }
          }
          inline_payload {
            name        = "store_analysis_result"
            description = "Store assessment/analysis results (code analysis, component analysis, etc.) for an application"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name     = "analysis_type"
                type     = "string"
                required = true
              }
              property {
                name     = "result"
                type     = "object"
                required = true
              }
            }
          }
          inline_payload {
            name        = "compute_risk_score"
            description = "Deterministically score an application's migration risk (0-100, higher = worse) across Risk/EOSL, Complexity, Team Readiness and Business Readiness; persists a risk-score analysis record and mirrors the score onto the app"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
            }
          }
          inline_payload {
            name        = "get_analysis_result"
            description = "Retrieve stored analysis results for an application"
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name     = "analysis_type"
                type     = "string"
                required = false
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}

# ── Target 5: Execution (async operations — start/poll pattern) ──────

resource "aws_bedrockagentcore_gateway_target" "execution" {
  depends_on         = [aws_lambda_permission.execution_agentcore]
  name               = "execution"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Async execution tools: start ATX analysis/transformation, poll status"

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tool_execution.arn
        tool_schema {
          inline_payload {
            name        = "start_code_analysis"
            description = "Launch ATX comprehensive-codebase-analysis on ECS Fargate. Returns job_id for polling."
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name        = "td_name"
                type        = "string"
                required    = false
                description = "ATX transform definition name (default: AWS/comprehensive-codebase-analysis)"
              }
            }
          }
          inline_payload {
            name        = "start_code_transformation"
            description = "Launch ATX code transformation on ECS Fargate. Returns job_id for polling."
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name        = "td_name"
                type        = "string"
                required    = true
                description = "ATX transform definition (e.g. AWS/java-version-upgrade)"
              }
              property {
                name     = "target_version"
                type     = "string"
                required = false
              }
            }
          }
          inline_payload {
            name        = "get_job_status"
            description = "Poll status of an async ECS job (code analysis or transformation)"
            input_schema {
              type = "object"
              property {
                name     = "job_id"
                type     = "string"
                required = true
              }
            }
          }
          inline_payload {
            name        = "provision_infrastructure"
            description = "Generate standardized Terraform for the app's target compute and STAGE it to S3 (generate + stage only; nothing applied)."
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name     = "vpc_cidr"
                type     = "string"
                required = false
              }
              property {
                name     = "account_id"
                type     = "string"
                required = false
              }
            }
          }
          inline_payload {
            name        = "generate_cicd"
            description = "Render the standardized CI/CD pipeline (build + coverage + deploy) and STAGE to S3 (generate + stage only; not committed)."
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name        = "cicd_target"
                type        = "string"
                required    = false
                description = "github_actions | gitlab | codepipeline"
              }
            }
          }
          inline_payload {
            name        = "commit_cicd_to_repo"
            description = "Commit the CI/CD files staged by generate_cicd into the app's linked CodeCommit repository (one commit on the migration branch). Call AFTER generate_cicd."
            input_schema {
              type = "object"
              property {
                name     = "app_id"
                type     = "string"
                required = true
              }
              property {
                name        = "branch"
                type        = "string"
                required    = false
                description = "Target branch (defaults to the linked repo's branch)"
              }
              property {
                name     = "commit_message"
                type     = "string"
                required = false
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}

# ── Lambda Resource-based Policies (allow Gateway to invoke) ─────────

resource "aws_lambda_permission" "portfolio_agentcore" {
  statement_id  = "AllowAgentCoreGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tool_portfolio.function_name
  principal     = "bedrock-agentcore.amazonaws.com"
  source_arn    = aws_iam_role.gateway_role.arn
}

resource "aws_lambda_permission" "strategy_agentcore" {
  statement_id  = "AllowAgentCoreGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tool_strategy.function_name
  principal     = "bedrock-agentcore.amazonaws.com"
  source_arn    = aws_iam_role.gateway_role.arn
}

resource "aws_lambda_permission" "planning_agentcore" {
  statement_id  = "AllowAgentCoreGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tool_planning.function_name
  principal     = "bedrock-agentcore.amazonaws.com"
  source_arn    = aws_iam_role.gateway_role.arn
}

resource "aws_lambda_permission" "documents_agentcore" {
  statement_id  = "AllowAgentCoreGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tool_documents.function_name
  principal     = "bedrock-agentcore.amazonaws.com"
  source_arn    = aws_iam_role.gateway_role.arn
}

resource "aws_lambda_permission" "execution_agentcore" {
  statement_id  = "AllowAgentCoreGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tool_execution.function_name
  principal     = "bedrock-agentcore.amazonaws.com"
  source_arn    = aws_iam_role.gateway_role.arn
}
