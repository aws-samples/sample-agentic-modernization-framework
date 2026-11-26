"""
FastAPI dependency injection providers.
Shared dependencies for all API routers.
"""

from __future__ import annotations

from backend.src.db.dynamodb import DynamoDBClient

# Singletons — initialized once, reused across requests
_db_client: DynamoDBClient | None = None


def get_db() -> DynamoDBClient:
    """Get the shared DynamoDB client instance."""
    global _db_client
    if _db_client is None:
        _db_client = DynamoDBClient()
    return _db_client


