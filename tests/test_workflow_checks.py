"""Workflow token scope and `pull_request_target`, read from workflow text."""

from __future__ import annotations

from repoagent.checks import workflows
from tests.factories import snapshot, workflow

UNSCOPED = """\
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""

# The shape of this repository's own cd-tag.yml: no top-level block, but every
# job scoped — including one that calls a reusable workflow.
EVERY_JOB_SCOPED = """\
on:
  push:
    branches: [main]

jobs:
  tag:
    permissions:
      contents: write
    uses: jay-withers/workflows/.github/workflows/release.yml@v1.4.11

  changes:
    needs: tag
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    steps:
      - run: true
"""


def test_a_top_level_permissions_block_scopes_the_token() -> None:
    assert workflows.unscoped_token(snapshot()) == []


def test_every_job_scoped_is_as_good_as_a_top_level_block() -> None:
    wf = workflow("cd-tag.yml", EVERY_JOB_SCOPED)

    assert workflows.unscoped_token(snapshot(workflows=(wf,))) == []


def test_one_unscoped_job_is_enough_to_fire() -> None:
    text = (
        EVERY_JOB_SCOPED + "\n  publish:\n    runs-on: ubuntu-latest\n    steps:\n      - run: x\n"
    )
    findings = workflows.unscoped_token(snapshot(workflows=(workflow("cd-tag.yml", text),)))

    assert len(findings) == 1
    assert findings[0].check == "workflows.unscoped_token"
    assert "cd-tag.yml" in findings[0].detail


def test_an_unscoped_workflow_is_named() -> None:
    wfs = (workflow(), workflow("lint.yml", UNSCOPED))
    findings = workflows.unscoped_token(snapshot(workflows=wfs))

    assert findings[0].severity == "medium"
    assert "lint.yml" in findings[0].detail
    assert "ci.yml" not in findings[0].detail


PR_TARGET = """\
on: pull_request_target
permissions:
  contents: read
jobs:
  label:
    runs-on: ubuntu-latest
    steps:
      - run: echo labelling
"""


def test_pull_request_target_on_its_own_is_legitimate() -> None:
    wf = workflow("label.yml", PR_TARGET)

    assert workflows.pull_request_target_checkout(snapshot(workflows=(wf,))) == []


def test_pull_request_target_checking_out_the_head_is_high_severity() -> None:
    text = PR_TARGET + (
        "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n"
    )
    findings = workflows.pull_request_target_checkout(
        snapshot(workflows=(workflow("label.yml", text),))
    )

    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert "label.yml" in findings[0].detail


def test_the_head_on_an_ordinary_pull_request_trigger_is_fine() -> None:
    text = UNSCOPED + "      - run: echo ${{ github.event.pull_request.head.sha }}\n"

    assert workflows.pull_request_target_checkout(snapshot(workflows=(workflow(text=text),))) == []


def test_a_reusable_workflow_takes_its_scope_from_the_caller() -> None:
    """`jay-withers/workflows` leaves called workflows unscoped by design."""
    text = UNSCOPED.replace("on: [pull_request]", "on:\n  workflow_call:\n    inputs: {}")

    assert workflows.unscoped_token(snapshot(workflows=(workflow("python.yml", text),))) == []


def test_a_reusable_workflow_that_also_runs_on_its_own_is_checked() -> None:
    text = UNSCOPED.replace("on: [pull_request]", "on:\n  workflow_call:\n  push:")

    assert len(workflows.unscoped_token(snapshot(workflows=(workflow("ci.yml", text),)))) == 1
