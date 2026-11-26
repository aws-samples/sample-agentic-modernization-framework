"""
Shared domain models and enums.
All fields use snake_case per project convention.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    status: str = "success"
    data: Any = None
    meta: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


_STATUS_MAP: dict[str, int] = {
    "NOT_FOUND": 404,
    "VALIDATION_ERROR": 400,
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "CONFLICT": 409,
    "WORKFLOW_ALREADY_RUNNING": 409,
    "GRAPH_DISABLED": 503,
    "SERVICE_UNAVAILABLE": 503,
}


def _http_status_for(code: str) -> int:
    """Map a machine-readable error code onto an HTTP status code."""
    if code in _STATUS_MAP:
        return _STATUS_MAP[code]
    c = code.upper()
    if c.endswith("_NOT_FOUND"):
        return 404
    if "INVALID" in c or "VALIDATION" in c:
        return 400
    if "EXPIRED" in c or "UNAUTHORIZED" in c or "AUTH" in c:
        return 401
    if "DISABLED" in c or "UNAVAILABLE" in c:
        return 503
    return 500


def error_response(
    message: str,
    code: str = "INTERNAL_ERROR",
    details: dict[str, Any] | None = None,
    status_code: int | None = None,
) -> JSONResponse:
    """Create a standardized error response carrying a real HTTP status code.

    Returning a bare ApiResponse here would be serialized by FastAPI with
    HTTP 200, so every failure would look like a success to clients and
    surface as an empty screen rather than an error. Returning a JSONResponse
    keeps the same JSON envelope but sets the status line correctly.

    Args:
        message: Human-readable error message.
        code: Machine-readable error code (e.g. NOT_FOUND, VALIDATION_ERROR).
        details: Optional additional context.
        status_code: Explicit HTTP status override; derived from `code` when omitted.

    Returns:
        JSONResponse with status="error", a structured error payload, and a
        non-2xx HTTP status code.
    """
    error_payload: dict[str, Any] = {"code": code, "message": message}
    if details:
        error_payload["details"] = details
    body = ApiResponse(status="error", error=error_payload).model_dump()
    return JSONResponse(status_code=status_code or _http_status_for(code), content=body)


_error_logger = logging.getLogger("migration_framework.errors")

GENERIC_ERROR_MESSAGE = "The request failed due to an internal error."


def generic_error_message(exc: BaseException, code: str = "INTERNAL_ERROR", context: str = "") -> str:
    """Log `exc` server-side and return a client-safe message carrying an error id.

    Exception text (ARNs, bucket names, stack details) never reaches the client
   ; the error id correlates the response with the log line.
    """
    error_id = uuid.uuid4().hex[:12]
    _error_logger.error(
        "%s%s error_id=%s: %s", code, f" [{context}]" if context else "", error_id, exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return f"{GENERIC_ERROR_MESSAGE} (code {code}, error id {error_id})"


def internal_error(
    exc: BaseException,
    code: str = "INTERNAL_ERROR",
    *,
    context: str = "",
    status_code: int | None = None,
) -> JSONResponse:
    """error_response() for an unexpected exception: logs it, returns a generic message."""
    return error_response(generic_error_message(exc, code, context), code=code, status_code=status_code)
