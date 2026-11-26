"""
DynamoDB client wrapper.
Single-table design using amf_portfolio table.
NO scan operations — only Query with PK/SK.
"""

from __future__ import annotations

import base64
import json

import boto3

from backend.src.config import settings


class DynamoDBClient:
    """Thin wrapper around the DynamoDB table resource."""

    def __init__(self):
        self._resource = boto3.resource("dynamodb", region_name=settings.aws_region)
        self._table = self._resource.Table(settings.dynamodb_table)

    @property
    def table(self):
        return self._table

    async def put_item(self, item: dict) -> dict:
        return self._table.put_item(Item=item)

    async def get_item(self, pk: str, sk: str) -> dict | None:
        response = self._table.get_item(Key={"PK": pk, "SK": sk})
        return response.get("Item")

    async def query(
        self,
        pk: str,
        sk_prefix: str | None = None,
        limit: int = 50,
        next_token: str | None = None,
    ) -> tuple[list[dict], str | None]:
        kwargs: dict = {
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": pk},
            "Limit": limit,
        }
        if sk_prefix:
            kwargs["KeyConditionExpression"] += " AND begins_with(SK, :sk)"
            kwargs["ExpressionAttributeValues"][":sk"] = sk_prefix
        if next_token:
            kwargs["ExclusiveStartKey"] = json.loads(base64.b64decode(next_token))

        response = self._table.query(**kwargs)
        items = response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        token = None
        if last_key:
            token = base64.b64encode(json.dumps(last_key).encode()).decode()
        return items, token

    async def update_item(self, pk: str, sk: str, updates: dict) -> dict:
        expr_parts = []
        attr_names = {}
        attr_values = {}
        for i, (key, val) in enumerate(updates.items()):
            expr_parts.append(f"#{key} = :v{i}")
            attr_names[f"#{key}"] = key
            attr_values[f":v{i}"] = val
        return self._table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
            ReturnValues="ALL_NEW",
        )

    async def delete_item(self, pk: str, sk: str) -> None:
        self._table.delete_item(Key={"PK": pk, "SK": sk})

