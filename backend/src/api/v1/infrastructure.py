"""
Infrastructure API — Returns AgentCore deployment info for the frontend.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from backend.src.config import settings
from backend.src.core.models import ApiResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/")
def get_infrastructure():
    """Return the configured AgentCore deployment (names/ids only — no live calls)."""
    gateway_endpoint = settings.agentcore_gateway_url or (
        f"https://{settings.agentcore_gateway_id}.gateway.bedrock-agentcore.{settings.aws_region}.amazonaws.com/mcp"
        if settings.agentcore_gateway_id
        else ""
    )
    return ApiResponse(data={
        "deployment_mode": "agentcore",
        "region": settings.aws_region,
        "project_prefix": settings.project_prefix,
        "environment": settings.environment,
        "dynamodb_table": settings.dynamodb_table,
        "s3_bucket": settings.s3_bucket,
        "graph_backend": settings.graph_backend,
        "wave_planning_enabled": settings.wave_planning_enabled,
        "harness": {
            "id": settings.agentcore_harness_id,
            "arn": settings.agentcore_harness_arn,
            "model": settings.bedrock_model_id,
        },
        "gateway": {
            "id": settings.agentcore_gateway_id,
            "endpoint": gateway_endpoint,
        },
        "workflow": {
            "state_machine": settings.sfn_state_machine_name,
            "invoke_lambda": settings.agentcore_invoke_lambda,
        },
        "ecs": {
            "cluster": settings.atx_ecs_cluster,
            "task_definition": settings.assessment_ecs_task_definition,
            "log_group": settings.ecs_log_group,
        },
    })
