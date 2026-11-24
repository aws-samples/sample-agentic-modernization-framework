"""Pytest support for the lambda-src tool tests.

Some sibling test modules (e.g. test_tool_documents.py) install a lightweight
``boto3`` stub into ``sys.modules`` at import time *only if* boto3 is not already
present. Importing the real boto3 here — before any test module is collected —
keeps that guard False, so the real library stays loaded and moto-based tests
import cleanly instead of hitting the stub.

``risk_scoring`` is imported by invoke_runtime.py but does not live in this
directory on disk — Terraform's archive_file data source copies it in from
backend/src/core/risk_scoring.py at package-build time (see lambdas.tf), so it
is only ever present inside the deployed Lambda zip, never in source control
here. Running tests standalone (outside that Terraform step) therefore fails
to import any module that imports invoke_runtime, with a plain
ModuleNotFoundError. Point sys.path at the real source location instead of
checking in a duplicate copy that could drift from it.
"""
import os
import sys
from pathlib import Path

# The Lambda runtime always sets AWS_REGION and the tool modules read it without a
# fallback (no baked region literal), so provide it for the standalone test run.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["AWS_REGION"])

import boto3  # noqa: E402,F401
import boto3.dynamodb.conditions  # noqa: E402,F401

_RISK_SCORING_SRC_DIR = Path(__file__).resolve().parents[4] / "backend" / "src" / "core"
if str(_RISK_SCORING_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_RISK_SCORING_SRC_DIR))
