"""
Shared DynamoDB helpers.
Used across API modules for table access and type conversion.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Callable, TypeVar

import boto3

from backend.src.config import settings

logger = logging.getLogger(__name__)

_table = None
_T = TypeVar("_T")
_CREDENTIAL_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "RequestExpired",
    "UnrecognizedClientException",
}


def is_credential_error(exc: Exception) -> bool:
    """Return true for AWS errors caused by stale or invalid credentials."""
    response = getattr(exc, "response", {}) or {}
    code = (response.get("Error") or {}).get("Code", "")
    if code in _CREDENTIAL_ERROR_CODES:
        return True
    message = str(exc)
    return any(marker in message for marker in _CREDENTIAL_ERROR_CODES)


def reset_aws_session() -> None:
    """Drop process-level AWS clients so the next request reloads credentials.

    Local SSO role credentials can rotate while uvicorn remains alive. Boto3's
    default session and our lazy table singleton otherwise retain the expired
    role credentials and make healthy processes serve empty/error data.
    """
    global _table
    _table = None
    boto3.DEFAULT_SESSION = None

    from backend.src.core import aws

    aws.clear_cache()

    # Keep the separate dependency-injection singleton aligned when loaded.
    try:
        from backend.src.api import deps

        deps._db_client = None
    except Exception as _err:
        logger.debug("best-effort step ignored: %s", _err)


def call_with_credential_refresh(operation: Callable[[], _T]) -> _T:
    """Run an AWS operation, resetting cached clients once on credential expiry."""
    try:
        return operation()
    except Exception as exc:
        if not is_credential_error(exc):
            raise
        reset_aws_session()
        return operation()


def get_table():
    """Get the shared DynamoDB table resource (lazy-initialized singleton)."""
    global _table
    if _table is None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        _table = dynamodb.Table(settings.dynamodb_table)
    return _table


def query_all(table, max_items: int | None = None, **kwargs) -> dict:
    """`table.query(**kwargs)` following LastEvaluatedKey (a single query stops at 1 MB).

    Returns a query-shaped dict (`{"Items": [...], "Count": n}`) so it is a drop-in
    replacement for a single `table.query` call. `max_items` bounds the read.
    """
    items: list = []
    kwargs = dict(kwargs)
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last or (max_items is not None and len(items) >= max_items):
            break
        kwargs["ExclusiveStartKey"] = last
    if max_items is not None:
        items = items[:max_items]
    return {"Items": items, "Count": len(items)}


def to_dynamo(obj: Any) -> Any:
    """Convert Python types to DynamoDB-compatible types (floats → Decimal, strip None)."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_dynamo(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [to_dynamo(i) for i in obj]
    return obj


def from_dynamo(obj: Any) -> Any:
    """Convert DynamoDB types back to Python (Decimal → int/float)."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):
        return {k: from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_dynamo(i) for i in obj]
    return obj
