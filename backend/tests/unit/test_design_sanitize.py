"""Unit tests for the design-doc [REQUIRES INPUT] sanitizer."""

from backend.src.api.v1.design import _sanitize_design_doc

META = {
    "target_account": "12345678910",
    "vpc_cidr": "192.168.0.0/16",
    "target_region": "us-east-1",
    "target_compute": "ec2",
}


def test_replaces_account_in_required_inputs_table():
    md = (
        "| Field | Status | Notes |\n"
        "|---|---|---|\n"
        "| AWS Account ID | [REQUIRES INPUT] | New dedicated account or existing 12-digit AWS Account ID |\n"
    )
    out = _sanitize_design_doc(md, META)
    assert "[REQUIRES INPUT]" not in out
    assert "12345678910" in out


def test_replaces_vpc_cidr_and_region():
    md = (
        "- VPC CIDR: [REQUIRES INPUT]\n"
        "- Target Region: [REQUIRES INPUT]\n"
    )
    out = _sanitize_design_doc(md, META)
    assert "192.168.0.0/16" in out
    assert "us-east-1" in out
    assert "[REQUIRES INPUT]" not in out


def test_leaves_genuinely_missing_placeholders():
    md = "- Cutover Window: [REQUIRES INPUT]\n- Budget Approval: [REQUIRES INPUT]\n"
    out = _sanitize_design_doc(md, META)
    assert out == md  # untouched — these aren't confirmed on META


def test_only_touches_lines_with_matching_label():
    md = "- Target Account: [REQUIRES INPUT]\n- Something Else: [REQUIRES INPUT]\n"
    out = _sanitize_design_doc(md, META)
    assert "12345678910" in out
    assert "- Something Else: [REQUIRES INPUT]" in out


def test_empty_or_missing_meta_is_safe():
    md = "| AWS Account ID | [REQUIRES INPUT] |\n"
    assert _sanitize_design_doc(md, {}) == md
    assert _sanitize_design_doc("", META) == ""


def test_idempotent():
    md = "| AWS Account ID | [REQUIRES INPUT] | ... |\n"
    once = _sanitize_design_doc(md, META)
    twice = _sanitize_design_doc(once, META)
    assert once == twice


# ── Schedule-risk banner ───────────────────────────────────────────────────────

# APP-MCC-ORACLE's real values: EOSL 2026-12-31, hard-deadline 2027-01-31 →
# deadline is 31 days AFTER EOSL → the app would run unsupported → CRITICAL.
TIGHT_META = {
    **META,
    "constraints": {"eosl_date": "2026-12-31", "hard_deadline": "2027-01-31"},
}
# Hard-deadline comfortably BEFORE EOSL (365-day safety margin) → no banner.
COMFY_META = {
    **META,
    "constraints": {"eosl_date": "2029-01-01", "hard_deadline": "2028-01-01"},
}


def test_banner_critical_when_deadline_after_eosl():
    out = _sanitize_design_doc("# Design\n", TIGHT_META)
    assert "Schedule Risk" in out and "CRITICAL" in out
    assert "unsupported" in out
    assert "31 day" in out
    # Banner prepends; original heading stays below it.
    assert out.rstrip().endswith("# Design")


def test_banner_warning_when_small_safety_gap():
    # 60-day safety margin (deadline before EOSL) → WARNING, not CRITICAL.
    out = _sanitize_design_doc(
        "# D\n",
        {"constraints": {"eosl_date": "2027-03-01", "hard_deadline": "2026-12-31"}},
    )
    assert "WARNING" in out
    assert "CRITICAL" not in out


def test_banner_skipped_when_buffer_comfortable():
    out = _sanitize_design_doc("# D\n", COMFY_META)
    assert "Schedule Risk" not in out


def test_banner_skipped_when_dates_missing():
    out = _sanitize_design_doc("# D\n", {})
    assert "Schedule Risk" not in out


def test_banner_idempotent_on_repeat_reads():
    once = _sanitize_design_doc("# D\n", TIGHT_META)
    twice = _sanitize_design_doc(once, TIGHT_META)
    # Only one banner — not two concatenated.
    assert once == twice
    assert once.count("Schedule Risk") == 1


def test_banner_reads_top_level_eosl_date_too():
    # When eosl_date / hard_deadline sit on META directly (not under constraints)
    out = _sanitize_design_doc(
        "# D\n", {"eosl_date": "2026-12-31", "hard_deadline": "2027-01-31"}
    )
    assert "Schedule Risk" in out
