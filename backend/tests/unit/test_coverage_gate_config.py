"""The per-app CI coverage gate is editable and validated (PUT /portfolio/apps)."""
import pytest

from backend.src.api.v1.portfolio import _normalise_coverage_gate


@pytest.mark.parametrize("raw,expected", [("50", 50), (55, 55), ("", None), (None, None), ("100", 100)])
def test_valid_values_are_normalised(raw, expected):
    body = {"coverage_threshold": raw, "coverage_threshold_reason": "  measured 52.1% on Java 25  "}
    assert _normalise_coverage_gate(body) is None
    assert body["coverage_threshold"] == expected
    assert body["coverage_threshold_reason"] == "measured 52.1% on Java 25"


@pytest.mark.parametrize("raw", ["-1", "101", "fifty", "50.5"])
def test_out_of_range_or_non_integer_is_refused(raw):
    assert "0 to 100" in _normalise_coverage_gate({"coverage_threshold": raw})


def test_zero_disables_the_gate_only_with_a_reason():
    assert "needs coverage_threshold_reason" in _normalise_coverage_gate({"coverage_threshold": "0"})
    assert _normalise_coverage_gate({"coverage_threshold": "0", "coverage_threshold_reason": "no tests"}) is None


def test_reason_length_is_capped():
    assert "at most" in _normalise_coverage_gate({"coverage_threshold_reason": "x" * 501})
