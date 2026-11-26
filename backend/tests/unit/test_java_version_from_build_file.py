"""The build file's declared Java release beats a lower META runtime.jdk_version.

APP-HE (2026-11-23): pom.xml declares release 25, META said jdk 21, the generated CodeBuild
ran corretto21 and failed "release version 25 not supported". A lower build-file version never
downgrades META, and a planned java-version-upgrade target still wins (CI builds transformed code).
"""
from __future__ import annotations

import pytest

from backend.tests.unit.test_journey_exec_infra import staged, te  # noqa: F401 — fixtures/helpers


def _with_build_file(mod, monkeypatch, java):
    monkeypatch.setattr(mod, "_build_module_facts",
                        lambda repo, build_dir, branch: {"java_version": java, "build_tool": "maven",
                                                         "has_dockerfile": False})


@pytest.mark.parametrize("meta_jdk,pom,expected", [("21", "25", "25"), ("21", "17", "21"), ("", "25", "25")])
def test_build_file_release_wins_when_higher(te, monkeypatch, meta_jdk, pom, expected):  # noqa: F811
    mod, env = te
    _with_build_file(mod, monkeypatch, pom)
    env.seed("APP-HE", runtime={"jdk_version": meta_jdk} if meta_jdk else {}, transformation_steps=[])
    out = mod.generate_cicd({"app_id": "APP-HE"})
    assert out["status"] == "staged", out
    spec = staged(env, "APP-HE", "cicd", "buildspec.yml")
    assert f"java: corretto{expected}" in spec
    assert f'amazoncorretto:{expected}-al2023' in staged(env, "APP-HE", "cicd", "Dockerfile")


def test_maven_buildspec_jacoco_supports_java25():
    from pathlib import Path

    spec = (Path(__file__).resolve().parents[3] / "backend/resources/templates/codepipeline/buildspec-java-maven.yml").read_text()
    version = spec.split('JACOCO_VERSION: "', 1)[1].split('"', 1)[0]
    assert tuple(int(x) for x in version.split(".")) >= (0, 8, 14), version
