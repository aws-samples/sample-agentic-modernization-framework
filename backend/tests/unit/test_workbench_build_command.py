"""Tests for the build command the workbench hands to ATX.

ATX accepts a ``buildCommand`` and runs it to verify that a transformation still
compiles. When none is supplied it says so plainly --

    The build command for this project is empty (not configured)
    Build verification cannot be performed due to missing build toolchain

-- and completes the step regardless. The pipeline then reports a successful
transformation over source that has never been built, which is
indistinguishable from one that was. These tests pin the detection so that
failure mode cannot return silently.

    pytest backend/tests/unit/test_workbench_build_command.py -q
"""
from __future__ import annotations

from backend.src.worker.workbench import _detect_build_command


def test_maven_project_is_detected(tmp_path):
    """A pom.xml must yield a Maven compile command."""
    (tmp_path / "pom.xml").write_text("<project/>")
    cmd = _detect_build_command(tmp_path)
    assert cmd == "mvn -B -q compile"


def test_maven_command_is_non_interactive(tmp_path):
    """The command runs unattended in a Fargate task with no TTY.

    An interactive Maven prompt would hang until the ATX shell timeout rather
    than failing, burning the agent-minute budget on a stall.
    """
    (tmp_path / "pom.xml").write_text("<project/>")
    assert "-B" in _detect_build_command(tmp_path)


def test_gradle_wrapper_is_preferred_over_system_gradle(tmp_path):
    """A wrapper pins the build's own Gradle version.

    Using a system gradle against a project with a wrapper is how a build
    breaks for reasons unrelated to the transformation.
    """
    (tmp_path / "build.gradle").write_text("plugins {}")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    assert _detect_build_command(tmp_path).startswith("./gradlew")


def test_gradle_without_wrapper_falls_back(tmp_path):
    (tmp_path / "build.gradle").write_text("plugins {}")
    assert _detect_build_command(tmp_path) == "gradle --console=plain compileJava"


def test_kotlin_gradle_script_is_recognised(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("plugins {}")
    assert _detect_build_command(tmp_path) == "gradle --console=plain compileJava"


def test_unknown_project_returns_empty_not_a_guess(tmp_path):
    """An unrecognised project must yield "" so the caller can warn.

    Returning a plausible-looking guess would be worse than returning nothing:
    ATX would run a command that fails for reasons unrelated to the
    transformation, and the step would be marked failed on a false signal.
    """
    (tmp_path / "README.md").write_text("no build tool here")
    assert _detect_build_command(tmp_path) == ""


def test_maven_wins_when_both_are_present(tmp_path):
    """A polyglot repo must resolve deterministically, not by filesystem order."""
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "build.gradle").write_text("plugins {}")
    assert _detect_build_command(tmp_path) == "mvn -B -q compile"


def test_directory_named_like_a_build_file_is_ignored(tmp_path):
    """Detection must test for a FILE.

    A directory called pom.xml would otherwise produce a Maven command for a
    project Maven cannot build.
    """
    (tmp_path / "pom.xml").mkdir()
    assert _detect_build_command(tmp_path) == ""


def test_monorepo_subprojects_are_compiled_in_their_own_dirs(tmp_path):
    """unishop-monolith-to-microservices: no root build file → ATX ran 'uncompiled'."""
    (tmp_path / "MonoToMicroLegacy").mkdir()
    (tmp_path / "MonoToMicroLegacy" / "gradlew").write_text("")
    (tmp_path / "MonoToMicroLambda").mkdir()
    (tmp_path / "MonoToMicroLambda" / "pom.xml").write_text("<project/>")
    (tmp_path / "MonoToMicroAssets").mkdir()
    (tmp_path / ".github").mkdir()
    assert _detect_build_command(tmp_path) == (
        "(cd MonoToMicroLambda && mvn -B -q compile) && (cd MonoToMicroLegacy && ./gradlew --console=plain compileJava)")
