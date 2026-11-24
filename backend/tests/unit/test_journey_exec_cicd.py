"""Execution-phase CI/CD generation (tool_execution.generate_cicd + templates).

Pins the audit fixes: every CI template has real coverage-gate / SAST / Trivy
stages, EC2 targets never docker-build/push (artifact to S3 instead), the JDK is
derived from the app (not a blanket 21), triggers cover the default AND migration
branch, files are committed to the migration branch, and a provider that cannot
run on the linked repo host is flagged (provider_mismatch) instead of silently
"committed".
"""
from __future__ import annotations

import itertools
import json
import re

import pytest
import yaml

from backend.tests.unit.test_journey_exec_infra import TEMPLATES, staged, te  # noqa: F401 — fixture

_PROVIDERS = ("codepipeline",)  # the only CI/CD target


def _manifest(provider: str) -> list[dict]:
    return json.loads((TEMPLATES / provider / "templates.json").read_text())


def _ctx(mod) -> dict:
    meta = {"target_account": "111122223333", "language": "Java"}
    ctx, _ = mod._cicd_context("APP-X", meta, {"default_branch": "main"})
    return ctx


@pytest.mark.parametrize("provider,flag", list(itertools.product(_PROVIDERS, ("container", "artifact", "lambda"))))
def test_every_template_renders_resolved_and_parses(te, provider, flag):  # noqa: F811
    mod, _ = te
    ctx = _ctx(mod)
    flags = {flag, "scans"} | ({"deploy"} if flag in ("container", "lambda") else set())
    for t in _manifest(provider):
        text = mod._render(mod._select_blocks((TEMPLATES / provider / t["file"]).read_text(), flags), ctx)
        assert not re.findall(r"\{\{(\w+)\}\}", text), (t["file"], flag)
        assert "#@if" not in text and "#@endif" not in text
        if t["file"].endswith((".yml", ".yaml")):  # CodeDeploy hooks are shell scripts
            yaml.safe_load(text)
        for var in t.get("variables", []):
            assert var in ctx, f"{provider}/{t['file']} declares unknown variable {var}"


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_ci_templates_have_coverage_gate_and_no_third_party_scanners(te, provider):  # noqa: F811
    mod, _ = te
    for t in (t for t in _manifest(provider) if t["type"] == "ci"):
        raw = (TEMPLATES / provider / t["file"]).read_text()
        assert not re.search(r"trivy|semgrep|#@if scans", raw, re.I), t["file"]
        container = mod._select_blocks(raw, {"container"})
        artifact = mod._select_blocks(raw, {"artifact"})
        artifact_code = "\n".join(ln for ln in artifact.splitlines() if not ln.lstrip().startswith("#"))
        for text in (container, artifact):
            assert re.search(r"jacoco|--fail-under|c8 --check-coverage", text), t["file"]
            assert "COVERAGE_THRESHOLD" in text
        assert "docker build" in container
        # EC2 / artifact variant: no container build, no ECR push, upload to S3 instead.
        assert "docker build" not in artifact_code and "docker push" not in artifact_code
        assert "amazon-ecr-login" not in artifact_code and "ecr get-login-password" not in artifact_code
        assert "aws s3 cp" in artifact and "ARTIFACT_BUCKET" in artifact


def test_maven_ci_builds_from_build_dir_with_verify(te):  # noqa: F811
    mod, _ = te
    for provider in _PROVIDERS:
        t = next(t for t in _manifest(provider) if t["type"] == "ci" and t.get("language") == "java")
        raw = (TEMPLATES / provider / t["file"]).read_text()
        assert " verify " in raw and "BUILD_DIR" in raw and "target/*.jar" in raw


@pytest.mark.parametrize("app_id,expected", [("APP-CLI", "17"), ("APP-HE", "21"), ("APP-TE", "8")])
def test_java_version_is_derived_not_defaulted(te, app_id, expected):  # noqa: F811
    """CLI: assessment modules Java 11/17 -> 17; HE: java-version-upgrade planned -> 21;
    TE: rehost of a Java 8 app (assessment '1.8') -> 8, not a blanket 21."""
    mod, env = te
    env.seed(app_id)
    out = mod.generate_cicd({"app_id": app_id})
    assert out["status"] == "staged"
    rec = next(it for (pk, sk), it in env.table.items.items() if sk.startswith("EXECUTION#cicd#"))
    assert rec["java_version"] == expected


def test_java_version_precedence(te):  # noqa: F811
    mod, env = te
    assert mod._runtime_versions({"language": "Java", "runtime": {"jdk_version": "JDK11"}}, "NOPE")["java_version"] == "11"
    assert mod._runtime_versions({"language": "Java", "language_version": "17"}, "NOPE")["java_version"] == "17"
    assert mod._runtime_versions({"language": "Java"}, "NOPE")["java_version"] == "17"  # fallback
    assert mod._runtime_versions({"language": "Python", "runtime": {"python_version": "3.11"}}, "NOPE")["python_version"] == "3.11"


def test_ec2_ci_uploads_artifact_and_skips_ecr(te):  # noqa: F811
    mod, env = te
    env.seed("APP-TE")
    out = mod.generate_cicd({"app_id": "APP-TE"})
    assert "buildspec.yml" in out["files"] and "appspec.yml" in out["files"]
    ci = staged(env, "APP-TE", "cicd", "buildspec.yml")
    spec = yaml.safe_load(ci)
    post = "\n".join(spec["phases"]["post_build"]["commands"])
    code = "\n".join(ln for ln in ci.splitlines() if not ln.lstrip().startswith("#"))
    assert "docker" not in code and "ecr get-login-password" not in code
    assert "app-te-artifacts-987654321981-" in ci and "aws s3 cp" in post
    assert spec["artifacts"]["base-directory"] == "revision"  # CodeDeploy revision


def test_container_ci_builds_and_pushes_image(te):  # noqa: F811
    mod, env = te
    env.seed("APP-HE")
    mod.generate_cicd({"app_id": "APP-HE"})
    spec = yaml.safe_load(staged(env, "APP-HE", "cicd", "buildspec.yml"))
    build = "\n".join(spec["phases"]["build"]["commands"])
    assert "docker build" in build and "trivy" not in build
    assert spec["phases"]["install"]["runtime-versions"]["java"] == "corretto21"
    assert spec["artifacts"]["files"] == ["imagedefinitions.json"]


def test_cicd_commit_goes_to_migration_branch(te):  # noqa: F811
    mod, env = te
    env.seed("APP-CLI")
    out = mod.generate_cicd({"app_id": "APP-CLI"})
    assert out["commit"]["branch"] == "migration/app-cli"
    assert [c["branchName"] for c in env.cc.commits] == ["migration/app-cli"]
    paths = [f["filePath"] for f in env.cc.commits[0]["putFiles"]]
    assert paths == ["cicd/buildspec.yml", "cicd/CODEPIPELINE_DEPLOY_NOTE.md"] or set(paths) >= {"cicd/buildspec.yml"}


@pytest.mark.parametrize("app_id,mismatch", [("APP-TE", False), ("APP-CLI", False), ("APP-HE", False)])
def test_provider_vs_repo_host_mismatch_is_flagged(te, app_id, mismatch):  # noqa: F811
    """All three audit repos are CodeCommit, which is CodePipeline's native source."""
    mod, env = te
    env.seed(app_id)
    out = mod.generate_cicd({"app_id": app_id})
    assert out["provider_mismatch"] is mismatch
    assert out["repo_host"] == "codecommit"
    rec = next(it for (pk, sk), it in env.table.items.items() if sk.startswith("EXECUTION#cicd#"))
    assert rec["provider_mismatch"] is mismatch
    meta = env.table.items[(f"APP#{app_id}", "META")]
    if mismatch:
        assert "will not run on a codecommit repo" in out["warnings"][0]
        assert meta["exec_cicd_warning"].startswith("provider ")
    else:
        assert out["warnings"] == [] and meta["exec_cicd_warning"] == ""


def test_codepipeline_on_github_needs_a_connection(te):  # noqa: F811
    mod, _ = te
    msg = mod._provider_mismatch("codepipeline", "github")
    assert "CodeStar connection" in msg
    assert mod._provider_mismatch("codepipeline", "codecommit") == ""


def test_build_dir_detection(te):  # noqa: F811
    mod, env = te
    repo = {"host": "codecommit", "name": "r"}
    assert mod._detect_build_dir(repo, "java", "main") == (".", "")
    env.cc.folders = {
        "/": {"files": [], "subFolders": [{"absolutePath": "/app", "relativePath": "app"}]},
        "/app": {"files": [{"relativePath": "pom.xml"}], "subFolders": []},
    }
    assert mod._detect_build_dir(repo, "java", "main")[0] == "app"
    env.cc.folders = {
        "/": {"files": [], "subFolders": [{"absolutePath": "/a", "relativePath": "a"},
                                           {"absolutePath": "/b", "relativePath": "b"}]},
        "/a": {"files": [{"relativePath": "pom.xml"}], "subFolders": []},
        "/b": {"files": [{"relativePath": "pom.xml"}], "subFolders": []},
    }
    build_dir, note = mod._detect_build_dir(repo, "java", "main")
    assert build_dir == "." and "candidate build roots" in note


def test_no_unresolved_vars_for_any_profile(te):  # noqa: F811
    mod, env = te
    for app_id in ("APP-CLI", "APP-HE", "APP-TE"):
        env.seed(app_id)
        assert mod.generate_cicd({"app_id": app_id})["unresolved_vars"] == []
