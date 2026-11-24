"""Linked repos must be clonable URLs (UAT APP-CLI was linked as a bare repo name,
so ATX cloned nothing). Whitespace is stripped (APP-TE's url had a leading tab)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.src.api.v1.portfolio import RepoModel, repo_url_error


@pytest.mark.parametrize("url", [
    "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/java-on-aws",
    "codecommit::us-east-1://java-on-aws",
    "",
])
def test_codecommit_urls_pass(url):
    assert repo_url_error(url) is None


@pytest.mark.parametrize("url", [
    "serverless-java-frameworks-samples", "github.com/org/app", "https://",
    "https://github.com/org/app.git", "ssh://git@host/org/app.git", "git@github.com:org/app.git",
    "https://gitlab.example.com/team/app.git",
    "https://git-codecommit.us-east-1.amazonaws.com.evil.com/v1/repos/app",
])
def test_non_codecommit_urls_are_rejected(url):
    assert "not an AWS CodeCommit clone URL" in repo_url_error(url)


def test_repo_model_strips_and_validates():
    repo = RepoModel(url="\t https://git-codecommit.us-east-1.amazonaws.com/v1/repos/svc ", branch=" main ")
    assert repo.url == "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/svc"
    assert repo.branch == "main"
    with pytest.raises(ValidationError):
        RepoModel(url="serverless-java-frameworks-samples")


async def test_put_repos_replaces_the_linked_set():
    """Removing a repo in the edit modal must unlink it (it used to stay → ATX cloned it)."""
    from unittest.mock import patch

    import boto3
    from moto import mock_aws

    from backend.src.api.v1 import portfolio

    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={"PK": "APP#A", "SK": "META", "app_id": "A"})
        t.put_item(Item={"PK": "APP#A", "SK": "REPO#old", "name": "old", "url": "https://h/old"})
        new = "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/unishop-monolith-to-microservices"
        with patch.object(portfolio, "get_table", return_value=t):
            resp = portfolio.update_app("A", {"repos": [{"url": new, "branch": "atx-run1"}]})
        assert resp.status == "success", resp
        sks = sorted(i["SK"] for i in t.scan()["Items"])
        assert sks == ["META", "REPO#unishop-monolith-to-microservices"]
        assert t.get_item(Key={"PK": "APP#A", "SK": "META"})["Item"]["repo_count"] == 1
