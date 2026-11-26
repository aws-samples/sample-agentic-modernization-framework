"""
Dockerfile fallback generator for the transform step.

AWS Transform (ATX) may modernize code without emitting a root Dockerfile, but the
downstream CI/CD (CodePipeline → CodeBuild → ECS) needs one to build an image.
After the ATX transforms run, `ensure_dockerfile` detects the repo's build system
and writes a standard multi-stage Dockerfile at the repo root *only if one is
absent* — so a hand-written or ATX-produced Dockerfile is never overwritten.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.src.config import settings
from backend.src.worker.config import logger

# Common locations a Dockerfile might already live — if any exists, we do nothing.
_DOCKERFILE_NAMES = ("Dockerfile", "dockerfile", "Containerfile")

# Detection: marker file at repo root → (stack, Dockerfile template).
_TEMPLATES: dict[str, str] = {
    "java-maven": """# syntax=docker/dockerfile:1
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /app
COPY . .
RUN mvn -q -B clean package -DskipTests
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /app/target/*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
""",
    "java-gradle": """# syntax=docker/dockerfile:1
FROM gradle:8-jdk21 AS build
WORKDIR /app
COPY . .
RUN gradle --no-daemon clean bootJar -x test || gradle --no-daemon clean build -x test
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /app/build/libs/*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
""",
    "node": """# syntax=docker/dockerfile:1
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci || npm install
COPY . .
RUN npm run build --if-present
FROM node:20-alpine
WORKDIR /app
COPY --from=build /app .
EXPOSE 3000
CMD ["npm", "start"]
""",
    "python": """# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY requirements*.txt pyproject.tom*l ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install --no-cache-dir .
COPY . .
EXPOSE 8000
CMD ["python", "-m", "app"]
""",
    "dotnet": """# syntax=docker/dockerfile:1
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY . .
RUN dotnet publish -c Release -o /app
FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
EXPOSE 80
ENTRYPOINT ["dotnet", "app.dll"]
""",
    "go": """# syntax=docker/dockerfile:1
FROM golang:1.22 AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -o /app/server ./...
FROM gcr.io/distroless/static
COPY --from=build /app/server /server
EXPOSE 8080
ENTRYPOINT ["/server"]
""",
}


def _detect_stack(repo_dir: Path) -> str | None:
    """Detect the build system from root marker files. Returns a template key."""
    has = lambda *names: any((repo_dir / n).exists() for n in names)  # noqa: E731
    if has("pom.xml"):
        return "java-maven"
    if has("build.gradle", "build.gradle.kts", "settings.gradle"):
        return "java-gradle"
    if has("package.json"):
        return "node"
    if has("requirements.txt", "pyproject.toml", "setup.py"):
        return "python"
    if has("go.mod"):
        return "go"
    if next(repo_dir.glob("*.csproj"), None) or next(repo_dir.glob("*.sln"), None):
        return "dotnet"
    return None


def _render(stack: str) -> str:
    """The stack's Dockerfile with every FROM image prefixed by `dockerfile_base_registry`
    (a pull-through/mirror registry); empty → the public images unchanged."""
    registry = settings.dockerfile_base_registry
    template = _TEMPLATES[stack]
    if not registry:
        return template
    prefix = registry if registry.endswith("/") else registry + "/"
    return re.sub(r"^FROM (\S+)", lambda m: f"FROM {prefix}{m.group(1)}", template, flags=re.MULTILINE)


def _has_dockerfile(repo_dir: Path) -> bool:
    return any((repo_dir / n).exists() for n in _DOCKERFILE_NAMES)


def ensure_dockerfile(repo_dir: Path) -> dict:
    """Write a standard Dockerfile at the repo root if none exists.

    Returns a status dict: {"status": "exists"|"generated"|"skipped", "stack": ...}.
    Never overwrites an existing Dockerfile.
    """
    repo_dir = Path(repo_dir)
    if _has_dockerfile(repo_dir):
        logger.info("Dockerfile already present in %s — leaving as-is", repo_dir.name)
        return {"status": "exists", "stack": None}

    stack = _detect_stack(repo_dir)
    if not stack:
        logger.warning("No known build system detected in %s — skipping Dockerfile generation", repo_dir.name)
        return {"status": "skipped", "stack": None}

    (repo_dir / "Dockerfile").write_text(_render(stack))
    logger.info("Generated %s Dockerfile at root of %s", stack, repo_dir.name)
    return {"status": "generated", "stack": stack}
