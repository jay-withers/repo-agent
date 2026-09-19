"""Estate consistency and Actions hygiene, from literals as always."""

from __future__ import annotations

from repoagent.checks import estate, workflows
from repoagent.github.client import parse_catalogue
from tests.factories import HEALTHY_TF_LOCK, snapshot, workflow

# The real catalogue's shape: quoted keys inside `repos = { ... }`, values
# containing nested blocks that a looser pattern would also match.
CATALOGUE = """
# A comment mentioning "not-a-repo" = { which should be ignored.
repos = {
  "azure-landingzone" = {
    description = "Landing zone"
    topics      = ["azure", "terraform"]
    required_status_checks = [
      { context = "pre-commit / Pre-commit" },
      { context = "terraform / Terraform" },
    ]
  }

  "repo-agent" = {
    description = "Scans repositories"
    topics      = ["azure"]
  }
}

other_variable = {
  "not-a-repo" = {
    description = "outside the repos block"
  }
}
"""


# --- catalogue parsing ------------------------------------------------------


def test_the_catalogue_yields_only_top_level_repository_keys() -> None:
    assert parse_catalogue(CATALOGUE) == frozenset({"azure-landingzone", "repo-agent"})


def test_nested_blocks_are_not_mistaken_for_repositories() -> None:
    """`required_status_checks = [{ context = ... }]` must not become a repo."""
    assert "context" not in parse_catalogue(CATALOGUE)


def test_a_catalogue_with_no_repos_block_yields_nothing() -> None:
    assert parse_catalogue("something_else = {}\n") == frozenset()


def test_an_empty_file_yields_nothing() -> None:
    assert parse_catalogue("") == frozenset()


# --- unmanaged --------------------------------------------------------------


def test_a_repository_outside_the_catalogue_is_flagged() -> None:
    findings = estate.unmanaged(snapshot(in_catalogue=False))

    assert len(findings) == 1
    assert findings[0].severity == "high"


def test_a_repository_in_the_catalogue_is_not_flagged() -> None:
    assert estate.unmanaged(snapshot(in_catalogue=True)) == []


def test_an_unreadable_catalogue_flags_nothing() -> None:
    """None must never render as "every repository is unmanaged"."""
    assert estate.unmanaged(snapshot(in_catalogue=None)) == []


# --- shared preset ----------------------------------------------------------


def test_a_config_not_extending_the_shared_preset_is_flagged() -> None:
    findings = estate.not_shared_preset(
        snapshot(renovate_config='{"extends": ["config:recommended"], "schedule": []}')
    )

    assert len(findings) == 1
    assert findings[0].severity == "low"


def test_extending_the_shared_preset_passes() -> None:
    assert estate.not_shared_preset(snapshot()) == []


def test_no_renovate_config_is_a_different_findings_problem() -> None:
    """`renovate.missing_config` owns that; this check must not double-report."""
    assert estate.not_shared_preset(snapshot(renovate_config=None)) == []


# --- terraform lock ---------------------------------------------------------


def test_a_lock_built_for_one_platform_is_flagged() -> None:
    """One `h1:` means one platform — the arm64-laptop lock that fails CI."""
    lock = (
        'provider "registry.terraform.io/hashicorp/azurerm" {\n'
        '  hashes = [\n    "h1:aaa=",\n    "zh:bbb",\n  ]\n}'
    )
    findings = estate.incomplete_terraform_lock(snapshot(terraform_lock=lock))

    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert "azurerm (1 of 3)" in findings[0].detail


def test_platform_names_are_never_looked_for_in_a_lock_file() -> None:
    """A lock file names no platform. Searching for one flagged the whole estate.

    This is a regression test for the first version of this check, which looked
    for the literal string `linux_amd64` — present in no lock file ever written.
    """
    assert "linux_amd64" not in HEALTHY_TF_LOCK
    assert estate.incomplete_terraform_lock(snapshot(terraform_lock=HEALTHY_TF_LOCK)) == []


def test_only_the_short_provider_is_named() -> None:
    """A lock can be complete for one provider and not another."""
    lock = (
        'provider "registry.terraform.io/hashicorp/azurerm" {\n'
        '  hashes = ["h1:a=", "h1:b=", "h1:c="]\n}\n'
        'provider "registry.terraform.io/hashicorp/random" {\n  hashes = ["h1:d="]\n}\n'
    )
    findings = estate.incomplete_terraform_lock(snapshot(terraform_lock=lock))

    assert "random (1 of 3)" in findings[0].detail
    assert "azurerm" not in findings[0].detail


def test_a_complete_lock_passes() -> None:
    assert estate.incomplete_terraform_lock(snapshot(terraform_lock=HEALTHY_TF_LOCK)) == []


def test_a_repository_with_no_terraform_is_not_flagged() -> None:
    assert estate.incomplete_terraform_lock(snapshot(terraform_lock=None)) == []


# --- pinned actions ---------------------------------------------------------


def test_a_third_party_action_on_a_tag_is_high_severity() -> None:
    text = "jobs:\n  a:\n    steps:\n      - uses: astral-sh/setup-uv@v3\n"
    findings = workflows.unpinned_actions(snapshot(workflows=(workflow(text=text),)))

    assert len(findings) == 1
    assert findings[0].check == "workflows.unpinned_actions"
    assert findings[0].severity == "high"
    assert "astral-sh/setup-uv@v3" in findings[0].detail


def test_a_github_owned_action_on_a_tag_is_only_low() -> None:
    """Different trust model — the tag is moved by GitHub itself."""
    text = "      - uses: actions/checkout@v4\n"
    findings = workflows.unpinned_actions(snapshot(workflows=(workflow(text=text),)))

    assert len(findings) == 1
    assert findings[0].check == "workflows.unpinned_first_party_actions"
    assert findings[0].severity == "low"


def test_sha_pinned_actions_pass() -> None:
    """The estate's own convention: a commit with the tag as a comment."""
    assert workflows.unpinned_actions(snapshot()) == []


def test_a_reusable_workflow_path_reference_is_understood() -> None:
    text = "    uses: jay-withers/workflows/.github/workflows/ci-python.yml@v1\n"
    findings = workflows.unpinned_actions(snapshot(workflows=(workflow(text=text),)))

    assert len(findings) == 1
    assert "ci-python.yml@v1" in findings[0].detail


def test_local_and_docker_references_are_skipped() -> None:
    """Neither has a ref that could be repointed."""
    text = "      - uses: ./.github/actions/setup\n      - uses: docker://alpine:3.20\n"

    assert workflows.unpinned_actions(snapshot(workflows=(workflow(text=text),))) == []


def test_the_count_spans_every_workflow_file() -> None:
    a = workflow("a.yml", "      - uses: foo/one@v1\n")
    b = workflow("b.yml", "      - uses: bar/two@main\n")
    findings = workflows.unpinned_actions(snapshot(workflows=(a, b)))

    assert "2 third-party action(s)" in findings[0].title


# --- runners ----------------------------------------------------------------


def test_a_retired_runner_is_flagged() -> None:
    text = "jobs:\n  a:\n    runs-on: ubuntu-20.04\n"
    findings = workflows.retired_runners(snapshot(workflows=(workflow(text=text),)))

    assert len(findings) == 1
    assert "ubuntu-20.04" in findings[0].detail


def test_a_supported_runner_passes() -> None:
    assert workflows.retired_runners(snapshot()) == []


def test_ubuntu_latest_is_not_a_finding() -> None:
    """The check is about images being withdrawn, not about pinning."""
    text = "    runs-on: ubuntu-latest\n"

    assert workflows.retired_runners(snapshot(workflows=(workflow(text=text),))) == []
