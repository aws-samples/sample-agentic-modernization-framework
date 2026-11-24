"""Wave planner follows backend/resources/skills/planning.md."""
from backend.src.core.wave_planner import PlannerConfig, generate_wave_plan


def app(aid, strategy="replatform", tier="tier_3", **kw):
    return {"app_id": aid, "app_name": aid.title(), "migration_strategy": strategy, "tier": tier, **kw}


def waves_by_app(plan):
    return {a["app_id"]: w["wave_number"] for w in plan["waves"] for a in w["apps"]}


def band_of(plan, aid):
    return next(w["band"] for w in plan["waves"] for a in w["apps"] if a["app_id"] == aid)


def test_close_outs_go_to_long_tail_and_unassessed_are_not_planned():
    plan = generate_wave_plan([app("A"), app("R", "retire", "tier_1"), app("K", "retain"), app("P", "repurchase"),
                               app("U", "")])
    w = waves_by_app(plan)
    assert "U" not in w and [x["app_id"] for x in plan["not_ready"]] == ["U"]
    assert w["R"] == w["K"] == w["P"] == max(w.values()) >= 4
    assert band_of(plan, "R") == "Long Tail"


def test_pilot_is_tier3_dependency_free_low_risk_and_capped():
    apps = [app(f"T{i}") for i in range(5)] + [app("C", tier="tier_2"), app("D", depends_on=["T0"])]
    plan = generate_wave_plan(apps, config=PlannerConfig(pilot_size=3))
    pilots = [a["app_id"] for w in plan["waves"] if w["wave_number"] == 0 for a in w["apps"]]
    assert len(pilots) == 3 and "C" not in pilots and "D" not in pilots
    assert waves_by_app(plan)["C"] == 2                         # tier_2 → Core band


def test_every_dependency_source_orders_waves():
    apps = [app("B", tier="tier_2"), app("A1", depends_on=["B"]), app("A2", dependencies_upstream=["B"]),
            app("A3"), app("X", dependencies_downstream=["A3"]), app("M1", machine_ids=["m1"]),
            app("M2", tier="tier_2", machine_ids=["m2"])]
    conns = [{"source_machine_id": "m1", "target_machine_id": "m2", "target_port": "443"}]
    w = waves_by_app(generate_wave_plan(apps, conns))
    assert w["A1"] > w["B"] and w["A2"] > w["B"]                # depends_on, L2 upstream
    assert w["A3"] > w["X"]                                     # X lists A3 downstream ⇒ A3 depends on X
    assert w["M1"] > w["M2"]                                    # machine CONN#


def test_dependency_by_app_name_is_resolved():
    apps = [app("APP-B", app_name="Billing", tier="tier_2"), app("APP-A", dependencies_upstream=["billing"])]
    w = waves_by_app(generate_wave_plan(apps))
    assert w["APP-A"] > w["APP-B"]


def test_shared_db_partners_share_a_wave():
    apps = [app("WEB", machine_ids=["w"]), app("DB", tier="tier_2", machine_ids=["d"])]
    conns = [{"source_machine_id": "w", "target_machine_id": "d", "target_port": "5432"}]
    plan = generate_wave_plan(apps, conns)
    w = waves_by_app(plan)
    assert w["WEB"] == w["DB"]
    assert not [c for c in plan["conflicts"] if c["type"] in ("shared_db_split", "dependency_order")]


def test_small_cycle_cowaves_large_cycle_goes_long_tail():
    small = [app("A", depends_on=["B"]), app("B", depends_on=["A"])]
    w = waves_by_app(generate_wave_plan(small))
    assert w["A"] == w["B"]
    ring = [app(f"R{i}", depends_on=[f"R{(i + 1) % 4}"]) for i in range(4)]
    plan = generate_wave_plan(ring)
    assert {band_of(plan, f"R{i}") for i in range(4)} == {"Long Tail"}
    assert any(c["type"] == "cycle_strangler" for c in plan["conflicts"])


def test_band_capacity_spills_and_tier1_is_spread():
    apps = [app(f"F{i}") for i in range(14)]                    # Foundation cap 8 (after 3 pilots)
    w = waves_by_app(generate_wave_plan(apps))
    assert sum(1 for v in w.values() if v == 1) == 8 and max(w.values()) >= 2
    t1 = [app(f"C{i}", tier="tier_1") for i in range(4)]        # 30% of 4 → 1 per wave
    w1 = waves_by_app(generate_wave_plan(t1))
    assert len(set(w1.values())) == 4


def test_customer_facing_never_in_wave_0_or_1():
    plan = generate_wave_plan([app("PORTAL", customer_facing=True), app("SVC")])
    assert waves_by_app(plan)["PORTAL"] >= 3


def test_blocked_by_unready_dependency_goes_long_tail():
    plan = generate_wave_plan([app("A", depends_on=["U"]), app("U", ""), app("B")])
    assert band_of(plan, "A") == "Long Tail"
    assert any(c["type"] == "dependency_not_ready" and c["app"] == "A" for c in plan["conflicts"])


def test_every_planned_app_has_a_reason_and_scores():
    plan = generate_wave_plan([app("A", tier="tier_1", compliance=["PCI"], peak_tps=5000), app("B", "refactor")])
    rows = {a["app_id"]: a for w in plan["waves"] for a in w["apps"]}
    assert rows["A"]["risk"] >= 7 and rows["B"]["complexity"] >= 3
    assert all(r["reason"] for r in rows.values())


def test_uat_shape_from_the_2026_11_04_review():
    """The 11:57 plan put a retire app in Wave 1 and unassessed apps in Pilot; APP-TE → APP-WEBAPP co-waved."""
    apps = [app("APP-CARDDEMO", "retire", "tier_1"), app("APP-CDK", "rehost"), app("APP-MUSIC", ""),
            app("APP-TE", "relocate", "tier_2", depends_on=["APP-WEBAPP"]), app("APP-WEBAPP", "replatform"),
            app("APP-HE", "replatform", "tier_2"), app("APP-CLI", "replatform", "tier_2")]
    plan = generate_wave_plan(apps)
    w = waves_by_app(plan)
    assert band_of(plan, "APP-CARDDEMO") == "Long Tail"
    assert "APP-MUSIC" not in w
    assert w["APP-TE"] > w["APP-WEBAPP"]
    assert not [c for c in plan["conflicts"] if c["type"] == "dependency_order"]
