"""
Pending approvals must reflect the CURRENT execution only.

Task tokens are written per step and deleted only when that step is approved,
so a failed, aborted or superseded run leaves its gates behind. Offering those
gates makes the UI unusable: approving one sends SendTaskSuccess against a dead
token, nothing happens, and the workflow appears broken.

The inverse failure is worse, so it is pinned too: hiding a LIVE gate would hang
the workflow with no way to approve it. When staleness cannot be established,
the gate must still be shown.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

TABLE = "test_portfolio"
APP = "APP-CATALOG"

# The live run started at 12:55 today.
RUN_STARTED = "2026-08-25T12:55:37.956180+00:00"
# Its gate was written two minutes later — note the '...Z' format, which differs
# from the workflow record's '+00:00'. Both are UTC.
LIVE_GATE = "2026-08-25T12:57:40Z"
# Left over from a run four days earlier.
STALE_GATE = "2026-08-21T02:51:57Z"


def _table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _token(t, step, received):
    t.put_item(Item={
        "PK": f"APP#{APP}", "SK": f"TASK_TOKEN#{step}",
        "app_id": APP, "step": step, "task_token": f"tok-{step}",
        "message": f"Approval required: {step}", "received_at": received,
    })


@pytest.fixture
def wf(monkeypatch):
    with mock_aws():
        t = _table()
        from backend.src.db import helpers

        monkeypatch.setattr(helpers, "get_table", lambda: t)
        from backend.src.api.v1 import workflow

        monkeypatch.setattr(workflow, "get_table", lambda: t, raising=False)
        yield workflow, t


def _steps(resp):
    d = resp.data if hasattr(resp, "data") else resp
    return [p["step"] for p in d]


class TestStaleGatesAreHidden:
    def test_only_the_current_runs_gate_is_offered(self, wf):
        workflow, t = wf
        t.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE",
                         "app_id": APP, "started_at": RUN_STARTED})
        _token(t, "discovery_review", LIVE_GATE)
        for step in ("assessment_review", "migration_plan_review", "execution_review",
                     "validation_review", "handover_review"):
            _token(t, step, STALE_GATE)

        steps = _steps(workflow.get_pending_approvals(APP))
        assert steps == ["discovery_review"], f"stale gates leaked: {steps}"

    def test_stale_count_is_reported_not_silently_dropped(self, wf):
        workflow, t = wf
        t.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE",
                         "app_id": APP, "started_at": RUN_STARTED})
        _token(t, "discovery_review", LIVE_GATE)
        _token(t, "assessment_review", STALE_GATE)
        resp = workflow.get_pending_approvals(APP)
        assert resp.meta["stale_hidden"] == 1

    def test_mixed_formats_compare_correctly(self, wf):
        """The token uses '...Z', the workflow record '+00:00' with micros."""
        workflow, t = wf
        t.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE",
                         "app_id": APP, "started_at": RUN_STARTED})
        _token(t, "discovery_review", LIVE_GATE)
        assert _steps(workflow.get_pending_approvals(APP)) == ["discovery_review"]


class TestNeverHideALiveGate:
    """Failing closed here means hanging the workflow — so fail open instead."""

    def test_no_active_workflow_shows_everything(self, wf):
        workflow, t = wf
        _token(t, "discovery_review", LIVE_GATE)
        _token(t, "assessment_review", STALE_GATE)
        steps = _steps(workflow.get_pending_approvals(APP))
        assert set(steps) == {"discovery_review", "assessment_review"}, (
            "with no run to compare against, every gate must still be offered"
        )

    def test_workflow_without_started_at_shows_everything(self, wf):
        workflow, t = wf
        t.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE", "app_id": APP})
        _token(t, "discovery_review", LIVE_GATE)
        _token(t, "assessment_review", STALE_GATE)
        assert len(_steps(workflow.get_pending_approvals(APP))) == 2

    def test_token_without_received_at_is_kept(self, wf):
        workflow, t = wf
        t.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE",
                         "app_id": APP, "started_at": RUN_STARTED})
        _token(t, "mystery_review", "")
        assert _steps(workflow.get_pending_approvals(APP)) == ["mystery_review"]

    def test_gate_exactly_at_run_start_is_kept(self, wf):
        """Boundary: equal timestamps are not 'before', so the gate is live."""
        workflow, t = wf
        t.put_item(Item={"PK": f"APP#{APP}", "SK": "WORKFLOW#ACTIVE",
                         "app_id": APP, "started_at": RUN_STARTED})
        _token(t, "discovery_review", RUN_STARTED[:19] + "Z")
        assert _steps(workflow.get_pending_approvals(APP)) == ["discovery_review"]


class TestOrdering:
    def test_newest_gate_first(self, wf):
        workflow, t = wf
        _token(t, "older", "2026-08-25T12:00:00Z")
        _token(t, "newer", "2026-08-25T13:00:00Z")
        assert _steps(workflow.get_pending_approvals(APP))[0] == "newer"


class TestSharedFilterHelper:
    """`current_run_pending_tokens` is the single source of truth used by BOTH
    /pending and /active — /active is the list the UI actually renders, so the
    filter must live in one place and be exercised directly."""

    def test_hides_pre_run_tokens_keeps_current(self):
        from backend.src.api.v1.workflow import current_run_pending_tokens
        items = [
            {"step": "discovery_review", "received_at": LIVE_GATE},
            {"step": "assessment_review", "received_at": STALE_GATE},
        ]
        kept, stale = current_run_pending_tokens(items, RUN_STARTED)
        assert [k["step"] for k in kept] == ["discovery_review"]
        assert stale == 1

    def test_fails_open_without_started_at(self):
        from backend.src.api.v1.workflow import current_run_pending_tokens
        kept, stale = current_run_pending_tokens([{"step": "a", "received_at": STALE_GATE}], "")
        assert len(kept) == 1 and stale == 0

    def test_boundary_equal_timestamp_is_kept(self):
        from backend.src.api.v1.workflow import current_run_pending_tokens
        kept, stale = current_run_pending_tokens(
            [{"step": "discovery_review", "received_at": RUN_STARTED[:19] + "Z"}], RUN_STARTED
        )
        assert [k["step"] for k in kept] == ["discovery_review"] and stale == 0

    def test_newest_first(self):
        from backend.src.api.v1.workflow import current_run_pending_tokens
        kept, _ = current_run_pending_tokens(
            [{"step": "older", "received_at": "2026-08-25T12:00:00Z"},
             {"step": "newer", "received_at": "2026-08-25T13:00:00Z"}], "")
        assert kept[0]["step"] == "newer"

    def test_decision_summary_passes_through_when_present(self):
        """R1/R2: a gate token's decision_summary must survive the reshape."""
        from backend.src.api.v1.workflow import current_run_pending_tokens
        summary = {
            "what_changed": "Recommend REPLATFORM (72%)",
            "confidence_or_risk": "72% — missing language field",
            "if_approve": "Assessment runs",
            "if_reject": "Discovery re-runs",
            "if_retry": "Re-enrich",
        }
        kept, _ = current_run_pending_tokens(
            [{"step": "discovery_review", "received_at": LIVE_GATE, "decision_summary": summary}], ""
        )
        assert kept[0]["decision_summary"] == summary

    def test_decision_summary_absent_key_is_omitted(self):
        """Backward compatible: gates without a summary don't grow the key."""
        from backend.src.api.v1.workflow import current_run_pending_tokens
        kept, _ = current_run_pending_tokens(
            [{"step": "discovery_review", "received_at": LIVE_GATE}], ""
        )
        assert "decision_summary" not in kept[0]


class TestFreshRunClearsStaleTokens:
    """Root cause: tokens are otherwise deleted only on that step's approval, so a
    fresh run must wipe prior gates rather than inherit them."""

    def test_reset_deletes_task_tokens(self, wf):
        workflow, t = wf
        _token(t, "discovery_review", STALE_GATE)
        _token(t, "assessment_review", STALE_GATE)
        t.put_item(Item={"PK": f"APP#{APP}", "SK": "META", "app_id": APP, "status": "completed"})

        workflow._reset_app_for_new_run(t, APP)

        from boto3.dynamodb.conditions import Key
        left = t.query(
            KeyConditionExpression=Key("PK").eq(f"APP#{APP}") & Key("SK").begins_with("TASK_TOKEN#")
        ).get("Items", [])
        assert left == [], "a fresh run must clear prior gates"
