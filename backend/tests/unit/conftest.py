"""Shared fixtures for unit tests."""

import os

import pytest

# Bypass auth for all unit tests — must be set before any app imports
os.environ["DEV_AUTH"] = "true"
os.environ["ENVIRONMENT"] = "dev"

# Also patch the already-loaded settings singleton
from backend.src.config import settings  # noqa: E402

settings.dev_auth = True
settings.environment = "dev"


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """Process-level caches (AWS clients, /active, governance, ATX list throttle)
    must not leak between tests that swap moto contexts / stubs."""
    from backend.src.core import aws

    def _clear():
        aws.clear_cache()
        try:
            from backend.src.api.v1 import governance, workbench, workflow

            workflow.invalidate_active_workflows_cache()
            governance.clear_cache()
            workbench.reset_atx_list_throttle()
        except ImportError:
            pass

    _clear()
    yield
    _clear()
