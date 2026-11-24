"""Deterministic wave planner implementing backend/resources/skills/planning.md.

PURE: no backend imports and no I/O. Every tunable is passed in, so the tool_planning Lambda zips this
file and runs the same code as the API (one implementation, no prompt-dependence).

    plan = generate_wave_plan(apps, connections, config=PlannerConfig(...))

Inputs
- apps: META dicts (app_id, app_name, tier, migration_strategy, machine_ids, depends_on,
  dependencies_upstream/_downstream, customer_facing, peak_tps, compliance, …).
- connections: machine CONN# records (source_machine_id, target_machine_id, target_port).

Output: {waves, not_ready, conflicts, rules, summary, app_dependencies}.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

CLOSE_OUT = {"retire", "retain", "repurchase"}
DB_PORTS = {"1521", "3306", "5432", "1433", "27017"}

BANDS = {
    0: ("Pilot", "tier_3, not customer-facing, no upstream dependencies; lowest risk + complexity"),
    1: ("Foundation", "Other non-critical, non-customer-facing apps (stateless services, shared infrastructure)"),
    2: ("Core", "Business-critical (tier_1/tier_2) apps that are not customer-facing"),
    3: ("Customer-Facing", "Portals, self-service, customer APIs"),
}
LONG_TAIL = ("Long Tail", "Close-outs (retire/retain/repurchase), blocked apps, complex dependency cycles")


@dataclass
class PlannerConfig:
    pilot_size: int = 3
    band_limits: dict[int, int] = field(default_factory=lambda: {0: 3, 1: 8, 2: 12, 3: 10})
    spill_limit: int = 10              # capacity of waves above the defined bands
    tier1_max_share: float = 0.30
    max_cycle_cowave: int = 3          # cycles up to this size share a wave; larger → Long Tail
    max_waves: int = 20                # safety bound on spill


def _s(v: Any) -> str:
    return str(v or "").strip()


def _truthy(v: Any) -> bool:
    return v is True or _s(v).lower() in ("true", "yes", "1", "y")


def _num(v: Any) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
    return []


# ── dependencies ────────────────────────────────────────────────────────────

def build_dependencies(apps: list[dict], connections: list[dict]) -> tuple[dict[str, set[str]], set[frozenset]]:
    """(deps app→{apps it depends on}, shared-DB pairs). Sources: CONN#, depends_on, L2 upstream/downstream."""
    ids = {a["app_id"] for a in apps if a.get("app_id")}
    by_name = {_s(a.get("app_name")).lower(): a["app_id"] for a in apps if a.get("app_id") and a.get("app_name")}

    def resolve(ref: Any) -> str:
        r = _s(ref)
        if r in ids:
            return r
        return by_name.get(r.lower(), "")

    m2a: dict[str, str] = {}
    for a in apps:
        for mid in a.get("machine_ids") or []:
            m2a.setdefault(str(mid), a["app_id"])

    deps: dict[str, set[str]] = defaultdict(set)
    shared: set[frozenset] = set()
    for c in connections:
        src = m2a.get(str(c.get("source_machine_id") or c.get("PK", "").removeprefix("MACHINE#")), "")
        dst = _s(c.get("target_app_id")) or m2a.get(str(c.get("target_machine_id", "")), "")
        if src and dst and src != dst:
            deps[src].add(dst)
            if _s(c.get("target_port")) in DB_PORTS:
                shared.add(frozenset((src, dst)))
    for a in apps:
        aid = a["app_id"]
        for ref in _as_list(a.get("depends_on")) + _as_list(a.get("dependencies_upstream")):
            b = resolve(ref)
            if b and b != aid:
                deps[aid].add(b)
        for ref in _as_list(a.get("dependencies_downstream")):   # ref depends on this app
            b = resolve(ref)
            if b and b != aid:
                deps[b].add(aid)
        if _truthy(a.get("shared_db")):
            for b in deps.get(aid, set()):
                shared.add(frozenset((aid, b)))
    return {k: v for k, v in deps.items() if v}, shared


# ── scoring (playbook Step 2) ───────────────────────────────────────────────

def risk_score(a: dict, shared_db: bool) -> int:
    comp = " ".join(str(x) for x in _as_list(a.get("compliance"))).upper()
    s = 3 * (a.get("tier") == "tier_1")
    s += 2 * (_num(a.get("peak_tps")) > 1000)
    s += 2 * shared_db
    s += 2 * any(k in comp for k in ("PCI", "SOX", "HIPAA"))
    s += 1 * _truthy(a.get("multi_region"))
    return min(10, s)


def complexity_score(a: dict, outbound: int) -> int:
    db = a.get("database")
    engine = (db.get("engine") if isinstance(db, dict) else db) or a.get("db_engine") or ""
    s = 3 * (_s(a.get("migration_strategy")).lower() == "refactor")
    s += 2 * any(k in _s(engine).lower() for k in ("oracle", "db2"))
    s += 2 * (_num(a.get("data_volume_gb")) > 500)
    s += 1 * (outbound > 5)
    s += len(_as_list(a.get("risk_factors")))
    conf = a.get("strategy_confidence")
    s += 1 * (conf not in (None, "") and _num(conf) < 60)
    return min(10, s)


def business_priority(a: dict) -> int:
    cons = a.get("constraints") if isinstance(a.get("constraints"), dict) else {}
    if cons.get("hard_deadline") or a.get("hard_deadline"):
        return 1
    lic = a.get("licensing") if isinstance(a.get("licensing"), dict) else {}
    if _num(a.get("annual_license") or lic.get("annual_cost")) > 50000:
        return 1
    if a.get("active_development") is False or _s(a.get("active_development")).lower() == "false":
        if a.get("tier") == "tier_3":
            return 3
    return 2


# ── graph helpers ───────────────────────────────────────────────────────────

def _sccs(nodes: list[str], deps: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components over the planned subgraph."""
    index, low, stack, on, out, counter = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in deps.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(sorted(comp))

    for n in sorted(nodes):
        if n not in index:
            visit(n)
    return out


class _UF:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


# ── planner ─────────────────────────────────────────────────────────────────

def generate_wave_plan(apps: list[dict], connections: list[dict] | None = None, *,
                       config: PlannerConfig | None = None) -> dict:
    cfg = config or PlannerConfig()
    apps = [a for a in apps if a.get("app_id")]
    by_id = {a["app_id"]: a for a in apps}
    deps, shared = build_dependencies(apps, connections or [])
    conflicts: list[dict] = []

    strategy = {aid: _s(a.get("migration_strategy")).lower() for aid, a in by_id.items()}
    not_ready = sorted(aid for aid, s in strategy.items() if not s)
    close_out = sorted(aid for aid, s in strategy.items() if s in CLOSE_OUT)
    candidates = sorted(aid for aid, s in strategy.items() if s and s not in CLOSE_OUT)
    cand_set = set(candidates)

    # Blocked: depends (transitively) on an app that can't be planned yet.
    blocked: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for aid in candidates:
            if aid in blocked:
                continue
            for b in deps.get(aid, ()):
                if b in not_ready or b in blocked:
                    blocked[aid] = b
                    changed = True
                    break
    for aid, b in sorted(blocked.items()):
        conflicts.append({"type": "dependency_not_ready", "app": aid, "depends_on": b,
                          "message": f"{aid} depends on {b}, which has no assessed strategy (or is blocked); moved to Long Tail."})
    for aid in candidates:
        for b in sorted(deps.get(aid, ())):
            if b in close_out:
                conflicts.append({"type": "dependency_close_out", "app": aid, "depends_on": b,
                                  "message": f"{aid} depends on {b}, which is {strategy[b]}; confirm the dependency is replaced."})

    planned = [a for a in candidates if a not in blocked]
    planned_set = set(planned)
    pdeps = {a: {b for b in deps.get(a, ()) if b in planned_set} for a in planned}

    # Groups: cycles (SCC) and shared-DB partners move together.
    uf = _UF(planned)
    long_tail_cycles: set[str] = set()
    for comp in _sccs(planned, pdeps):
        if len(comp) > 1:
            if len(comp) <= cfg.max_cycle_cowave:
                for x in comp[1:]:
                    uf.union(comp[0], x)
                conflicts.append({"type": "cycle_cowave", "apps": comp,
                                  "message": f"Dependency cycle {' ↔ '.join(comp)}: co-waved with a coordinated cutover."})
            else:
                long_tail_cycles.update(comp)
                conflicts.append({"type": "cycle_strangler", "apps": comp,
                                  "message": f"Cycle of {len(comp)} apps: strangler-fig candidate (decouple first); moved to Long Tail."})
    for pair in shared:
        a, b = tuple(pair)
        if a in planned_set and b in planned_set and a not in long_tail_cycles and b not in long_tail_cycles:
            uf.union(a, b)
    groups: dict[str, list[str]] = defaultdict(list)
    for a in planned:
        if a not in long_tail_cycles:
            groups[uf.find(a)].append(a)

    # Scores and the earliest band per app.
    shared_apps = {x for p in shared for x in p}
    info: dict[str, dict] = {}
    for aid, a in by_id.items():
        info[aid] = {"risk": risk_score(a, aid in shared_apps),
                     "complexity": complexity_score(a, len(deps.get(aid, ()))),
                     "priority": business_priority(a)}

    def band_of(aid: str) -> int:
        a = by_id[aid]
        if _truthy(a.get("customer_facing")):
            return 3
        return 2 if a.get("tier") in ("tier_1", "tier_2") else 1

    tier1_total = sum(1 for a in apps if a.get("tier") == "tier_1" and a["app_id"] in planned_set)
    tier1_cap = max(1, int(cfg.tier1_max_share * tier1_total))

    def cap(w: int) -> int:
        return cfg.band_limits.get(w, cfg.spill_limit)

    # Group dependency DAG → topological order (Kahn), stable by priority/risk/complexity.
    gid = {a: root for root, members in groups.items() for a in members}
    gdeps = {g: {gid[b] for a in m for b in pdeps.get(a, ()) if b in gid and gid[b] != g} for g, m in groups.items()}

    def gkey(g):
        m = groups[g]
        return (min(info[a]["priority"] for a in m), sum(info[a]["risk"] + info[a]["complexity"] for a in m), g)

    order, indeg = [], {g: len(d) for g, d in gdeps.items()}
    users = defaultdict(set)
    for g, d in gdeps.items():
        for x in d:
            users[x].add(g)
    ready = sorted((g for g, n in indeg.items() if n == 0), key=gkey)
    while ready:
        g = ready.pop(0)
        order.append(g)
        for u in sorted(users[g]):
            indeg[u] -= 1
            if indeg[u] == 0:
                ready.append(u)
        ready.sort(key=gkey)

    # Pilot picks (playbook: lowest risk × complexity, tier_3, zero upstream deps, not customer-facing).
    pilot_pool = [g for g in order if len(groups[g]) == 1 and not gdeps[g]
                  and by_id[groups[g][0]].get("tier") == "tier_3" and band_of(groups[g][0]) == 1]
    pilot_pool.sort(key=lambda g: (info[groups[g][0]]["risk"] + info[groups[g][0]]["complexity"],
                                   info[groups[g][0]]["priority"], g))
    pilots = set(pilot_pool[: min(cfg.pilot_size, cap(0))])

    wave_of: dict[str, int] = {}
    load: dict[int, int] = defaultdict(int)
    t1load: dict[int, int] = defaultdict(int)
    reason: dict[str, str] = {}
    for g in order:
        m = groups[g]
        dep_floor = max((wave_of[gid[b]] + 1 for a in m for b in pdeps.get(a, ()) if gid.get(b) in wave_of), default=0)
        if g in pilots:
            w, why = 0, "Pilot: tier_3, no upstream dependencies, lowest risk + complexity"
        else:
            base = max(band_of(a) for a in m)
            w = max(base, dep_floor, 1)
            why = f"{BANDS[min(base, 3)][0]} band" + (f"; after its dependencies (≥ wave {dep_floor})" if dep_floor > base else "")
        n_t1 = sum(1 for a in m if by_id[a].get("tier") == "tier_1")
        start = w
        while (load[w] + len(m) > cap(w) and load[w] > 0) or (n_t1 and t1load[w] + n_t1 > tier1_cap and t1load[w] > 0):
            w += 1
            if w > start + cfg.max_waves:
                break
        if w != start:
            why += f"; spilled from wave {start} (capacity / tier_1 spread)"
        if any(_truthy(by_id[a].get("customer_facing")) for a in m) and w < 3:
            w = 3
        for a in m:
            wave_of[a] = w
            reason[a] = why + (f"; co-waved with {', '.join(x for x in m if x != a)}" if len(m) > 1 else "")
        wave_of[g] = w
        load[w] += len(m)
        t1load[w] += n_t1

    last = max([wave_of[a] for a in planned if a in wave_of] + [3]) + 1
    for aid in close_out:
        wave_of[aid], reason[aid] = last, f"Close-out ({strategy[aid]}): Long Tail"
    for aid in blocked:
        wave_of[aid], reason[aid] = last, f"Blocked: depends on {blocked[aid]} (not ready)"
    for aid in long_tail_cycles:
        wave_of[aid], reason[aid] = last, "Large dependency cycle: decouple first (strangler-fig)"

    # Final rule check.
    for a in planned:
        for b in pdeps.get(a, ()):
            if a in wave_of and b in wave_of and wave_of[a] <= wave_of[b] and gid.get(a) != gid.get(b):
                conflicts.append({"type": "dependency_order", "app": a, "depends_on": b,
                                  "message": f"{a} (wave {wave_of[a]}) must come after {b} (wave {wave_of[b]})."})
    for pair in shared:
        a, b = tuple(pair)
        if a in wave_of and b in wave_of and a in planned_set and b in planned_set and wave_of[a] != wave_of[b]:
            conflicts.append({"type": "shared_db_split", "apps": sorted(pair),
                              "message": f"{a} and {b} share a database but are in waves {wave_of[a]} and {wave_of[b]}."})

    waves = []
    for w in sorted({wave_of[a] for a in by_id if a in wave_of}):
        members = sorted((a for a in by_id if wave_of.get(a) == w),
                         key=lambda a: (info[a]["priority"], info[a]["risk"] + info[a]["complexity"], a))
        label, criteria = (LONG_TAIL if w == last else BANDS.get(w, (f"Wave {w}", "Spill-over (capacity or dependency order)")))
        waves.append({
            "wave_number": w, "label": label if w in BANDS or w == last else f"Wave {w}", "band": label,
            "criteria": criteria,
            "apps": [{"app_id": a, "app_name": by_id[a].get("app_name", ""), "strategy": strategy[a],
                      "tier": by_id[a].get("tier", ""),
                      "machine_count": by_id[a].get("machine_count") or len(by_id[a].get("machine_ids") or []),
                      "reason": reason.get(a, ""), **info[a], "depends_on": sorted(deps.get(a, ()))}
                     for a in members],
        })
    not_ready_out = [{"app_id": a, "app_name": by_id[a].get("app_name", ""),
                      "reason": "No assessed migration strategy yet; run Discovery/Assessment first."} for a in not_ready]
    return {
        "waves": waves,
        "not_ready": not_ready_out,
        "conflicts": conflicts,
        "rules": [
            "Retire/retain/repurchase → Long Tail; apps without an assessed strategy are not planned",
            "A depends on B ⇒ A's wave ≥ B's wave + 1 (CONN#, depends_on, L2 upstream/downstream)",
            "Shared database ⇒ same wave; dependency cycles ≤ "
            f"{cfg.max_cycle_cowave} apps co-wave, larger → Long Tail",
            f"At most {tier1_cap} tier_1 app(s) per wave (30% of {tier1_total}); customer-facing never in wave 0/1",
            f"Band limits {cfg.band_limits}; pilot size {cfg.pilot_size}",
        ],
        "summary": {
            "total_waves": len(waves),
            "total_apps": sum(len(w["apps"]) for w in waves),
            "not_ready": len(not_ready),
            "conflicts": len(conflicts),
            "apps_per_wave": [{"wave": w["wave_number"], "label": w["label"], "count": len(w["apps"])} for w in waves],
        },
        "app_dependencies": {k: sorted(v) for k, v in deps.items()},
    }
