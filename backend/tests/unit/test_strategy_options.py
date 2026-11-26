"""Unit tests for the strategy-driven options + validator."""

from backend.src.core.strategy_options import (
    cicd_options_for,
    compute_options_for,
    database_options_for,
    requires_aws_targeting,
    transforms_allowed_for,
    validate_assessment_inputs,
)


# ── option tables ─────────────────────────────────────────────────────────────
def test_lift_strategies_force_ec2():
    for s in ("rehost", "relocate"):
        assert compute_options_for(s) == {"ec2"}
        assert database_options_for(s) == {"same_as_source", "dynamodb", "rds_mysql"}
        assert not transforms_allowed_for(s)


def test_modern_strategies_allow_full_set():
    for s in ("replatform", "refactor"):
        opts = compute_options_for(s)
        # Scope: ECS Fargate + Lambda; EC2 is lift-and-shift only.
        assert {"ecs_fargate", "lambda"} == opts
        assert "eks" not in opts
        assert transforms_allowed_for(s)


def test_codepipeline_is_the_only_cicd_target():
    # Scope: no GitHub Actions / GitLab / Jenkins.
    for s in ("rehost", "relocate", "replatform", "refactor"):
        assert cicd_options_for(s) == {"codepipeline"}


def test_closeout_strategies_have_no_targets():
    # retain is routed to close-out (ResolveStrategy → Handover) like retire/repurchase.
    for s in ("retire", "retain", "repurchase"):
        assert compute_options_for(s) == {"none"}
        assert database_options_for(s) == {"none"}
        assert cicd_options_for(s) == {"none"}
        assert not requires_aws_targeting(s)
        assert not transforms_allowed_for(s)


# ── validator ─────────────────────────────────────────────────────────────────
_VALID = {
    "target_compute": "ecs_fargate",
    "target_database": "aurora_postgresql",
    "cicd_target": "codepipeline",
    "target_account": "123456789012",
    "vpc_cidr": "10.0.0.0/16",
    "db_instance_class": "db.r6g.large",
}


def test_valid_replatform_passes():
    assert validate_assessment_inputs("replatform", _VALID) == []


def test_rehost_rejects_fargate():
    errs = validate_assessment_inputs("rehost", {**_VALID, "target_compute": "ecs_fargate",
                                                   "target_database": "same_as_source"})
    assert any(e["field"] == "target_compute" for e in errs)


def test_rehost_requires_lift_db():
    errs = validate_assessment_inputs("rehost", {**_VALID, "target_compute": "ec2"})
    assert any(e["field"] == "target_database" for e in errs)


def test_relocate_rejects_transforms():
    errs = validate_assessment_inputs(
        "relocate",
        {"target_compute": "ec2", "target_database": "same_as_source",
         "cicd_target": "codepipeline", "target_account": "123456789012", "vpc_cidr": "10.0.0.0/16",
         "transformation_steps": ["AWS/java-version-upgrade"]},
    )
    assert any(e["field"] == "transformation_steps" for e in errs)


def test_short_account_id_rejected():
    errs = validate_assessment_inputs("replatform", {**_VALID, "target_account": "12345678"})
    assert any(e["field"] == "target_account" and "12 digits" in e["message"] for e in errs)


def test_missing_account_id_required_for_replatform():
    errs = validate_assessment_inputs("replatform", {**_VALID, "target_account": ""})
    assert any(e["field"] == "target_account" for e in errs)


def test_bad_vpc_cidr_rejected():
    errs = validate_assessment_inputs("replatform", {**_VALID, "vpc_cidr": "999.999.0.0/16"})
    assert any(e["field"] == "vpc_cidr" for e in errs)


def test_public_cidr_rejected():
    errs = validate_assessment_inputs("replatform", {**_VALID, "vpc_cidr": "8.8.8.0/16"})
    assert any(e["field"] == "vpc_cidr" for e in errs)


def test_retire_does_not_require_aws_targeting():
    assert validate_assessment_inputs("retire", {"target_compute": "none", "target_database": "none",
                                                  "cicd_target": "none"}) == []


def test_vpc_cidr_must_be_a_createvpc_valid_network():
    """APP-HE/APP-TE (2026-10-13) were approved with 10.0.0.2/24 and 10.0.0.1/24 — CreateVpc rejects both."""
    from backend.src.core.strategy_options import vpc_cidr_ok

    assert vpc_cidr_ok("10.0.0.0/16") and vpc_cidr_ok("10.20.4.0/24") and vpc_cidr_ok("192.168.0.0/24") and not vpc_cidr_ok("192.168.0.0/25") and not vpc_cidr_ok("192.168.0.0/28")
    for bad in ("10.0.0.2/24", "10.0.0.1/24", "10.0.0.0/29", "10.0.0.0/30", "8.8.8.0/24", "10.0.0.0/15"):
        assert not vpc_cidr_ok(bad), bad
