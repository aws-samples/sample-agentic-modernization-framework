"""Discovery 7R rules (tool_strategy) vs backend/resources/skills/discovery.md.

Pins the journey-audit findings:
- RELOCATE: VMware current_compute + DC-exit/lease signal (APP-TE).
- REPLATFORM: compound Java + monolith + not containerized + manual deploy (APP-HE).
- a keyword-only "microservices" must not override stronger structured evidence
  (APP-TE was recommended refactor/rehost off that one word).
- strategy_confidence/strategy_rationale always describe META.migration_strategy;
  the engine's pick is stored as recommended_* (APP-TE showed 'rehost' with
  refactor's "Matched trigger(s): ['microservices']").
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_LAMBDA_SRC = Path(__file__).resolve().parents[3] / "infra" / "environments" / "agentcore" / "lambda-src"
if str(_LAMBDA_SRC) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_SRC))
os.environ.setdefault("DYNAMODB_TABLE", "amf_portfolio")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

APP = "APP-X"
RUN = "arn:aws:states:us-east-1:000000000000:execution:amf-agentcore-workflow:APP-X-20261006-090000"

# Structured L2 fields of APP-HE / APP-TE (/tmp/review/<APP>/l2_app-data.json).
HE = {
    "description": "HE Health Engine — Health Services monolith application (Java/Spring Boot 2.7) on-prem.",
    "tier": "tier_1", "language": "Java", "framework": "Spring Boot 2.7", "architecture": "monolith",
    "deployment_method": "manual", "current_compute": "on-prem VMware vSphere", "containerization": "none",
    "constraints": {"dc_lease_end": "2027-03-31", "eosl_date": "2027-11-30"},
    "database": {"engine": "PostgreSQL", "licensing": "open_source"},
    "change_history": {"release_frequency": "bi-weekly", "team_size": 8, "active_development": True},
    "current_cost": {"monthly_infra": 22000, "annual_license": 0},
}
TE = {
    "description": "TE Telecom Engine — Telecom microservices application (Java/Spring Boot 2.7) on-prem.",
    "tier": "tier_1", "language": "Java", "framework": "Spring Boot 2.7", "architecture": "microservices",
    "deployment_method": "CI/CD (Jenkins)", "current_compute": "on-prem VMware vSphere",
    "containerization": "partial (Docker)",
    "constraints": {"dc_lease_end": "2027-03-31", "eosl_date": "2027-11-30"},
    "database": {"engine": "PostgreSQL", "licensing": "open_source"},
    "change_history": {"release_frequency": "bi-weekly", "team_size": 8, "active_development": True},
}


@pytest.fixture
def ts(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName="amf_portfolio",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        import tool_strategy

        monkeypatch.setattr(tool_strategy, "table", t)
        yield tool_strategy


def _seed(ts, **fields):
    ts.table.put_item(Item={"PK": f"APP#{APP}", "SK": "META", "app_id": APP, **fields})


def _meta(ts):
    return ts.table.get_item(Key={"PK": f"APP#{APP}", "SK": "META"})["Item"]


def _confirm_in_current_run(ts):
    ts.table.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE", "execution_arn": RUN})
    ts.table.put_item(Item={"PK": f"APP#{APP}", "SK": "APPROVAL_AUDIT#2026-10-06T10:00:00Z",
                            "step": "discovery_review", "approved": True, "execution_arn": RUN})


# ── rules ─────────────────────────────────────────────────────────────────────

def test_he_compound_replatform(ts):
    best, _ = ts._evaluate_rules(HE)
    assert best["strategy"] == "replatform", best
    assert "manual deployment" in best["rationale"]
    assert "not containerized" in best["rationale"]


def test_te_vmware_with_lease_relocates_and_keyword_microservices_does_not_override(ts):
    best, candidates = ts._evaluate_rules(TE)
    assert best["strategy"] == "relocate", best
    assert "current_compute=on-prem VMware vSphere" in best["rationale"]
    assert "dc_lease_end=2027-03-31" in best["rationale"]
    # The keyword match is still visible, just not decisive.
    assert any(c["strategy"] == "refactor" and c["evidence"] == "keywords" for c in candidates)
    assert "microservices" in best["rationale"]


def test_vmware_without_exit_signal_is_not_relocate(ts):
    best, _ = ts._evaluate_rules({"current_compute": "VMware vSphere 7"})
    assert best["strategy"] != "relocate"


def test_vmware_with_no_change_text_relocates(ts):
    best, _ = ts._evaluate_rules({"current_compute": "vSphere", "notes": "DC exit by Q2, no code change allowed"})
    assert best["strategy"] == "relocate"


def test_single_replatform_signal_is_not_enough(ts):
    best, _ = ts._evaluate_rules({"deployment_method": "manual"})
    assert best["strategy"] == "rehost" and best["rationale"].startswith("Default")


def test_oracle_enterprise_licensing_counts_as_replatform_signal(ts):
    best, _ = ts._evaluate_rules({"deployment_method": "manual",
                                  "database": {"engine": "Oracle 12c", "licensing": "enterprise"}})
    assert best["strategy"] == "replatform"


def test_structured_refactor_needs_three_signals(ts):
    app = {"tier": "tier_1", "architecture": "microservices", "containerization": "kubernetes",
           "change_history": {"active_development": True, "release_frequency": "daily"}}
    best, _ = ts._evaluate_rules(app)
    assert best["strategy"] == "refactor" and "signal" in best["rationale"]


def test_legacy_language_rehost(ts):
    best, _ = ts._evaluate_rules({"language": "COBOL"})
    assert best["strategy"] == "rehost" and "legacy language" in best["rationale"]


def test_close_out_intent_still_wins(ts):
    best, _ = ts._evaluate_rules({**HE, "notes": "Application is deprecated; decommission after Q1."})
    assert best["strategy"] == "retire"


def test_keyword_only_refactor_still_works_without_structured_evidence(ts):
    best, _ = ts._evaluate_rules({"description": "target architecture is microservices and cloud-native"})
    assert best["strategy"] == "refactor"


def test_new_l2_fields_are_in_the_strategy_corpus(ts):
    corpus = ts._strategy_corpus({"current_compute": "VMware", "containerization": "none",
                                  "strategy_rationale": "x", "recommended_rationale": "y"})
    assert "vmware" in corpus and "none" in corpus
    assert "x" not in corpus.split() and "y" not in corpus.split()


def test_rules_are_deterministic(ts):
    assert ts._evaluate_rules(TE) == ts._evaluate_rules(dict(TE))


# ── strategy fields describe META.migration_strategy ──────────────────────────

def test_unconfirmed_recommendation_persists_recommended_fields(ts):
    _seed(ts, **HE)
    out = ts.recommend_strategy({"app_id": APP})
    meta = _meta(ts)
    assert out["persisted"] and meta["migration_strategy"] == "replatform"
    assert meta["recommended_strategy"] == "replatform"
    assert meta["strategy_confidence"] == meta["recommended_confidence"]
    assert meta["strategy_scores"]["replatform"] == meta["strategy_confidence"]


def test_confirmed_strategy_gets_its_own_confidence_and_rationale(ts):
    """APP-TE: confirmed rehost was shown with refactor's keyword rationale."""
    _seed(ts, **TE, migration_strategy="rehost")
    _confirm_in_current_run(ts)
    out = ts.recommend_strategy({"app_id": APP})
    meta = _meta(ts)
    assert out["persisted"] is False and meta["migration_strategy"] == "rehost"
    assert meta["recommended_strategy"] == "relocate"
    assert meta["strategy_rationale"].startswith("Human-confirmed rehost; engine recommended relocate (")
    assert "Matched trigger(s): ['microservices']" != meta["strategy_rationale"]
    # no evidence for rehost → the floor, not relocate's confidence
    assert meta["strategy_confidence"] == str(ts.CONFIDENCE_FLOOR)


def test_confirmed_strategy_with_evidence_reports_that_evidence(ts):
    _seed(ts, **TE, migration_strategy="refactor")
    _confirm_in_current_run(ts)
    ts.recommend_strategy({"app_id": APP})
    meta = _meta(ts)
    assert meta["strategy_confidence"] == meta["strategy_scores"]["refactor"]


def test_confirmed_strategy_agreeing_with_engine(ts):
    _seed(ts, **HE, migration_strategy="replatform")
    _confirm_in_current_run(ts)
    ts.recommend_strategy({"app_id": APP})
    meta = _meta(ts)
    assert meta["strategy_rationale"].startswith("Human-confirmed replatform; engine agrees (")
    assert meta["strategy_confidence"] == meta["recommended_confidence"]


def test_workflow_and_tool_share_rationale_wording(ts):
    """workflow._confirmed_strategy_rationale (discovery gate) == tool_strategy._confirmed_note."""
    from backend.src.api.v1.workflow import _NO_EVIDENCE_CONFIDENCE, _confirmed_strategy_rationale

    best = {"strategy": "rehost", "confidence": 45, "rationale": "Default: insufficient data"}
    meta = {"recommended_strategy": "rehost", "recommended_confidence": "45",
            "recommended_rationale": "Default: insufficient data", "strategy_scores": {"rehost": "45"}}
    conf, rationale = _confirmed_strategy_rationale("relocate", meta)
    assert rationale == ts._confirmed_note("relocate", best)
    assert conf == str(_NO_EVIDENCE_CONFIDENCE) == str(ts.CONFIDENCE_FLOOR)
