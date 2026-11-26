"""Unit tests for deterministic migration risk scoring (pure, no I/O)."""

from datetime import date

from backend.src.core.risk_scoring import (
    BUSINESS_QUESTIONS,
    CATEGORY_MAX,
    OVERALL_MAX,
    TEAM_QUESTIONS,
    calculate_risk_score,
    render_report_md,
)


def _cat(result, name):
    return next(c for c in result["categories"] if c["category"] == name)


def _all(questions, status):
    return {k: status for k, _ in questions}


def test_shape_and_bounds():
    r = calculate_risk_score({})
    assert r["max"] == OVERALL_MAX == 100
    assert len(r["categories"]) == 4
    assert 0 <= r["overall"] <= 100
    for c in r["categories"]:
        assert c["max"] == CATEGORY_MAX == 25
        assert 0 <= c["score"] <= 25


def test_empty_app_is_worst_readiness_but_low_tech_risk():
    # No EOSL (0), no deps/repos/machines but no tests (5), readiness unanswered -> 25 each
    r = calculate_risk_score({})
    assert _cat(r, "Risk (EOSL)")["score"] == 0
    assert _cat(r, "Team Readiness")["score"] == 25
    assert _cat(r, "Business Readiness")["score"] == 25
    # complexity: 0 deps + 0 repos + 5 (no tests) + 0 machines = 5
    assert _cat(r, "Complexity")["score"] == 5


def test_eosl_expired_is_max_risk():
    r = calculate_risk_score({"eosl_date": "2020-01-01"})
    row = _cat(r, "Risk (EOSL)")
    assert row["score"] == 25
    assert "expired" in row["reason"].lower()


def test_eosl_before_2032_is_mid_risk():
    r = calculate_risk_score({"eosl_date": "2030-06-01"})
    assert _cat(r, "Risk (EOSL)")["score"] == 13


def test_eosl_after_2032_and_year_only():
    assert _cat(calculate_risk_score({"eosl_date": "2035"}), "Risk (EOSL)")["score"] == 0
    assert _cat(calculate_risk_score({"eosl": 2040}), "Risk (EOSL)")["score"] == 0


def test_complexity_high_deps_repos_no_tests_many_machines():
    r = calculate_risk_score({"dependencies_count": 15, "repo_count": 5, "machine_count": 12})
    # deps>10 -> 10, repos>3 -> 5, no tests -> 5, machines>=10 -> 5 => 25 (capped)
    assert _cat(r, "Complexity")["score"] == 25


def test_complexity_low_with_all_tests():
    r = calculate_risk_score(
        {"dependencies_count": 2, "repo_count": 1, "unit_tests": True, "qa_tests": True, "functional_tests": True}
    )
    # deps<5 -> 0, repos<=1 -> 0, all tests -> 0, 0 machines -> 0
    assert _cat(r, "Complexity")["score"] == 0


def test_complexity_from_dependency_lists_and_testing_strategy():
    r = calculate_risk_score(
        {"dependencies_upstream": ["a", "b", "c"], "dependencies_downstream": ["d", "e", "f"],
         "repos": [{}, {}], "testing_strategy": ["Unit tests", "Functional / e2e"]}
    )
    # 6 deps -> 5, 2 repos -> 2, 2 test types present -> round(1/3*5)=2, 0 machines -> 0 => 9
    assert _cat(r, "Complexity")["score"] == 9


def test_complexity_includes_machine_count():
    base = {"dependencies_count": 0, "repo_count": 1,
            "unit_tests": True, "qa_tests": True, "functional_tests": True}
    # All low except machine_count — isolates the machine axis.
    assert _cat(calculate_risk_score({**base, "machine_count": 1}), "Complexity")["score"] == 0
    assert _cat(calculate_risk_score({**base, "machine_count": 3}), "Complexity")["score"] == 2
    assert _cat(calculate_risk_score({**base, "machine_count": 5}), "Complexity")["score"] == 4
    assert _cat(calculate_risk_score({**base, "machine_count": 50}), "Complexity")["score"] == 5


def test_complexity_falls_back_to_machine_ids_length():
    r = calculate_risk_score({
        "dependencies_count": 0, "repo_count": 1,
        "unit_tests": True, "qa_tests": True, "functional_tests": True,
        "machine_ids": ["m1", "m2", "m3", "m4"],  # 4 → 4 points
    })
    assert _cat(r, "Complexity")["score"] == 4


def test_complexity_reason_mentions_machines():
    r = calculate_risk_score({"machine_count": 7})
    assert "machine" in _cat(r, "Complexity")["reason"].lower()


# ── schema-path coverage ─────────────────────────────────────────────────────
def test_eosl_read_from_constraints_sub_object():
    # Many apps store eosl_date under `constraints.eosl_date` rather than top-level.
    r = calculate_risk_score({"constraints": {"eosl_date": "2030-06-01"}})
    assert _cat(r, "Risk (EOSL)")["score"] == 13


def test_dependencies_read_from_nested_object():
    # Discovery sometimes stores deps as dependencies: {upstream:[...], downstream:[...]}
    r = calculate_risk_score({
        "dependencies": {"upstream": ["a", "b", "c"], "downstream": ["d", "e"]},
    })
    # 5 deps → bucket 5-10 → 5 pts; + 5 pts for no tests = 10 pts total complexity
    assert _cat(r, "Complexity")["score"] == 10


def test_readiness_all_completed_is_zero():
    r = calculate_risk_score(
        {"team_readiness": _all(TEAM_QUESTIONS, "Completed"),
         "business_readiness": _all(BUSINESS_QUESTIONS, "Completed")}
    )
    assert _cat(r, "Team Readiness")["score"] == 0
    assert _cat(r, "Business Readiness")["score"] == 0
    assert r["inputs_complete"] is True


def test_readiness_status_gradient():
    r = calculate_risk_score({"team_readiness": _all(TEAM_QUESTIONS, "Pending")})
    # 5 questions * 4 pts = 20
    assert _cat(r, "Team Readiness")["score"] == 20


def test_unknown_status_treated_as_worst():
    r = calculate_risk_score({"business_readiness": _all(BUSINESS_QUESTIONS, "banana")})
    assert _cat(r, "Business Readiness")["score"] == 25


def test_tiers():
    low = calculate_risk_score(
        {"team_readiness": _all(TEAM_QUESTIONS, "Completed"),
         "business_readiness": _all(BUSINESS_QUESTIONS, "Completed")}
    )
    assert low["tier"] == "Low"
    high = calculate_risk_score({"eosl_date": "2019-01-01", "dependencies_count": 20, "repo_count": 9})
    assert high["overall"] >= 67 and high["tier"] == "High"


def test_report_md_renders_table():
    md = render_report_md("KDR", calculate_risk_score({"eosl_date": "2020-01-01"}))
    assert "Migration Risk Score — KDR" in md
    assert "| Risk (EOSL) |" in md
    assert "Overall" in md


def test_inputs_complete_false_when_partial():
    r = calculate_risk_score({"team_readiness": {"team_availability": "Completed"}})
    assert r["inputs_complete"] is False


def test_future_year_boundary_2032_exact():
    # 2032 exactly is NOT "before 2032" -> low risk
    assert _cat(calculate_risk_score({"eosl_date": "2032-01-01"}), "Risk (EOSL)")["score"] == 0
    assert date.today().year <= 2032  # guard: bands assume we're pre-2032
