"""The worker image must carry the JDK + Maven that ATX Java transformations build with."""
from __future__ import annotations

from pathlib import Path


def test_worker_image_pins_java_and_maven():
    repo_root = Path(__file__).resolve().parents[3]
    dockerfile = (repo_root / "docker/Dockerfile.worker").read_text()

    assert "java-21-amazon-corretto-jdk" in dockerfile
    assert "ARG MAVEN_VERSION=3.9.9" in dockerfile
    assert 'ENV JAVA_HOME="/usr/lib/jvm/java-21-amazon-corretto"' in dockerfile
    assert 'ENV MAVEN_HOME="/opt/apache-maven-${MAVEN_VERSION}"' in dockerfile
