"""
System / capabilities endpoint.

Exposes feature flags so the frontend can adapt its UI — most importantly
whether wave planning is available (which depends on a configured graph
backend: dynamodb (default), neptune, or none = disabled).
"""

from fastapi import APIRouter

from backend.src.config import settings

router = APIRouter()


@router.get("/capabilities")
def get_capabilities():
    """Return runtime feature flags for the frontend to adapt behavior."""
    return {
        "environment": settings.environment,
        "deployment_mode": "agentcore",
        "aws_region": settings.aws_region,
        "agentcore_harness_id": settings.agentcore_harness_id,
        "bedrock_model_id": settings.bedrock_model_id,
        "graph_backend": settings.graph_backend,
        "wave_planning_enabled": settings.wave_planning_enabled,
        # Deploy gate (enable_deploy, default off): false = execution_review approves the branch, applies nothing.
        "deploy_enabled": settings.deploy_enabled,
        # The journey state machine the backend will start executions on. Lets
        # you confirm at runtime which flow is active (v2 = 7R + Cutover).
        "sfn_state_machine_name": settings.sfn_state_machine_name,
        # Target-region picker + UI polling cadence.
        "supported_target_regions": settings.supported_target_regions_list,
        "validation_poll_ms": settings.validation_poll_ms,
        "validation_poll_max": settings.validation_poll_max,
        "workflow_poll_ms": settings.workflow_poll_ms,
    }
