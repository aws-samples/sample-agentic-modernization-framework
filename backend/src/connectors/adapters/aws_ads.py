"""
AWS Application Discovery Service (ADS) adapter.
Parses discovery_tool_export data into canonical portfolio model.

Handles both formats:
  - Full export (10 files: server_inventory, performance, network, etc.)
  - Minimal export (3 files: vmware_data, network, database + MPA CSVs)

Gracefully degrades — missing files are skipped.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from backend.src.connectors.adapters.paths import safe_path

logger = logging.getLogger(__name__)


def parse_discovery_export(export_dir: str | Path) -> dict:
    """Parse an ADS discovery export directory into canonical format.

    Args:
        export_dir: Path to the export directory (contains full_exports/ and mpa_exports/)

    Returns:
        Dict with keys: servers, connections, databases, performance
    """
    export_dir = Path(export_dir).resolve()
    full_dir = safe_path(export_dir, export_dir / "full_exports")
    mpa_dir = safe_path(export_dir, export_dir / "mpa_exports")

    result = {
        "servers": [],
        "connections": [],
        "databases": [],
        "performance": {},
    }

    # ── Parse server inventory (full export) ───────────────────────────
    server_inv = safe_path(export_dir, full_dir / "server_inventory.csv")
    if server_inv.exists():
        result["servers"] = _parse_server_inventory(server_inv)
        logger.info("Parsed %d servers from server_inventory.csv", len(result["servers"]))

    # ── Fallback: Parse MPA VMware export for servers ──────────────────
    if not result["servers"]:
        mpa_vmware = safe_path(export_dir, mpa_dir / "vmware_data_mpa.csv")
        if mpa_vmware.exists():
            result["servers"] = _parse_mpa_vmware(mpa_vmware)
            logger.info("Parsed %d servers from vmware_data_mpa.csv", len(result["servers"]))

    # ── Parse performance metrics ──────────────────────────────────────
    perf_file = safe_path(export_dir, full_dir / "server_performance_metrics.csv")
    if perf_file.exists():
        result["performance"] = _parse_performance_metrics(perf_file)
        logger.info("Parsed performance for %d servers", len(result["performance"]))
    elif result["servers"]:
        # MPA data already has performance embedded in server records
        pass

    # ── Parse network connections ──────────────────────────────────────
    net_file = safe_path(export_dir, full_dir / "network_data_full.csv")
    if net_file.exists():
        result["connections"] = _parse_network_connections(net_file)
        logger.info("Parsed %d network connections", len(result["connections"]))

    # ── Parse database instances ───────────────────────────────────────
    db_file = safe_path(export_dir, full_dir / "database_data_full.csv")
    if db_file.exists():
        result["databases"] = _parse_databases(db_file)
        logger.info("Parsed %d database instances", len(result["databases"]))

    # ── Enrich servers with performance data ───────────────────────────
    if result["performance"]:
        for server in result["servers"]:
            sid = server.get("server_id", "")
            if sid in result["performance"]:
                server.update(result["performance"][sid])

    # ── Parse VMware JSON for additional detail ────────────────────────
    vmware_json = safe_path(export_dir, full_dir / "vmware_data_full.json")
    if vmware_json.exists():
        _enrich_from_vmware_json(vmware_json, result["servers"])

    return result


# ── Parsers ────────────────────────────────────────────────────────────


def _parse_server_inventory(path: Path) -> list[dict]:
    """Parse server_inventory.csv into canonical server records."""
    servers = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            servers.append(
                {
                    "server_id": row.get("server_id", ""),
                    "hostname": row.get("server_name", ""),
                    "ip_address": row.get("primary_ip_address", ""),
                    "os_name": row.get("os_name", ""),
                    "os_version": row.get("os_version", ""),
                    "cpu_cores": _int(row.get("cpu_count", 0)),
                    "memory_gb": _float(row.get("total_memory_gb", 0)),
                    # server_inventory.csv carries this column too — it was only
                    # mapped in the MPA parser, so an ADS-format import produced
                    # machines with no storage and the UI showed 0.0 TB total.
                    "storage_gb": _float(row.get("Storage-Total Disk Size (GB)", 0)),
                    "virtualization": row.get("hypervisor_type", ""),
                    "hypervisor_host": row.get("hypervisor_hostname", ""),
                    "environment": row.get("environment", ""),
                    "power_state": row.get("power_state", ""),
                    "disk_iops_avg": _float(row.get("disk_total_iops_avg", 0)),
                    "disk_iops_peak": _float(row.get("disk_total_iops_peak", 0)),
                    "disk_throughput_avg_mbps": _float(row.get("disk_total_throughput_avg_mbps", 0)),
                    "network_cards": _int(row.get("total_num_network_cards", 0)),
                    "num_disks": _int(row.get("total_num_disks", 0)),
                }
            )
    return servers


def _parse_mpa_vmware(path: Path) -> list[dict]:
    """Parse vmware_data_mpa.csv (simplified format with utilization)."""
    servers = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            servers.append(
                {
                    "server_id": row.get("Serverid", ""),
                    "hostname": row.get("HOSTNAME", ""),
                    "os_name": row.get("osName", ""),
                    "os_version": row.get("osVersion", ""),
                    "cpu_cores": _int(row.get("numCpus", 0)),
                    "cores_per_cpu": _int(row.get("numCoresPerCpu", 0)),
                    "memory_gb": _float(row.get("totalRAM (GB)", 0)),
                    "virtualization": row.get("hypervisor", ""),
                    "is_physical": row.get("isPhysical", "Virtual") == "Physical",
                    "avg_cpu_utilization": _float(row.get("avgCpuUsagePctDec (%)", 0)),
                    "peak_cpu_utilization": _float(row.get("maxCpuUsagePctDec (%)", 0)),
                    "avg_memory_utilization": _float(row.get("avgRamUtlPctDec (%)", 0)),
                    "peak_memory_utilization": _float(row.get("maxRamUsagePctDec (%)", 0)),
                    "storage_gb": _float(row.get("Storage-Total Disk Size (GB)", 0)),
                    "storage_max_read_iops": _float(row.get("Storage-Max Read IOPS", 0)),
                    "storage_max_write_iops": _float(row.get("Storage-Max Write IOPS", 0)),
                }
            )
    return servers


def _parse_performance_metrics(path: Path) -> dict[str, dict]:
    """Parse server_performance_metrics.csv into a dict keyed by server_id."""
    perf = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sid = row.get("server_id", "")
            perf[sid] = {
                "avg_cpu_utilization": _float(row.get("cpu_utilization_avg_pct", 0)),
                "peak_cpu_utilization": _float(row.get("cpu_utilization_peak_pct", 0)),
                "avg_memory_utilization": _float(row.get("memory_utilization_avg_pct", 0)),
                "peak_memory_utilization": _float(row.get("memory_utilization_peak_pct", 0)),
                "network_in_avg_mbps": _float(row.get("network_in_avg_mbps", 0)),
                "network_out_avg_mbps": _float(row.get("network_out_avg_mbps", 0)),
            }
    return perf


def _parse_network_connections(path: Path) -> list[dict]:
    """Parse network_data_full.csv into canonical connection records."""
    connections = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            connections.append(
                {
                    "source_server_id": row.get("Source Server ID", ""),
                    "source_ip": row.get("Source Server IP Address", ""),
                    "source_port": row.get("Source Port", ""),
                    "target_server_id": row.get("Target Server ID", ""),
                    "target_ip": row.get("Target Server IP Address", ""),
                    "target_port": row.get("Target Port", ""),
                    "source_process": row.get("Source Process Name", ""),
                    "target_process": row.get("Target Process Name", ""),
                    "protocol": row.get("Transport Protocol", ""),
                    "state": row.get("State", ""),
                    "connection_count": _int(row.get("Count", 0)),
                }
            )
    return connections


def _parse_databases(path: Path) -> list[dict]:
    """Parse database_data_full.csv into canonical database records."""
    databases = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("Engine Type"):
                continue
            databases.append(
                {
                    "server_id": row.get("Server ID", ""),
                    "engine_type": row.get("Engine Type", ""),
                    "instance_name": row.get("Instance Name", ""),
                    "version": row.get("Version", ""),
                    "edition": row.get("Edition", ""),
                    "status": row.get("Status", ""),
                    "is_clustered": row.get("Is Clustered", "N") == "Y",
                    "port": row.get("Server Connection Port", ""),
                    "hostname": row.get("Server Connection Address", ""),
                    "install_path": row.get("Install Path", ""),
                    "data_path": row.get("Data Path", ""),
                }
            )
    return databases


def _enrich_from_vmware_json(path: Path, servers: list[dict]):
    """Enrich server records with VMware-specific data (IP, host, disk, network)."""
    try:
        with open(path, encoding="utf-8") as f:
            vmware_data = json.load(f)

        # Build lookups by server_id and hostname
        server_by_id = {s["server_id"]: s for s in servers}
        server_by_hostname = {s.get("hostname", "").lower(): s for s in servers if s.get("hostname")}

        for vm_id, vm_data in vmware_data.items():
            info = vm_data.get("info", {})
            vm_name = info.get("vm_name", "").lower()

            # Match by server_id first, then by hostname
            server = server_by_id.get(vm_id) or server_by_hostname.get(vm_name)
            if not server:
                continue

            # Basic info
            server["vmware_host"] = info.get("host", "")
            server["vmware_tools_os"] = info.get("os_according_to_the_vmware_tools", "")
            server["total_disk_capacity_mb"] = info.get("total_disk_capacity_mib", 0)
            server["dns_name"] = info.get("dns_name", "")
            server["power_state"] = info.get("powerstate", "")
            if not server.get("ip_address"):
                server["ip_address"] = info.get("primary_ip_address", "")

            # Network/VLAN info
            networks = vm_data.get("network", [])
            if isinstance(networks, list) and networks:
                net = networks[0] if isinstance(networks[0], dict) else {}
                server["network_name"] = net.get("network", "")
                server["switch"] = net.get("switch", "")
                server["mac_address"] = net.get("mac_address", "")
                server["ipv6_address"] = net.get("ipv6_address", "")

            # Disk paths
            disks = vm_data.get("disk", [])
            if isinstance(disks, list) and disks:
                disk_paths = []
                for d in disks:
                    if isinstance(d, dict) and d.get("disk_path"):
                        disk_paths.append(d["disk_path"])
                if disk_paths:
                    server["disk_paths"] = disk_paths

            # Partition free space
            partitions = vm_data.get("partition", [])
            if isinstance(partitions, list) and partitions:
                total_free_mb = 0
                for p in partitions:
                    if isinstance(p, dict) and p.get("free_mib"):
                        try:
                            total_free_mb += int(p["free_mib"])
                        except (ValueError, TypeError):
                            pass
                if total_free_mb > 0:
                    server["storage_free_gb"] = round(total_free_mb / 1024, 1)

            # Tools status
            tools = vm_data.get("tools", {})
            if isinstance(tools, dict):
                server["vmware_tools_status"] = tools.get("tools", "")

    except Exception as exc:
        logger.warning("Failed to parse vmware_data_full.json: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────


def _int(val) -> int:
    try:
        return int(float(val)) if val else 0
    except (ValueError, TypeError):
        return 0


def _float(val) -> float:
    try:
        return round(float(val), 2) if val else 0.0
    except (ValueError, TypeError):
        return 0.0
