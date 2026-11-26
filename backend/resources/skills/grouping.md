---
name: grouping
description: Application grouping skill — auto-groups discovered servers into logical applications using naming conventions, connection affinity, infrastructure patterns, and subnet analysis. Produces draft groups for customer refinement.
metadata:
  type: skill
  phase: grouping
  agent: discovery
---

# Skill: Server-to-Application Grouping

## When this skill fires

- After L1 discovery ingestion completes (AWS Application Discovery Service data loaded)
- User requests "group servers", "create applications", "auto-group"
- Called by the discovery agent when machines exist but no applications are defined

## Procedure

### Step 1 — Load all machines
Query all MACHINE# records from DynamoDB. Extract hostname, IP, OS, connections.

### Step 2 — Apply grouping rules (priority order)

#### Rule 1: Infrastructure / Shared Services
Servers running known infrastructure software are grouped into `INFRA`:
- **Monitoring**: nagios, splunk, arcsight, datadog, prometheus, grafana
- **Security**: crowdstrike, nessus, qualys, rapid7
- **Backup**: veeam, netbackup, commvault, veritas
- **Config management**: puppet, ansible, chef, sccm, salt
- **Licensing**: flexera, snow
- **Identity**: ldap, active directory
- **Network**: dns, ntp, proxy, jump, bastion
- **Tools prefix**: hostname starts with `cal-`, `tool-`, `mgmt-`

#### Rule 2: Multi-segment hyphenated with environment codes (ADS pattern)
Hostnames like `{app}-{env}-{role}-{num}` (e.g., `te-prd-app-01`, `he-uat-mysql-03`):
- Second segment is an environment code (prd, prod, uat, dev, stg, alp, alpha, bta, beta, qa, dr, perf)
- Group by **first segment only** (the app code) — all environments of same app = one group
- Examples: `te-prd-app-01`, `te-uat-mysql-02`, `te-dev-web-01` → all group to `TE`

#### Rule 3: Numbered-prefix pattern
Hostnames like `NNNN-{env}-{type}` (e.g., `0152-on-asp`, `6564-ab-asp`):
- Group by `{env}-{type}` suffix → application group
- These are typically identical app instances across different tenants/customers

#### Rule 4: Short hyphenated names
Hostnames like `prefix-role-instance` (e.g., `mcc-oracle-1`, `q-tps-mddbprod-10`):
- Use `{prefix}-{second_segment}` as group identifier
- If second segment is a number, use prefix only

#### Rule 5: Concatenated naming convention
Hostnames like `{app3}{role2-4}{env}{num}` (e.g., `paphcts10`, `emrmt13`, `qemrns24`):
- First 3 characters = application code
- Middle 2-4 characters = role code:
  - `eht` = Enterprise Hosting (app tier)
  - `hct` = Health Check/Tomcat (web tier)
  - `pbs` = PBS/Portal Backend Services
  - `phs` = Physical/Portal Hosting
  - `mss` = Messaging Services
  - `rmt` = Remote (terminal services)
  - `rpt` = Reporting
  - `rns` = RNS (network services)
  - `rnf` = RNF (notification)
  - `pst` = Persistent/Storage
  - `dat` = Database
  - `ept` = Endpoint
  - `sst` = Session State
  - `mst` = Master
  - `bit` = BIT (batch/integration)
  - `bzt` = BZT (business zone)
  - `imt` = Integration Middleware
  - `sk` = Skill/Worker nodes

#### Rule 6: Connection affinity (refinement)
After initial grouping, check ungrouped servers:
- If a server has 10+ connections to servers in a known group → assign to that group
- If a server connects equally to multiple groups → keep ungrouped for manual review

#### Rule 7: Subnet/Datacenter grouping (tie-breaker)
Servers in the same /24 subnet that share a naming pattern prefix:
- Likely belong to the same application environment

### Step 3 — Assign application IDs and names

For each group:
- `app_id`: `APP-{GROUP_PREFIX}` (e.g., `APP-PAP`, `APP-MCC-ORACLE`, `APP-NUM-ON-ASP`)
- `app_name`: `{GROUP_PREFIX} Application` (user can rename later)
- `machine_ids`: list of all machine IDs in this group
- `machine_count`: count

Infrastructure group:
- `app_id`: `APP-INFRA`
- `app_name`: `Infrastructure & Shared Services`

### Step 4 — Store results
- Create APP# META records in DynamoDB for each group
- Update MACHINE# records with `assigned_app_id` field
- Mark as `status: discovered` (not yet enriched)

## Output Contract

Each application group produces:
- `app_id` — stable identifier (never changes)
- `app_name` — display name (user can edit)
- `machine_ids` — list of assigned machines
- `machine_count` — server count
- `grouping_method` — how it was grouped (naming/connection/manual)
- `grouping_confidence` — high/medium/low
- `status` — discovered

## Quality Bar

- Every machine MUST belong to exactly one group (no orphans in final state)
- Infrastructure is always a separate group
- Groups with 50+ servers should be flagged for sub-grouping review
- Single-server groups are allowed but flagged as "needs review"

## User Refinement

After auto-grouping, user can:
- **Rename** — change `app_name` (app_id stays stable)
- **Merge** — combine two groups into one
- **Split** — break a large group into smaller ones
- **Move machines** — reassign servers between groups
- **Add machines** — assign ungrouped servers to a group
- **Create new group** — manually define a new application
- **Delete group** — dissolve (machines go to ungrouped)

## Anti-patterns

- Do NOT create one group per server (defeats purpose)
- Do NOT merge all Windows servers into one group
- Do NOT ignore connection data — it's the strongest signal after naming
- Do NOT overwrite user-refined groups on re-ingestion
