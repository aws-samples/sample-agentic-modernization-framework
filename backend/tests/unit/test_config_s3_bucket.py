"""The data bucket name is never guessed without the account ID."""
from unittest.mock import MagicMock, patch

import pytest

from backend.src.config import Settings


def _settings(**kw):
    return Settings(_env_file=None, project_prefix="amf", aws_region="us-east-1", s3_bucket="", **kw)


def test_explicit_bucket_wins():
    assert Settings(_env_file=None, s3_bucket="my-bucket").get_s3_bucket() == "my-bucket"


def test_derived_from_account():
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111122223333"}
    with patch("boto3.client", return_value=sts):
        assert _settings().get_s3_bucket() == "amf-data-111122223333-us-east-1"


def test_sts_failure_raises_instead_of_an_account_less_name():
    sts = MagicMock()
    sts.get_caller_identity.side_effect = Exception("no credentials")
    s = _settings()
    with patch("boto3.client", return_value=sts), pytest.raises(RuntimeError, match="S3_BUCKET"):
        s.get_s3_bucket()
    assert not s.s3_bucket
