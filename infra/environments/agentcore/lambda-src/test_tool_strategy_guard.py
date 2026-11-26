"""Tests for recommend_strategy's guards against silently re-routing a workflow.

These pin a real incident. On 2026-08-26 a harness run classified APP-CATALOG —
a Tier-1 entitlement service — as 'repurchase' (buy a SaaS replacement). That
routes the state machine to close_out, so assessment, design, execution,
transform and validation were all skipped and the run still reported SUCCESS.

Three defects combined:

1. The trigger "erp" matched as a SUBSTRING inside "enterprise", in the string
   "licensing details (byol, open-source, enterprise)".
2. That string lives in `completeness_detail` — a field this pipeline derives.
   The scan read the whole record, including its own outputs.
3. `strategy_rationale` then recorded "matched trigger(s): ['erp']", so the next
   run matched "erp" in its OWN previous output. Self-reinforcing: the app could
   never escape the classification.
4. And the write was unconditional, overwriting a human-confirmed decision.

    DYNAMODB_TABLE=test_table AWS_DEFAULT_REGION=us-east-1 pytest test_tool_strategy_guard.py -q
"""
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DYNAMODB_TABLE", "test_table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

TABLE_NAME = os.environ["DYNAMODB_TABLE"]
APP = "APP-CATALOG"

# The exact string that caused the incident.
POISON = "licensing details (byol, open-source, enterprise)"


@pytest.fixture
def mod(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                       {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        import tool_strategy as ts
        monkeypatch.setattr(ts, "table", ddb.Table(TABLE_NAME), raising=False)
        yield ts


def _seed(mod, approved=False, **fields):
    mod.table.put_item(Item={"PK": f"APP#{APP}", "SK": "META", "app_id": APP, **fields})
    if approved:
        # The real confirmation signal: an append-only approval audit record,
        # written by the approve endpoint. NOT a field on META — that was the
        # bug in the first version of this guard.
        mod.table.put_item(Item={
            "PK": f"APP#{APP}", "SK": "APPROVAL_AUDIT#2026-08-26T04:29:58Z",
            "type": "APPROVAL_AUDIT", "app_id": APP, "step": "assessment_review",
            "approved": True, "actor": "dev@migration-framework.local",
        })


def _strategy(mod):
    return mod.table.get_item(Key={"PK": f"APP#{APP}", "SK": "META"})["Item"].get("migration_strategy")


# ── Defect 1: substring matching ──────────────────────────────────────────────

def test_erp_does_not_match_enterprise(mod):
    """The incident, reduced to one assertion."""
    assert not mod._mentions(POISON, "erp")


@pytest.mark.parametrize("corpus,trigger", [
    ("this is an enterprise application", "erp"),
    ("a simpler deployment model", "simple"),
    ("scrm-like naming", "crm"),
    ("the interpreter is slow", "erp"),
    ("itsmart tooling", "itsm"),
])
def test_triggers_do_not_match_inside_words(mod, corpus, trigger):
    assert not mod._mentions(corpus, trigger)


@pytest.mark.parametrize("corpus,trigger", [
    ("we run SAP ERP here", "erp"),
    ("this is a simple app", "simple"),
    ("salesforce crm", "crm"),
    ("lift-and-shift please", "lift-and-shift"),
    ("target is microservices", "microservices"),
])
def test_real_mentions_still_match(mod, corpus, trigger):
    """The guard must not make the rules useless."""
    assert mod._mentions(corpus.lower(), trigger)


# ── Defect 2 and 3: scanning the pipeline's own output ────────────────────────

def test_derived_fields_are_not_scanned(mod):
    """completeness_detail and strategy_rationale must be invisible to the scan."""
    _seed(mod, completeness_detail=POISON,
          strategy_rationale="matched trigger(s): ['erp']")
    corpus = mod._strategy_corpus(mod.table.get_item(
        Key={"PK": f"APP#{APP}", "SK": "META"})["Item"])
    assert "enterprise" not in corpus
    assert "erp" not in corpus


def test_transformation_steps_are_not_scanned(mod):
    """Spec instructions on the record must not steer the strategy.

    units.md context mentions containerisation, microservices and managed
    databases — all of which are triggers. Left in scope, writing a spec would
    change the app's strategy.
    """
    _seed(mod, transformation_steps=[
        {"td_name": "AWS/java-version-upgrade",
         "additional_context": "containerize this and move to microservices with a managed db"},
    ])
    corpus = mod._strategy_corpus(mod.table.get_item(
        Key={"PK": f"APP#{APP}", "SK": "META"})["Item"])
    for t in ("containerize", "microservices", "managed db"):
        assert t not in corpus, f"{t} leaked into the strategy corpus"


def test_the_incident_no_longer_reproduces(mod):
    """End to end: the exact record shape must not yield repurchase."""
    _seed(mod, completeness_detail=POISON, description="Catalog & Entitlement Service",
          tier="tier_1", data_classification="restricted")
    out = mod.recommend_strategy({"app_id": APP})
    assert out["strategy"] != "repurchase", out
    assert _strategy(mod) != "repurchase"


# ── Defect 4: overwriting a human decision ────────────────────────────────────

def test_confirmed_strategy_is_not_overwritten(mod):
    """The guard that stops a run silently re-routing itself."""
    _seed(mod, approved=True, migration_strategy="refactor",
          description="a simple lift-and-shift candidate")  # would recommend rehost
    out = mod.recommend_strategy({"app_id": APP})
    assert out["persisted"] is False
    assert out["confirmed"] is True
    assert out["strategy"] == "refactor"
    assert _strategy(mod) == "refactor", "the confirmed decision must survive"


def test_guard_keys_on_the_audit_trail_not_a_meta_field(mod):
    """Regression: the first version checked META for last_approved_at.

    The approve endpoint writes that to WORKFLOW#ACTIVE, so the guard silently
    never fired and the overwrite still happened in production.
    """
    _seed(mod, migration_strategy="refactor", last_approved_at="2026-08-26T04:29:58Z",
          description="a simple lift-and-shift candidate")
    # A last_approved_at on META alone is NOT the signal — no audit record here.
    out = mod.recommend_strategy({"app_id": APP})
    assert out["persisted"] is True, "a META field must not be mistaken for approval"

    # With a real audit record, the guard fires.
    _seed(mod, approved=True, migration_strategy="refactor",
          description="a simple lift-and-shift candidate")
    out = mod.recommend_strategy({"app_id": APP})
    assert out["persisted"] is False


def test_explicit_confirmation_marker_also_works(mod):
    _seed(mod, migration_strategy="refactor", strategy_confirmed_at="2026-08-26T04:29:58Z",
          description="a simple lift-and-shift candidate")
    out = mod.recommend_strategy({"app_id": APP})
    assert out["persisted"] is False and out["strategy"] == "refactor"


def test_unreadable_audit_trail_fails_safe(mod, monkeypatch):
    """If we cannot prove the strategy is unconfirmed, do not overwrite it.

    Declining to change a decision is recoverable; silently re-routing a
    workflow is not.
    """
    _seed(mod, migration_strategy="refactor", description="a simple app")

    def broken_query(**kw):
        raise RuntimeError("throttled")

    monkeypatch.setattr(mod.table, "query", broken_query, raising=False)
    out = mod.recommend_strategy({"app_id": APP})
    assert out["persisted"] is False
    assert out["strategy"] == "refactor"


def test_disagreement_is_reported_not_hidden(mod):
    """Silence would be worse than disagreement — the reviewer needs to see it."""
    _seed(mod, approved=True, migration_strategy="refactor",
          description="a simple app for datacenter closure")
    out = mod.recommend_strategy({"app_id": APP})
    assert out["agrees_with_confirmed"] is False
    assert out["recommendation"] == "rehost"
    assert "refactor" in out["rationale"] and "rehost" in out["rationale"]


def test_force_can_override_a_confirmed_strategy(mod):
    """An explicit decision must still be possible."""
    _seed(mod, approved=True, migration_strategy="refactor",
          description="a simple app for datacenter closure")
    out = mod.recommend_strategy({"app_id": APP, "force": True})
    assert out["persisted"] is True
    assert _strategy(mod) == "rehost"


def test_unconfirmed_strategy_is_still_written(mod):
    """A first-pass recommendation must persist as before."""
    _seed(mod, description="target architecture is microservices and cloud-native")
    out = mod.recommend_strategy({"app_id": APP})
    assert out["persisted"] is True
    assert out["strategy"] == "refactor"
    assert _strategy(mod) == "refactor"


def test_strategy_present_but_never_confirmed_is_overwritable(mod):
    """A seeded value with no approval timestamp is a guess, not a decision."""
    _seed(mod, migration_strategy="rehost",
          description="target architecture is microservices and cloud-native")
    out = mod.recommend_strategy({"app_id": APP})
    assert out["persisted"] is True
    assert _strategy(mod) == "refactor"


# ── Discovery validation regression (found 2026-10-02) ────────────────────────
#
# invoke_runtime.py's deterministic validate_phase requires strategy_confidence
# to be non-empty before Discovery can pass Check_Discovery. A confirmed
# strategy's branch returned persisted:False and skipped the update_item call
# ENTIRELY -- never writing strategy_confidence either. Every subsequent
# Discovery run on an app with a confirmed strategy failed validation forever
# (bounded re-run, then MarkFailed), discovered testing APP-CATALOG after an
# unrelated AgentCore Harness fix.

def test_confirmed_strategy_still_persists_confidence(mod):
    """The strategy stays locked, but confidence/rationale must still be written
    every run -- Validate_Discovery's deterministic check depends on it."""
    _seed(mod, approved=True, migration_strategy="refactor",
          description="a simple lift-and-shift candidate")
    out = mod.recommend_strategy({"app_id": APP})
    assert out["persisted"] is False
    assert out["strategy"] == "refactor"

    item = mod.table.get_item(Key={"PK": f"APP#{APP}", "SK": "META"})["Item"]
    assert item.get("strategy_confidence") not in (None, ""), (
        "strategy_confidence must be set even when the strategy itself is locked, "
        "or Discovery can never pass validation again for this app"
    )
    assert item.get("strategy_rationale"), "rationale should accompany the confidence score"


def test_confirmed_strategy_confidence_updates_on_rerun(mod):
    """Confidence must be refreshed each run, not just written once."""
    _seed(mod, approved=True, migration_strategy="refactor",
          description="a simple lift-and-shift candidate")  # recommends rehost
    mod.recommend_strategy({"app_id": APP})
    first = mod.table.get_item(Key={"PK": f"APP#{APP}", "SK": "META"})["Item"]["strategy_confidence"]

    # Re-seed with a richer corpus that should raise the would-have-recommended
    # confidence; confirms the write path runs on every call, not just once.
    _seed(mod, approved=True, migration_strategy="refactor",
          description="a simple lift-and-shift candidate with a hard deadline in 2 months")
    mod.recommend_strategy({"app_id": APP})
    second = mod.table.get_item(Key={"PK": f"APP#{APP}", "SK": "META"})["Item"]["strategy_confidence"]
    assert second is not None
