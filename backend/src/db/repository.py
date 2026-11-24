"""
Shared DynamoDB single-table reads (API and worker).
NO scan operations — only Query with PK/SK.
"""

from __future__ import annotations

import logging

from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)


# ── Shared read helper (single source; API and worker both use this) ────


def read_full_app(table, app_id: str) -> dict | None:
    """Read the full application aggregate (META + owners/repos/notes + machines).

    Lives in the db layer so both the API routers and the ECS worker can use it
    without the worker importing an API module.
    """
    from backend.src.db.helpers import from_dynamo

    resp = table.query(KeyConditionExpression=Key("PK").eq(f"APP#{app_id}"))
    items = resp.get("Items", [])
    if not items:
        return None

    app_data: dict = {}
    owners: list = []
    repos: list = []
    notes: list = []

    # Strip DynamoDB infra keys from aggregate children so the owners/repos/notes
    # payloads don't leak PK/SK/GSI* back out through the API.
    _INFRA_KEYS = ("PK", "SK", "GSI1PK", "GSI1SK")
    def _strip(d):
        return {k: v for k, v in d.items() if k not in _INFRA_KEYS}

    for item in items:
        item = from_dynamo(item)
        sk = item.get("SK", "")
        if sk == "META":
            app_data = item
        elif sk.startswith("OWNER#"):
            owners.append(_strip(item))
        elif sk.startswith("REPO#"):
            repos.append(_strip(item))
        elif sk.startswith("NOTE#"):
            notes.append(_strip(item))

    machine_ids = app_data.get("machine_ids", [])
    machines = []
    if machine_ids:
        for batch_start in range(0, len(machine_ids), 100):
            batch_ids = machine_ids[batch_start : batch_start + 100]
            keys = [{"PK": f"MACHINE#{mid}", "SK": "META"} for mid in batch_ids]
            response = table.meta.client.batch_get_item(RequestItems={table.name: {"Keys": keys}})
            for m in response.get("Responses", {}).get(table.name, []):
                machines.append(from_dynamo(m))

    app_data["owners"] = owners or app_data.get("owners", [])
    app_data["repos"] = repos or app_data.get("repos", [])
    app_data["additional_notes"] = notes
    app_data["machines"] = machines
    return app_data
