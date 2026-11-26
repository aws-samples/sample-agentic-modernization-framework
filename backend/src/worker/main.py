"""
Migration Worker entrypoint.

Runs the long-running compute for the AgentCore Harness flow. The Step
Functions state machine or execution tool launches this container in one of
four governed modes:

  MODE=code-analysis   → ATX comprehensive-codebase-analysis (LaunchATX state)
  MODE=workbench       → ATX code transformation (LaunchTransformation state)
  MODE=infra           → terraform validate + plan on generated IaC (InfrastructureValidate state)
  MODE=ci-fix          → CI-readiness fixes on the migration branch (FixCIGaps state, spec 14)

The ATX modes call back to Step Functions when launched with a task token. The
review publisher finalizes its DynamoDB JOB record and S3 audit report directly.
"""

from __future__ import annotations

import sys

from backend.src.worker.config import MODE, logger


def main():
    """Route to the appropriate ATX pipeline based on MODE."""
    if MODE == "code-analysis":
        from backend.src.worker.code_analysis import run_code_analysis

        run_code_analysis()
    elif MODE == "workbench":
        from backend.src.worker.workbench import run_workbench

        run_workbench()
    elif MODE == "infra":
        from backend.src.worker.terraform_runner import run_terraform

        run_terraform()
    elif MODE == "ci-fix":
        from backend.src.worker.ci_fix import run_ci_fix

        run_ci_fix()
    else:
        logger.error(
            "Unknown MODE: %s (expected 'code-analysis', 'workbench', 'infra' or 'ci-fix')",
            MODE,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
