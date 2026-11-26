"""Tests for spec-driven transformation steps in tool_execution.

Pins the contract that lets an AIDLC ``units.md`` carry a DIFFERENT ATX
instruction per unit through to the CLI's ``additionalPlanContext``, while the
legacy shape (a list of bare transform-definition names) keeps working — live
app records still hold plain string lists, so a regression here would silently
stop transforms for every existing application.

    DYNAMODB_TABLE=test_table AWS_DEFAULT_REGION=us-east-1 pytest test_tool_execution_units.py -q
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
REPO = "https://github.com/example-org/sample-app"

UNIT_CONTEXT = (
    "Upgrade this Dropwizard service from Java 17 to Java 21. Preserve the decoration "
    "order exactly as Retry.decorateSupplier(retry, CircuitBreaker.decorateSupplier(...))."
)


def _seed(table, transformation_steps):
    table.put_item(Item={
        "PK": f"APP#{APP}", "SK": "META",
        "app_id": APP, "language": "java", "target_version": "21",
        "transformation_steps": transformation_steps,
    })
    table.put_item(Item={
        "PK": f"APP#{APP}", "SK": f"REPO#{REPO}", "url": REPO, "branch": "main",
    })


@pytest.fixture
def steps_for(monkeypatch):
    """Build transformation steps without launching ECS.

    ``start_code_transformation`` ends in an ECS RunTask; the step list is the
    contract under test, so the launch is stubbed and the steps captured.
    """
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
        table = ddb.Table(TABLE_NAME)

        import tool_execution

        monkeypatch.setattr(tool_execution, "table", table, raising=False)
        captured = {}

        def fake_launch(job_id, app_id, mode, td_name, target_version="",  # nosec B107 — test fixture (fake token), not a credential
                        task_token="", extra_env=None):
            """Match the real signature; steps travel as JSON in extra_env."""
            import json as _json
            captured["steps"] = _json.loads((extra_env or {}).get("WORKBENCH_STEPS", "[]"))
            captured["mode"] = mode
            return {"task_arn": "arn:aws:ecs:::task/stub", "status": "launched"}

        monkeypatch.setattr(tool_execution, "_launch_ecs_task", fake_launch, raising=False)

        def run(transformation_steps, **args):
            captured.clear()
            _seed(table, transformation_steps)
            tool_execution.start_code_transformation({"app_id": APP, **args})
            return captured.get("steps", [])

        yield run


def test_legacy_string_list_still_works(steps_for):
    """A bare list of TD names must keep producing a derived context.

    This is the shape live records hold today.
    """
    steps = steps_for(["AWS/java-version-upgrade"])
    assert len(steps) == 1
    assert steps[0]["td_name"] == "AWS/java-version-upgrade"
    assert steps[0]["additional_context"] == "The target Java version to upgrade to is Java 21."
    assert steps[0]["context_source"] == "derived"


def test_unit_context_reaches_the_step(steps_for):
    """A unit-derived entry must pass its OWN instruction through verbatim."""
    steps = steps_for([
        {"td_name": "AWS/java-version-upgrade", "additional_context": UNIT_CONTEXT},
    ])
    assert len(steps) == 1
    assert steps[0]["additional_context"] == UNIT_CONTEXT
    assert steps[0]["context_source"] == "unit"


def test_each_unit_keeps_its_own_context(steps_for):
    """The point of the change: per-unit instructions must not collapse.

    ``build_context`` could not express this — it applies one value to every
    step, so two units would receive identical instructions.
    """
    steps = steps_for([
        {"td_name": "AWS/java-version-upgrade", "additional_context": "First: Java 21."},
        {"td_name": "AWS/java-version-upgrade", "additional_context": "Second: add Micrometer."},
    ])
    assert [s["additional_context"] for s in steps] == ["First: Java 21.", "Second: add Micrometer."]
    assert all(s["context_source"] == "unit" for s in steps)


def test_mixed_shapes_are_tolerated(steps_for):
    """A part-migrated record must not break: dicts and strings can coexist."""
    steps = steps_for([
        "AWS/java-version-upgrade",
        {"td_name": "AWS/java-performance-optimization", "additional_context": UNIT_CONTEXT},
    ])
    assert [s["context_source"] for s in steps] == ["derived", "unit"]
    assert steps[1]["additional_context"] == UNIT_CONTEXT


def test_unit_target_version_overrides_meta(steps_for):
    """A unit may pin its own target without a context sentence."""
    steps = steps_for([{"td_name": "AWS/java-version-upgrade", "target_version": "17"}])
    assert steps[0]["additional_context"] == "The target Java version to upgrade to is Java 17."
    assert steps[0]["context_source"] == "derived"


def test_explicit_argument_still_wins(steps_for):
    """An explicit call argument outranks the unit's instruction.

    Preserves the manual override an operator uses to re-run one step.
    """
    steps = steps_for(
        [{"td_name": "AWS/java-version-upgrade", "additional_context": UNIT_CONTEXT}],
        build_context="Operator override.",
    )
    assert steps[0]["additional_context"] == "Operator override."


def test_malformed_entries_are_dropped_not_crashed(steps_for):
    """An entry with no usable TD name must be skipped, not raise.

    A dict missing td_name would otherwise launch ATX with an empty -n flag.
    """
    steps = steps_for([
        {"additional_context": "orphaned, no td_name"},
        {"td_name": "AWS/java-version-upgrade", "additional_context": UNIT_CONTEXT},
    ])
    assert len(steps) == 1
    assert steps[0]["td_name"] == "AWS/java-version-upgrade"


def test_build_command_flows_from_the_unit(steps_for):
    """A unit may name the command ATX runs to verify its own changes.

    ATX accepts a ``buildCommand`` and runs it to check the transformation still
    compiles. Supplying none makes it report "The build command for this project
    is empty (not configured)" and complete the step anyway, so the pipeline
    reports success over code that has never been built.
    """
    steps = steps_for([{
        "td_name": "AWS/java-version-upgrade",
        "additional_context": UNIT_CONTEXT,
        "build_command": "mvn -B -q verify",
    }])
    assert steps[0]["build_command"] == "mvn -B -q verify"


def test_build_command_defaults_to_empty_for_worker_detection(steps_for):
    """Absent a unit value the key is present and empty, not missing.

    Empty means "detect from the repository" in the worker. The key must still
    exist so the worker reads a defined value rather than relying on .get()
    behaviour that a future refactor could change.
    """
    steps = steps_for([{"td_name": "AWS/java-version-upgrade"}])
    assert steps[0]["build_command"] == ""


def test_legacy_string_entries_carry_an_empty_build_command(steps_for):
    """The legacy bare-name shape must not lose the new key.

    Live records still hold plain string lists; a KeyError in the worker would
    stop transforms for every existing application.
    """
    steps = steps_for(["AWS/java-version-upgrade"])
    assert steps[0]["build_command"] == ""


def test_repo_source_ref_reaches_the_worker_step(steps_for, monkeypatch):
    """A migration may create a review branch from an explicit legacy commit.

    The source ref must travel in the step; without it the worker correctly
    refuses to invent a branch base when the remote target branch is absent.
    """
    import tool_execution

    original_query = tool_execution.table.query

    def query_with_source_ref(**kwargs):
        response = original_query(**kwargs)
        for item in response.get("Items", []):
            if item.get("SK", "").startswith("REPO#"):
                item["branch"] = "demo/amf-modernization"
                item["source_ref"] = "347ec91"
                item["review_base_branch"] = "demo/legacy-baseline"
        return response

    monkeypatch.setattr(tool_execution.table, "query", query_with_source_ref)
    steps = steps_for([{"td_name": "AWS/java-version-upgrade"}])
    assert steps[0]["repo_branch"] == "demo/amf-modernization"
    assert steps[0]["repo_source_ref"] == "347ec91"
    assert steps[0]["repo_review_base_branch"] == "demo/legacy-baseline"
