"""
Typed single-table accessors (Requirements 32.1, 32.2, 32.3, 40.2).

A thin, synchronous accessor layer over the Portfolio_Table that:
  • reads/writes entities by their key builders (see keys.py),
  • exposes GSI1 "list all of a type" queries (applications, jobs, active
    workflows),
  • provides conditional / idempotent writes for workflow & phase transitions.

Type conversion (float↔Decimal, drop None) is handled via helpers.to_dynamo /
from_dynamo so callers work in plain Python types.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.conditions import Key as KeyCond

from backend.src.config import settings
from backend.src.db import keys
from backend.src.db.helpers import from_dynamo, get_table, query_all, to_dynamo

logger = logging.getLogger(__name__)


# ── Generic put / get keyed by a keys.Key ─────────────────────────────


def put(key: keys.Key, attrs: dict) -> dict:
    """Write an item at `key`, merging in GSI1 attributes and PK/SK.

    Returns the persisted item (Python types).
    """
    item = {**to_dynamo(attrs), **key.as_key(), **key.gsi1_attrs()}
    get_table().put_item(Item=item)
    return from_dynamo(item)


def get(key: keys.Key) -> dict | None:
    """Read a single item by key. None if absent."""
    resp = get_table().get_item(Key=key.as_key())
    item = resp.get("Item")
    return from_dynamo(item) if item else None


def delete(key: keys.Key) -> None:
    get_table().delete_item(Key=key.as_key())


# ── Entity reads ──────────────────────────────────────────────────────


def get_application(app_id: str) -> dict | None:
    return get(keys.application(app_id))


def get_active_workflow(app_id: str) -> dict | None:
    return get(keys.workflow_active(app_id))


def get_latest_analysis(app_id: str, analysis_type: str) -> dict | None:
    """Newest analysis record of a type (records are ANALYSIS#<type>#<ts>)."""
    resp = get_table().query(
        KeyConditionExpression=KeyCond("PK").eq(keys.app_pk(app_id))
        & KeyCond("SK").begins_with(keys.analysis_prefix(analysis_type)),
        ScanIndexForward=False,  # newest first
        Limit=1,
    )
    items = resp.get("Items", [])
    return from_dynamo(items[0]) if items else None


def list_app_children(app_id: str, sk_prefix: str | None = None) -> list[dict]:
    """All records under an application, optionally filtered by SK prefix."""
    cond = KeyCond("PK").eq(keys.app_pk(app_id))
    if sk_prefix:
        cond = cond & KeyCond("SK").begins_with(sk_prefix)
    resp = query_all(get_table(), KeyConditionExpression=cond)  # paginated (1 MB pages)
    return from_dynamo(resp.get("Items", []))


def list_machine_connections(machine_ids: list[str], table=None) -> list[dict]:
    """CONN# edges of every machine, in `machine_ids` order (duplicates kept).

    CONN# rows live under MACHINE#<id> and need a begins_with condition, so
    BatchGetItem can't fetch them; instead each distinct machine is queried once
    (paginated) and the queries run on a bounded thread pool rather than serially.
    """
    table = table or get_table()
    unique = list(dict.fromkeys(m for m in machine_ids if m))
    if not unique:
        return []

    def _one(mid: str) -> list[dict]:
        resp = query_all(
            table,
            KeyConditionExpression=KeyCond("PK").eq(f"MACHINE#{mid}") & KeyCond("SK").begins_with("CONN#"),
        )
        return from_dynamo(resp.get("Items", []))

    workers = max(1, min(settings.parallel_query_workers, len(unique)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        by_machine = dict(zip(unique, pool.map(_one, unique), strict=True))
    out: list[dict] = []
    for mid in machine_ids:
        if mid:
            out.extend(by_machine.get(mid, []))
    return out


# ── GSI1 "list all of a type" queries ─────────────────────────────────


def _query_type(gsi1pk: str, limit: int = 500) -> list[dict]:
    table = get_table()
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "IndexName": "GSI1",
        "KeyConditionExpression": KeyCond("GSI1PK").eq(gsi1pk),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last or len(items) >= limit:
            break
        kwargs["ExclusiveStartKey"] = last
    return from_dynamo(items[:limit])


def list_all_applications(limit: int | None = None) -> list[dict]:
    return _query_type(keys.TYPE_APP, settings.list_apps_max if limit is None else limit)


def list_all_machines(limit: int | None = None) -> list[dict]:
    return _query_type(keys.TYPE_MACHINE, settings.list_machines_max if limit is None else limit)


def list_latest_analysis_by_type(analysis_type: str, limit: int | None = None) -> list[dict]:
    """Newest ANALYSIS record of one type PER app, across every app (via GSI1).

    Unlike ``list_completed_analysis_by_type`` this does NOT filter on
    ``status == "completed"`` — some record types (e.g. ``assessment-summary``)
    do not carry a status attribute. Records share SK ``ANALYSIS#<type>#<iso-ts>``
    so the lexically-greatest SK per app_id is the newest. Never a table scan.
    """
    table = get_table()
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "IndexName": "GSI1",
        "KeyConditionExpression": KeyCond("GSI1PK").eq(keys.TYPE_ANALYSIS),
        "FilterExpression": Attr("analysis_type").eq(analysis_type),
    }
    limit = settings.list_analysis_max if limit is None else limit
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last or len(items) >= limit:
            break
        kwargs["ExclusiveStartKey"] = last

    latest: dict[str, dict] = {}
    for it in from_dynamo(items[:limit]):
        app_id = it.get("app_id")
        if not app_id:
            continue
        cur = latest.get(app_id)
        if cur is None or str(it.get("SK", "")) > str(cur.get("SK", "")):
            latest[app_id] = it
    return list(latest.values())
