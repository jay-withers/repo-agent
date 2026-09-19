"""Estate consistency and Actions hygiene, from literals as always."""

from __future__ import annotations

from repoagent.checks import estate, workflows
from repoagent.github.client import parse_catalogue, parse_catalogue_entries
from tests.factories import HEALTHY_TF_LOCK, snapshot, workflow
from tests.helpers import route_client

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


def test_the_catalogue_carries_each_repositorys_required_contexts() -> None:
    entries = parse_catalogue_entries(CATALOGUE)

    assert entries["azure-landingzone"] == frozenset(
        {"pre-commit / Pre-commit", "terraform / Terraform"}
    )


def test_a_repository_requiring_no_checks_is_empty_not_absent() -> None:
    """Declaring no required checks is a different fact from not being declared."""
    entries = parse_catalogue_entries(CATALOGUE)

    assert entries["repo-agent"] == frozenset()
    assert "not-a-repo" not in entries


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


# --- required checks nothing reports ----------------------------------------

# The shape azure-landingzone had: a local gate job reporting `ci-terraform`,
# while the catalogue required `terraform / Terraform`.
LOCAL_GATE_WORKFLOW = """\
name: ci-terraform
on: [pull_request]
jobs:
  changes:
    runs-on: ubuntu-24.04
    steps:
      - run: echo hello
  ci-terraform:
    needs: [changes]
    if: always()
    runs-on: ubuntu-24.04
    steps:
      - run: echo hello
"""

# The shape it has now: a job calling the reusable workflow, whose own gate job
# makes the context `terraform / Terraform`.
CALLER_WORKFLOW = """\
name: ci-terraform
on: [pull_request]
jobs:
  terraform:
    permissions:
      contents: read
    uses: jay-withers/workflows/terraform.yml@2d4b1e0f3a5c7d9e1f2a3b4c5d6e7f8091a2b3c4 # v1.4.1
  terraform-plan:
    needs: terraform
    runs-on: ubuntu-24.04
    steps:
      - run: echo hello
"""


def test_a_required_context_no_workflow_can_report_is_flagged() -> None:
    findings = estate.unreportable_required_check(
        snapshot(
            workflows=(workflow(name="ci-terraform.yml", text=LOCAL_GATE_WORKFLOW),),
            required_checks=("terraform / Terraform",),
        )
    )

    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert "terraform / Terraform" in findings[0].detail


def test_a_reusable_workflow_call_satisfies_its_namespaced_context() -> None:
    """Only the caller job id can be checked — the name after the slash is
    defined in whichever repository owns the reusable workflow."""
    assert (
        estate.unreportable_required_check(
            snapshot(
                workflows=(workflow(name="ci-terraform.yml", text=CALLER_WORKFLOW),),
                required_checks=("terraform / Terraform", "terraform-plan"),
            )
        )
        == []
    )


def test_a_jobs_name_is_its_context_where_it_has_one() -> None:
    named = """\
name: ci
on: [pull_request]
jobs:
  build_and_test:
    name: Test
    runs-on: ubuntu-24.04
    steps:
      - run: echo hello
"""
    assert (
        estate.unreportable_required_check(
            snapshot(workflows=(workflow(name="ci.yml", text=named),), required_checks=("Test",))
        )
        == []
    )


def test_a_catalogue_that_could_not_be_read_flags_nothing() -> None:
    """The same tri-state as `in_catalogue`, for the same reason."""
    assert estate.unreportable_required_check(snapshot(required_checks=None)) == []


def test_a_repository_with_no_workflows_at_all_flags_nothing() -> None:
    """A failed detail query leaves every snapshot workflow-less, and must not
    report the whole estate as misconfigured — `hygiene.no_ci` owns that fact."""
    assert (
        estate.unreportable_required_check(
            snapshot(workflows=(), required_checks=("terraform / Terraform",))
        )
        == []
    )


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


# --- ignored repositories ---------------------------------------------------


def test_a_repository_with_an_ignored_topic_is_dropped_before_anything_else() -> None:
    """Not fetched, not checked, not sent to the model."""
    import httpx

    from repoagent.jobs import scan

    repos = {
        "total_count": 2,
        "repositories": [
            {
                "name": "git-demo",
                "full_name": "jay-withers/git-demo",
                "description": None,
                "default_branch": "main",
                "archived": False,
                "pushed_at": "2026-09-01T00:00:00Z",
                "topics": ["git", "learning", "tutorial"],
                "license": None,
                "html_url": "https://github.com/jay-withers/git-demo",
            },
            {
                "name": "repo-agent",
                "full_name": "jay-withers/repo-agent",
                "description": "d",
                "default_branch": "main",
                "archived": False,
                "pushed_at": "2026-09-01T00:00:00Z",
                "topics": ["azure"],
                "license": {"key": "mit"},
                "html_url": "https://github.com/jay-withers/repo-agent",
            },
        ],
    }
    calls: list[httpx.Request] = []
    client = route_client(
        {
            "/app/installations/1/access_tokens": httpx.Response(
                201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
            ),
            "/app/installations": httpx.Response(200, json=[{"id": 1}]),
            "/installation/repositories": httpx.Response(200, json=repos),
            "/graphql": httpx.Response(200, json={"data": {}}),
        },
        calls=calls,
    )

    result = scan.run(send_email=False, http=client)

    assert result.ignored == (("jay-withers/git-demo", "tutorial"),)
    assert [r.full_name for r in result.repos] == ["jay-withers/repo-agent"]
    assert not any(f.repo == "jay-withers/git-demo" for f in result.findings)
    # The detail query never asked about it.
    graphql = [c for c in calls if "graphql" in str(c.url)]
    assert not any(b"git-demo" in c.content for c in graphql)


def test_ignored_repositories_are_named_in_the_digest() -> None:
    """An exemption nobody can see is one nobody revisits."""
    from repoagent import digest
    from repoagent.models import ScanResult

    result = ScanResult(repos=(), ignored=(("jay-withers/git-demo", "tutorial"),))

    assert "Ignored by topic: jay-withers/git-demo (tutorial)" in digest.render_text(result)


def test_topic_matching_ignores_case() -> None:
    from repoagent.settings import settings

    assert "tutorial" in settings().ignored_topics
    assert "no-scan" in settings().ignored_topics
