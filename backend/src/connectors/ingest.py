"""
Discovery data ingestion pipeline.
Takes canonical discovery data (from any adapter) and loads it into DynamoDB.

Flow:
  1. Adapter parses source files → canonical format
  2. This module maps canonical → DynamoDB portfolio model
  3. Creates Machine records, infers Applications from naming patterns
  4. Stores network connections as dependency edges
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from backend.src.config import settings
from backend.src.db.helpers import from_dynamo, get_table, to_dynamo

logger = logging.getLogger(__name__)


def ingest_discovery_data(data: dict, source: str = "aws_ads") -> dict:
    """Ingest canonical discovery data into DynamoDB.

    Uses batch_writer for fast bulk writes (25 items per batch).

    Args:
        data: Dict with keys: servers, connections, databases, performance
        source: Source identifier (e.g. "aws_ads")

    Returns:
        Summary of what was ingested.
    """
    table = get_table()
    now = datetime.now(UTC).isoformat()

    servers = data.get("servers", [])
    connections = data.get("connections", [])
    databases = data.get("databases", [])

    # ── Step 1: Create Machine records (batch write) ───────────────────
    machines_created = 0
    server_id_to_machine_id = {}

    with table.batch_writer() as batch:
        for server in servers:
            machine_id = _generate_machine_id(server)
            server_id_to_machine_id[server.get("server_id", "")] = machine_id

            machine_item = {
                "PK": f"MACHINE#{machine_id}",
                "SK": "META",
                "GSI1PK": "TYPE#MACHINE",
                "GSI1SK": machine_id,
                "machine_id": machine_id,
                "hostname": server.get("hostname", ""),
                "ip_address": server.get("ip_address", ""),
                "environment": server.get("environment", "prod"),
                "os_name": server.get("os_name", ""),
                "os_version": server.get("os_version", ""),
                "cpu_cores": server.get("cpu_cores", 0),
                "memory_gb": server.get("memory_gb", 0),
                "storage_gb": server.get("storage_gb", 0),
                "storage_free_gb": server.get("storage_free_gb", 0),
                "virtualization": server.get("virtualization", ""),
                "hypervisor_host": server.get("hypervisor_host", server.get("vmware_host", "")),
                "avg_cpu_utilization": server.get("avg_cpu_utilization", 0),
                "peak_cpu_utilization": server.get("peak_cpu_utilization", 0),
                "cpu_p95": server.get("cpu_p95", 0),
                "avg_memory_utilization": server.get("avg_memory_utilization", 0),
                "peak_memory_utilization": server.get("peak_memory_utilization", 0),
                "disk_iops_avg": server.get("disk_iops_avg", 0),
                "storage_max_read_iops": server.get("storage_max_read_iops", 0),
                "storage_max_write_iops": server.get("storage_max_write_iops", 0),
                "network_rx_max_kbps": server.get("network_rx_max_kbps", 0),
                "network_tx_max_kbps": server.get("network_tx_max_kbps", 0),
                "reads_avg_iops": server.get("reads_avg_iops", 0),
                "reads_max_iops": server.get("reads_max_iops", 0),
                "writes_avg_iops": server.get("writes_avg_iops", 0),
                "writes_max_iops": server.get("writes_max_iops", 0),
                "reads_p95_kbps": server.get("reads_p95_kbps", 0),
                "writes_p95_kbps": server.get("writes_p95_kbps", 0),
                "dense_io_gb": server.get("dense_io_gb", 0),
                "queue_max": server.get("queue_max", 0),
                "ec2_recommendation": server.get("ec2_recommendation", ""),
                "ec2_min_shared": server.get("ec2_min_shared", ""),
                "ec2_max_shared": server.get("ec2_max_shared", ""),
                "ec2_dedicated": server.get("ec2_dedicated", ""),
                "rds_recommendation": server.get("rds_recommendation", ""),
                "suggested_strategy": server.get("suggested_strategy", ""),
                "cpu_description": server.get("cpu_description", ""),
                "deps_all": server.get("deps_all", 0),
                "deps_non_infra": server.get("deps_non_infra", 0),
                "power_state": server.get("power_state", "Running"),
                "dns_name": server.get("dns_name", ""),
                "network_name": server.get("network_name", ""),
                "switch": server.get("switch", ""),
                "mac_address": server.get("mac_address", ""),
                "disk_paths": server.get("disk_paths", []),
                "vmware_tools_status": server.get("vmware_tools_status", ""),
                "discovery_source": source,
                "discovered_at": now,
                "created_at": now,
                "source_server_id": server.get("server_id", ""),
            }
            batch.put_item(Item=to_dynamo(machine_item))
            machines_created += 1

    logger.info("Machines: %d created", machines_created)

    # ── Step 2: Create Database software records (batch) ───────────────
    dbs_created = 0
    seen_db_keys = set()
    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for db in databases:
            server_id = db.get("server_id", "")
            machine_id = server_id_to_machine_id.get(server_id)
            if not machine_id:
                for sid, mid in server_id_to_machine_id.items():
                    if server_id.startswith(sid.split(":")[0]):
                        machine_id = mid
                        break
            if not machine_id:
                continue

            sw_key = f"{db.get('engine_type', 'unknown')}#{db.get('instance_name', '')}"
            full_key = f"MACHINE#{machine_id}|SW#{sw_key}"
            if full_key in seen_db_keys:
                continue
            seen_db_keys.add(full_key)

            sw_item = {
                "PK": f"MACHINE#{machine_id}",
                "SK": f"SW#{sw_key}",
                "product": db.get("engine_type", ""),
                "vendor": _infer_vendor(db.get("engine_type", "")),
                "version": db.get("version", ""),
                "edition": db.get("edition", ""),
                "category": "database",
                "is_licensable": True,
                "instance_name": db.get("instance_name", ""),
                "port": db.get("port", ""),
                "status": db.get("status", ""),
                "is_clustered": db.get("is_clustered", False),
            }
            batch.put_item(Item=to_dynamo(sw_item))
            dbs_created += 1

    logger.info("Created %d database software records", dbs_created)

    # ── Step 3: Infer applications from hostname patterns ──────────────
    apps = _infer_applications(servers, databases)
    apps_created = 0
    apps_skipped = 0

    for app in apps:
        app_id = app["app_id"]
        machine_ids = app.get("machine_ids", [])

        existing = table.get_item(Key={"PK": f"APP#{app_id}", "SK": "META"})

        if "Item" in existing:
            existing_item = from_dynamo(existing["Item"])
            existing_machines = set(existing_item.get("machine_ids", []))
            new_machines = set(machine_ids) - existing_machines
            if new_machines:
                merged_machines = list(existing_machines | set(machine_ids))
                table.update_item(
                    Key={"PK": f"APP#{app_id}", "SK": "META"},
                    UpdateExpression="SET machine_ids = :mids, machine_count = :mc, updated_at = :ua",
                    ExpressionAttributeValues=to_dynamo(
                        {
                            ":mids": merged_machines,
                            ":mc": len(merged_machines),
                            ":ua": now,
                        }
                    ),
                )
            apps_skipped += 1
            continue

        app_item = {
            "PK": f"APP#{app_id}",
            "SK": "META",
            "GSI1PK": "TYPE#APP",
            "GSI1SK": app_id,
            "app_id": app_id,
            "app_name": app.get("app_name", ""),
            "description": app.get("description", ""),
            "status": "discovered",
            "tier": "tier_2",
            "language": app.get("language", ""),
            "framework": app.get("framework", ""),
            "architecture": "monolith",
            "machine_ids": machine_ids,
            "machine_count": len(machine_ids),
            "discovery_source": source,
            "discovered_at": now,
            "created_at": now,
        }
        table.put_item(Item=to_dynamo(app_item))
        apps_created += 1

    logger.info("Applications: %d created, %d skipped", apps_created, apps_skipped)

    # ── Step 4: Store network connections (batch, top 500) ─────────────
    connections_stored = 0
    top_connections = sorted(connections, key=lambda c: c.get("connection_count", 0), reverse=True)[:500]
    seen_conn_keys = set()

    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for conn in top_connections:
            source_machine = server_id_to_machine_id.get(conn.get("source_server_id", ""), "")
            target_machine = server_id_to_machine_id.get(conn.get("target_server_id", ""), "")
            if not source_machine or not target_machine:
                continue

            conn_id = hashlib.sha256(
                f"{source_machine}:{conn.get('source_ip')}:{conn.get('target_ip')}:{conn.get('target_port')}".encode()
            ).hexdigest()[:12]

            full_key = f"MACHINE#{source_machine}|CONN#{conn_id}"
            if full_key in seen_conn_keys:
                continue
            seen_conn_keys.add(full_key)

            conn_item = {
                "PK": f"MACHINE#{source_machine}",
                "SK": f"CONN#{conn_id}",
                "source_machine_id": source_machine,
                "source_ip": conn.get("source_ip", ""),
                "target_machine_id": target_machine,
                "target_ip": conn.get("target_ip", ""),
                "target_port": conn.get("target_port", ""),
                "protocol": conn.get("protocol", ""),
                "source_process": conn.get("source_process", ""),
                "target_process": conn.get("target_process", ""),
                "connection_count": conn.get("connection_count", 0),
            }
            batch.put_item(Item=to_dynamo(conn_item))
            connections_stored += 1

    logger.info("Stored %d network connections", connections_stored)

    return {
        "machines_created": machines_created,
        "machines_updated": 0,
        "databases_created": dbs_created,
        "applications_inferred": apps_created,
        "applications_skipped": apps_skipped,
        "connections_stored": connections_stored,
        "source": source,
    }


# ── Helpers ────────────────────────────────────────────────────────────


def _generate_machine_id(server: dict) -> str:
    """Generate a stable machine ID from server data."""
    hostname = server.get("hostname", "")
    if hostname:
        return f"M-{hashlib.sha256(hostname.encode()).hexdigest()[:8].upper()}"
    return f"M-{uuid.uuid4().hex[:8].upper()}"


def _infer_applications(servers: list[dict], databases: list[dict]) -> list[dict]:
    """Infer application groupings from hostname naming patterns.

    Common patterns:
      - te-prd-app-01, te-prd-app-02 → app "TE" (environment: prd)
      - he-prd-app-03 → app "HE"
      - TE-UAT-MYSQL-01 → app "TE" (environment: uat, role: mysql)
    """
    # Group servers by prefix (first segment of hostname)
    app_groups: dict[str, list[dict]] = {}

    for server in servers:
        hostname = server.get("hostname", "").lower()
        if not hostname:
            continue

        # Classify into group
        group = _classify_hostname(hostname)
        if group not in app_groups:
            app_groups[group] = []
        app_groups[group].append(server)

    # Create application records
    apps = []
    ungrouped_servers = []

    for group_name, group_servers in app_groups.items():
        if group_name == "UNGROUPED" or len(group_servers) < 2:
            ungrouped_servers.extend(group_servers)
            continue

        app_id = f"APP-{group_name}"
        machine_ids = [_generate_machine_id(s) for s in group_servers]

        # Determine grouping confidence
        if len(group_servers) >= settings.ingest_group_high_confidence_servers:
            confidence = "high"
        elif len(group_servers) >= settings.ingest_group_medium_confidence_servers:
            confidence = "medium"
        else:
            confidence = "low"

        # Determine category
        is_infra = group_name == "INFRA"

        apps.append(
            {
                "app_id": app_id,
                "app_name": f"{'Infrastructure & Shared Services' if is_infra else group_name + ' Application'}",
                "description": f"Auto-grouped from {len(group_servers)} server(s) — pattern: '{group_name.lower()}'",
                "machine_ids": machine_ids,
                "grouping_method": "naming_convention",
                "grouping_confidence": confidence,
                "language": "",
                "framework": "",
            }
        )

    # Put remaining ungrouped into a single catch-all
    if ungrouped_servers:
        machine_ids = [_generate_machine_id(s) for s in ungrouped_servers]
        apps.append(
            {
                "app_id": "APP-UNGROUPED",
                "app_name": "Ungrouped Servers (Needs Review)",
                "description": f"{len(ungrouped_servers)} servers could not be auto-grouped. Assign manually.",
                "machine_ids": machine_ids,
                "grouping_method": "ungrouped",
                "grouping_confidence": "low",
                "language": "",
                "framework": "",
            }
        )

    return apps


# ── Infrastructure keywords ────────────────────────────────────────────

_INFRA_KEYWORDS = [
    "sccm",
    "flexera",
    "arcsight",
    "nessus",
    "crowdstrike",
    "netbackup",
    "backup",
    "monitoring",
    "nagios",
    "splunk",
    "ldap",
    "dns",
    "ntp",
    "proxy",
    "jump",
    "bastion",
    "veeam",
    "puppet",
    "license",
    "oms",
    "ansible",
    "chef",
    "qualys",
    "rapid7",
    "datadog",
]


def _classify_hostname(hostname: str) -> str:
    """Classify a hostname into an application group.

    Rules (priority order):
    1. Infrastructure/shared services (known tool names)
    2. Multi-segment hyphenated with environment codes (app-env-role-num) → group by app
    3. Numbered-prefix pattern (NNNN-env-type)
    4. Short hyphenated (prefix-descriptor-num)
    5. Concatenated names (app3 + role2-4 + num)
    """
    import re

    h = hostname.lower()

    # Rule 1: Infrastructure
    for kw in _INFRA_KEYWORDS:
        if kw in h:
            return "INFRA"
    if h.startswith("cal-"):
        return "INFRA"

    # Rule 2: Multi-segment hyphenated with known environment codes
    # Pattern: {app}-{env}-{role}-{num} e.g., te-prd-app-01, he-uat-mysql-03
    _ENV_CODES = {"prd", "prod", "uat", "dev", "stg", "staging", "alp", "alpha", "bta", "beta", "qa", "dr", "perf"}
    if "-" in h:
        parts = h.split("-")
        if len(parts) >= 3 and parts[1] in _ENV_CODES:
            # Pattern: app-env-role-num → group by app prefix
            return parts[0].upper()
        if len(parts) >= 4 and parts[2] in _ENV_CODES:
            # Pattern: site-app-env-role → group by first two segments
            return f"{parts[0].upper()}-{parts[1].upper()}"

    # Rule 3: Numbered-prefix (e.g., 0152-on-asp, 6564-ab-asp)
    if re.match(r"^\d{4}-", h):
        parts = h.split("-")
        if len(parts) >= 3:
            return f"NUM-{parts[1].upper()}-{parts[2].upper()}"
        return "NUM-APPS"

    # Rule 4: Short hyphenated (mcc-oracle-1, q-tps-mddbprod-10, dm-db06)
    if "-" in h:
        parts = h.split("-")
        prefix = parts[0]
        if len(parts) >= 2 and not parts[1].isdigit():
            return f"{prefix.upper()}-{parts[1].upper()}"
        return prefix.upper()

    # Rule 5: Concatenated — first 3 chars = app code
    match = re.match(r"^([a-z]{3})", h)
    if match:
        return match.group(1).upper()

    return "UNGROUPED"


def _infer_vendor(engine_type: str) -> str:
    """Infer vendor from database engine type."""
    engine_lower = engine_type.lower()
    if "sql_server" in engine_lower or "mssql" in engine_lower:
        return "Microsoft"
    if "oracle" in engine_lower:
        return "Oracle"
    if "mysql" in engine_lower:
        return "Oracle"
    if "postgres" in engine_lower:
        return "PostgreSQL"
    return ""
