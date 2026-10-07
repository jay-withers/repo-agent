"""The root Dockerfile: base image pinning and the user the image runs as."""

from __future__ import annotations

from pathlib import Path

from repoagent.checks import container
from tests.factories import snapshot

DIGEST = "sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88"


def _both(dockerfile: str | None) -> set[str]:
    snap = snapshot(dockerfile=dockerfile)
    return {f.check for f in container.unpinned_base_image(snap) + container.runs_as_root(snap)}


def test_this_repositorys_own_dockerfile_is_clean() -> None:
    """Multi-stage, a stage alias reused, a digest, a numeric USER."""
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()

    assert _both(dockerfile) == set()


def test_no_dockerfile_is_silent() -> None:
    assert _both(None) == set()


def test_a_tag_without_a_digest_fires() -> None:
    findings = container.unpinned_base_image(
        snapshot(dockerfile="FROM python:3.14-slim\nUSER 10001\n")
    )

    assert len(findings) == 1
    assert findings[0].severity == "medium"
    assert "python:3.14-slim" in findings[0].detail


def test_stage_aliases_scratch_and_arg_images_have_nothing_to_pin() -> None:
    dockerfile = (
        "ARG BASE=python:3.14\n"
        f"FROM --platform=linux/amd64 python:3.14@{DIGEST} AS Builder\n"
        "FROM ${BASE} AS other\n"
        "FROM builder AS runtime\n"
        "FROM scratch\n"
        "USER 10001\n"
    )

    assert container.unpinned_base_image(snapshot(dockerfile=dockerfile)) == []


def test_no_user_in_the_final_stage_runs_as_root() -> None:
    findings = container.runs_as_root(snapshot(dockerfile=f"FROM python@{DIGEST}\n"))

    assert len(findings) == 1
    assert findings[0].check == "container.runs_as_root"


def test_a_user_in_a_builder_stage_does_not_carry_into_the_final_one() -> None:
    dockerfile = f"FROM python@{DIGEST} AS build\nUSER 10001\nFROM build\nRUN true\n"

    assert _both(dockerfile) == {"container.runs_as_root"}


def test_an_explicit_root_user_fires() -> None:
    findings = container.runs_as_root(snapshot(dockerfile=f"FROM python@{DIGEST}\nUSER root\n"))

    assert "USER root" in findings[0].detail
