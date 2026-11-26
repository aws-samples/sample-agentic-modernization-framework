"""Buildspecs pull Docker Official Images through the ECR Public mirror (Docker Hub 429s CodeBuild)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TPL = Path(__file__).resolve().parents[2] / "resources/templates/codepipeline"
SPECS = ["buildspec-java-maven.yml", "buildspec-java-gradle.yml", "buildspec-node-npm.yml", "buildspec-python-pip.yml"]


def _awk_program(spec: str) -> str:
    txt = (TPL / spec).read_text()
    return re.search(r"awk -v M=\"\$DOCKERHUB_MIRROR\" '(.*?)' \"\$BUILD_DIR/Dockerfile\"", txt, re.S).group(1)


@pytest.mark.parametrize("spec", SPECS)
def test_every_container_buildspec_mirrors_before_docker_build(spec):
    txt = (TPL / spec).read_text()
    assert 'DOCKERHUB_MIRROR: "{{dockerhub_mirror}}"' in txt
    assert txt.index("DOCKERHUB_MIRROR\" ] && [ -f") < txt.index("docker build -t ${ECR_URI}")


@pytest.mark.skipif(not shutil.which("awk"), reason="awk not available")
def test_rewrite_only_unqualified_official_images(tmp_path):
    dockerfile = "\n".join([
        "FROM amazoncorretto:21",
        "FROM --platform=linux/amd64 python:3.12-slim AS build",
        "FROM build AS test",
        "from node:20-alpine as web",
        "FROM bitnami/nginx:1.25",
        "FROM public.ecr.aws/amazoncorretto/amazoncorretto:21-al2023",
        "FROM ${BASE_IMAGE}",
        "FROM scratch",
        "RUN echo FROM nothing",
    ]) + "\n"
    out = subprocess.run(["awk", "-v", "M=public.ecr.aws/docker/library", _awk_program(SPECS[0])],
                         input=dockerfile, capture_output=True, text=True, check=True).stdout.splitlines()
    assert out[0] == "FROM public.ecr.aws/docker/library/amazoncorretto:21"
    assert out[1] == "FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim AS build"
    assert out[2] == "FROM build AS test"                                   # stage alias untouched
    assert out[3] == "from public.ecr.aws/docker/library/node:20-alpine as web"
    assert out[4] == "FROM bitnami/nginx:1.25"                              # namespaced: untouched
    assert out[5] == "FROM public.ecr.aws/amazoncorretto/amazoncorretto:21-al2023"
    assert out[6] == "FROM ${BASE_IMAGE}" and out[7] == "FROM scratch"
    assert out[8] == "RUN echo FROM nothing"


def test_tool_execution_renders_the_mirror():
    src = (Path(__file__).resolve().parents[3] / "infra/environments/agentcore/lambda-src/tool_execution.py").read_text()
    assert '"dockerhub_mirror": _DOCKERHUB_MIRROR' in src
    assert 'os.environ.get("DOCKERHUB_MIRROR", "public.ecr.aws/docker/library")' in src
