---
name: planning
description: Planning agent skill — generates dependency-aware wave plans from portfolio data. Uses application relationships (network connections, shared databases, integration points) to sequence migrations safely.
metadata:
  type: skill
  phase: planning
  agent: planning
  data_sources: [dynamodb, l1_discovery_connections]
---

# Skill: Wave Planning & Dependency Analysis

## When this skill fires

- ≥3 apps have completed assessment (strategy_confidence set)
- User clicks "Generate Wave Plan" on the Wave Planning page
- Portfolio-level assessment triggered

## Data Sources for Relationship Discovery

### L1 Discovery — ADS (AWS Application Discovery Service)
Full export provides the richest relationship data:

**server_inventory.csv** (77-137 servers):
- Server identity, OS, CPU, RAM, hypervisor
- Used to create Machine nodes in the graph

**network_data_full.csv** (44-1018 connections):
- Source Server ID → Target Server ID (with IPs)
- Target Port — identifies service type (1521=Oracle, 8080=Java, 443=HTTPS)
- Source/Target Process Name — identifies WHAT is communicating (java, oracle, httpd)
- Count — connection frequency (higher = stronger dependency)
- State — LISTEN, ESTABLISHED, TIME_WAIT

**process_metrics.csv** (31K+ records):
- Running processes per server (process name, user, command line)
- Auto-detects: java, python, node, oracle, mysql, postgres, tomcat, weblogic, nginx
- Stored as `detected_software` on each Machine node
- Used to infer application type without needing L2 enrichment

**server_storage_performance.csv** (per-server):
- Per-disk mount point, filesystem, total/used/free GB
- Read/Write IOPS avg + peak per disk
- Critical for EBS sizing: if peak IOPS > 3000 → io2 required, else gp3

**network_interfaces.csv** (123 NICs):
- Multi-NIC servers, VLAN assignments, subnet membership
- Gateway and DNS configuration
- Identifies network segmentation (which servers share a VLAN = same security zone)

**vmware_data_full.json** (28-day performance window):
- 137 VMs with CPU/RAM/Disk performance over 28 days
- Max, min, avg utilization — for right-sizing
- VM-to-host mapping — identifies hypervisor clusters

### L2 Discovery — Declared Dependencies
- `dependencies.upstream` — apps this app explicitly depends on
- `dependencies.downstream` — apps that depend on this app
- `dependencies.shared_db` — shared database flag (migration blocker)
- `integrations` — named services/APIs (kafka, redis, payment-gateway)

### Assessment — Code-Level Dependencies
- ATX dependency analysis → import/API call graphs
- Shared libraries, shared schemas
- Cross-repo references

## Relationship Types (graph model)

```
// Core relationships
(:Application)-[:DEPENDS_ON]->(:Application)       # Declared dependency (L2)
(:Application)-[:COMMUNICATES_WITH]->(:Application) # Inferred from network (L1)
(:Application)-[:SHARES_DATABASE]->(:Database)      # Shared DB (strongest blocker)
(:Application)-[:INTEGRATES_WITH]->(:Service)       # External integration

// Infrastructure relationships
(:Machine)-[:HOSTS]->(:Application)                 # Machine assignment
(:Machine)-[:CONNECTS_TO {port, protocol, process, count}]->(:Machine) # Network flow (L1)
(:Machine)-[:IN_VLAN]->(:VLAN)                      # Network segment (ADS network_interfaces)
(:Machine)-[:RUNS]->(:Software)                     # Detected software (ADS process_metrics)

// Organizational
(:Application)-[:BELONGS_TO]->(:BusinessUnit)
(:Application)-[:ASSIGNED_TO]->(:Wave)              # Wave assignment (output)
(:Application)-[:OWNED_BY]->(:Owner)

// ADS-specific inferred relationships
(:Machine)-[:TALKS_TO_DB {port: 1521}]->(:Machine)  # Oracle dependency
(:Machine)-[:TALKS_TO_DB {port: 3306}]->(:Machine)  # MySQL dependency
(:Machine)-[:TALKS_TO_DB {port: 5432}]->(:Machine)  # PostgreSQL dependency
(:Machine)-[:TALKS_TO_DB {port: 1433}]->(:Machine)  # SQL Server dependency
(:Machine)-[:WEB_CALL {port: 443|80|8080}]->(:Machine) # HTTP/API call
(:Machine)-[:SHARES_STORAGE]->(:Machine)            # SMB/NFS (port 445/2049)
```

## Wave Assignment Procedure

> **Deterministic in code.** `generate_wave_plan` (Harness tool) and the portal's Generate button run
> `backend/src/core/wave_planner.py`, which implements every step below: eligibility, the 3-axis scores, bands with
> size limits, the hard rules, cycles and conflicts. Call the tool and explain its output (each app's `reason`, plus
> `not_ready` and `conflicts`). **Do not** re-assign waves by hand in your answer. If the plan looks wrong, the inputs
> are wrong (strategy, tier, `customer_facing`, dependencies), so name the field to fix.

### Step 1 — Build the dependency graph
For each application:
- Load declared dependencies (upstream/downstream)
- Load inferred dependencies from network connections (L1 data)
- Identify shared databases (strongest dependency type)
- Calculate dependency score: inbound edges + outbound edges

### Step 2 — Score each app on 3 axes

**Risk score (0-10):** higher = riskier
- +3 if tier_1 (critical)
- +2 if peak_tps > 1000 (high traffic)
- +2 if shared_db = true
- +2 if compliance contains PCI/SOX/HIPAA
- +1 if multi_region required

**Complexity score (0-10):** higher = harder
- +3 if strategy = refactor
- +2 if database.engine in [Oracle, DB2] (complex migration)
- +2 if data_volume_gb > 500
- +1 if outbound_dependencies > 5
- +1 per known risk factor
- +1 if strategy_confidence < 60%

**Business priority (1-3):**
- 1 = must-move (EOSL, DC lease expiry, license renewal)
- 2 = should-move (cost savings, modernization mandate)
- 3 = could-move (opportunistic)
- Auto-derive: if constraints.hard_deadline exists → priority 1
- Auto-derive: if annual_license > 50000 → priority 1 or 2
- Auto-derive: if active_development = false AND tier_3 → priority 3

### Step 3 — Wave assignment rules (in priority order)

| Wave | Label | Criteria | Max Apps |
|------|-------|----------|----------|
| 0 | Pilot | Lowest risk × lowest complexity × zero upstream deps × tier_3 | 1-3 |
| 1 | Foundation | Stateless services, shared infrastructure (LDAP, DNS, monitoring) | 5-8 |
| 2 | Core | Business-critical but not customer-facing (billing, rating, internal APIs) | 8-12 |
| 3 | Customer-Facing | Portals, self-service, customer APIs | 5-10 |
| 4 | Long Tail | Retire candidates, blocked apps, complex dependencies | Remaining |

**Hard rules:**
- If app A depends on app B → A.wave ≥ B.wave + 1 (never co-wave dependent before dependency)
- If shared_db between A and B → same wave (migrate together) OR B.wave = A.wave - 1 (DB first)
- Never > 30% of tier_1 apps in same wave (spread risk)
- Customer-facing apps NEVER in wave 0 or 1
- Retire/Retain apps go to wave 4 (or excluded)

### Step 4 — Cycle resolution
- Cycles ≤ 3 apps → co-wave with coordinated cutover
- Cycles > 3 → flag as strangler-fig candidate (decouple first)

### Step 5 — Gate criteria between waves
Each wave has explicit entry criteria:
- Wave N starts only when ALL apps in Wave N-1 are stable for 2 weeks
- "Stable" = health checks passing, error rate < baseline, no rollbacks in progress
- Exception: pilot wave (wave 0) can start immediately

## Output Contract

```json
{
  "waves": [
    {
      "wave_number": 0,
      "label": "Pilot",
      "apps": [
        {"app_id": "...", "app_name": "...", "rationale": "...", "strategy": "..."}
      ],
      "estimated_duration_weeks": 4,
      "gate_criteria": ["All pilot apps stable 2 weeks", "Runbook validated"]
    }
  ],
  "dependency_graph": {
    "nodes": [...],
    "edges": [{"from": "APP-X", "to": "APP-Y", "type": "communicates_with", "weight": 150}]
  },
  "deferred": [{"app_id": "...", "reason": "Circular dependency, needs decoupling"}],
  "risk_summary": {
    "total_waves": 5,
    "estimated_total_weeks": 20,
    "highest_risk_apps": [...],
    "migration_blockers": [...]
  }
}
```

## Quality Bar

- Every wave has explicit gate criteria
- No wave > 15 apps
- Wave 0 has 1-3 apps, no exceptions
- Customer-facing never in wave 0/1
- Shared DB apps are co-waved or sequenced correctly
- Dependency violations = 0 (never migrate before your dependency)
- Cycles explicitly called out (not hidden)
- Deferred apps have clear reason

## Anti-Patterns

- Do NOT pick the most important app for the pilot. Pilot = least risky.
- Do NOT generate a wave plan without gate criteria.
- Do NOT ignore shared_db flag — it's the strongest blocker.
- Do NOT silently drop apps. Everything goes into a wave or into deferred.
- Do NOT put retire/retain apps in early waves — they don't need to move.
