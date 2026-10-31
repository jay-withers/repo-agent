"""Reading GitHub as the installed App.

Returns raw dictionaries and leaves interpretation to the caller, so that the
parsing and the checks can be tested against saved payloads with no network.

**Do not add search-API calls here.** GitHub's search endpoints have a far
tighter secondary rate limit than the core REST API, and they return 403 rather
than 429 when you cross it. Four search calls per repository across eleven
repositories tripped it within seconds during design. Everything this agent
needs is available from the core API and from GraphQL, which is why neither
appears below.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from ..fetch import get_json, graphql
from ..settings import settings
from . import auth

logger = logging.getLogger(__name__)

# GitHub's maximum, and the reason listing every repository takes one request
# rather than several.
PAGE_SIZE = 100


def installation_repos(client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Every repository this App is installed on.

    One request for up to 100 repositories, paginating only if there are more.
    """
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = get_json(
            f"{settings().github_api_url}/installation/repositories",
            headers=auth.auth_headers(client=client),
            params={"per_page": PAGE_SIZE, "page": page},
            client=client,
        )
        batch = payload.get("repositories", [])
        repos.extend(batch)

        # `total_count` is the honest stopping condition: a final page exactly
        # PAGE_SIZE long would otherwise cost one extra empty request, and a
        # short page is not a reliable end marker on every endpoint.
        if len(repos) >= payload.get("total_count", len(repos)) or not batch:
            break
        page += 1

    logger.info("installation covers %d repositories", len(repos))
    return repos


# Enough of a file to judge it by, and a hard ceiling on what one repository can
# contribute to the triage prompt. A Renovate config is rarely over a few hundred
# bytes; a README can be a book, and the model is charged for all of it.
MAX_FILE_BYTES = 8_000

# Repositories per GraphQL request. GraphQL costs are scored on the *potential*
# node count of the whole document, not what comes back, so one query aliasing
# every repository at once fails on a large installation with a node-limit error
# rather than degrading. Ten keeps the score well under the limit and still means
# one or two requests in practice.
DETAIL_BATCH_SIZE = 10

# Renovate reads the first of these it finds, so the order is Renovate's, not
# ours. `renovate.json5` and the `.github/` variants are all in normal use, and
# reporting "no Renovate config" at a repository that has one under a path we did
# not look at is the most annoying false positive this agent could produce.
RENOVATE_CONFIG_PATHS = (
    "renovate.json",
    "renovate.json5",
    ".github/renovate.json",
    ".github/renovate.json5",
    ".renovaterc",
    ".renovaterc.json",
)

# One alias per repository, each a full detail fragment.
#
# Deliberately absent: `branchProtectionRules` and `vulnerabilityAlerts`. Both
# need permissions beyond the read-only metadata/contents/pull-requests set this
# App holds, and GraphQL reports a permission failure as an `errors` entry that
# `fetch.graphql` turns into a FetchError — so asking for either would fail the
# whole batch, not just that field.
_REPO_FRAGMENT = """
  r%(index)d: repository(owner: %(owner)s, name: %(name)s) {
    nameWithOwner
    defaultBranchRef { name }
    %(files)s
    workflows: object(expression: "HEAD:.github/workflows") {
      ... on Tree {
        entries {
          name
          # The text, not just the name: whether an action is pinned to a commit
          # and which runner a job asks for are both answered by the contents and
          # by nothing else. Workflow files are small; a README is the thing worth
          # being careful about.
          object { ... on Blob { text } }
        }
      }
    }
    pullRequests(states: OPEN, first: 30, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      nodes {
        number
        title
        url
        isDraft
        createdAt
        updatedAt
        author { login }
        # For `renovate.blocked_prs`: BLOCKED is a required check failing (or
        # still running), DIRTY is a conflict. Needs only Pull requests: read,
        # unlike statusCheckRollup, which wants the Checks permission too.
        mergeStateStatus
      }
    }
    # Whether Renovate has ever opened a pull request here at all, which is a
    # different question from whether one is open now — a repository that has
    # never received a single update looks identical to a fully merged one if
    # you only count what is open.
    #
    # Ordered ASC rather than DESC: Renovate opens its first PR within days of
    # onboarding, so the *earliest* pull requests are where the evidence is,
    # and a repository with a long human history would push it off a DESC page.
    # `totalCount` is what makes the answer honest — where it exceeds the page,
    # absence proves nothing and `parse` returns None rather than False.
    # Author is the only field needed; nothing here is ever rendered.
    closedPullRequests: pullRequests(
      states: [MERGED, CLOSED], first: 30, orderBy: {field: CREATED_AT, direction: ASC}
    ) {
      totalCount
      nodes { author { login } }
    }
    # One page of branches for `hygiene.stale_branches`. Fifty rather than a
    # hundred because GraphQL scores the *potential* node count — 50 refs, each
    # with an open-PR count, across a ten-repository batch is comfortably
    # inside the limit; `totalCount` lets the check say "at least" when it is
    # not the whole list.
    branches: refs(refPrefix: "refs/heads/", first: 50) {
      totalCount
      nodes {
        name
        target { ... on Commit { committedDate } }
        associatedPullRequests(states: OPEN) { totalCount }
      }
    }
    releases(first: 1, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { tagName publishedAt }
    }
  }
"""


def _file_selection() -> str:
    """The `object(expression:)` aliases that pull each file's text.

    One alias per candidate path, because GraphQL has no "first of these that
    exists" and a missing path simply returns null — which costs nothing and
    saves a request per path per repository.
    """
    parts = []
    for i, path in enumerate(RENOVATE_CONFIG_PATHS):
        parts.append(f'renovate{i}: object(expression: "HEAD:{path}") {{ ... on Blob {{ text }} }}')
    parts.append('readme: object(expression: "HEAD:README.md") { ... on Blob { text } }')
    # The lock file's *platform hashes* are the point: one missing linux_amd64
    # fails pre-commit in CI on every pull request, which is a trap this estate
    # has already fallen into. See docs/design.md (GitHub API).
    parts.append(
        'tflock: object(expression: "HEAD:terraform/.terraform.lock.hcl") { ... on Blob { text } }'
    )
    parts.append(
        'tflock_root: object(expression: "HEAD:.terraform.lock.hcl") { ... on Blob { text } }'
    )
    parts.append('dockerfile: object(expression: "HEAD:Dockerfile") { ... on Blob { text } }')
    return "\n    ".join(parts)


def repo_details(
    full_names: list[str],
    client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch the detail behind each repository, keyed by `owner/name`.

    Batched with one alias per repository rather than one request each: the
    round trips are what cost time here, and the secondary rate limit is about
    burst and concurrency rather than total volume.

    A repository whose alias comes back null — renamed, or newly unreadable —
    is omitted rather than raising. Losing detail for one repository degrades
    its findings; losing the run reports nothing about any of them.
    """
    details: dict[str, dict[str, Any]] = {}
    files = _file_selection()

    for start in range(0, len(full_names), DETAIL_BATCH_SIZE):
        batch = full_names[start : start + DETAIL_BATCH_SIZE]
        variables: dict[str, Any] = {}
        fragments = []
        for index, full_name in enumerate(batch):
            owner, _, name = full_name.partition("/")
            variables[f"o{index}"] = owner
            variables[f"n{index}"] = name
            fragments.append(
                _REPO_FRAGMENT
                % {"index": index, "owner": f"$o{index}", "name": f"$n{index}", "files": files}
            )
        # Names go in as variables rather than interpolated into the document:
        # a repository called `") { ... }` is a valid GitHub name and would
        # otherwise rewrite the query.
        signature = ", ".join(f"$o{i}: String!, $n{i}: String!" for i in range(len(batch)))
        query = f"query RepoDetails({signature}) {{\n{''.join(fragments)}\n}}"

        data = graphql(
            settings().github_graphql_url,
            query=query,
            variables=variables,
            headers=auth.auth_headers(client=client),
            client=client,
        )
        for index, full_name in enumerate(batch):
            node = (data or {}).get(f"r{index}")
            if node:
                details[full_name] = node
            else:
                logger.warning("no detail returned for %s", full_name)

    logger.info("fetched detail for %d repositories", len(details))
    return details


# The App permission GitHub calls "Code scanning alerts" in its settings page and
# `security_events` everywhere else.
CODE_SCANNING_PERMISSION = "security_events"


def code_scanning_alerts(
    full_names: list[str],
    client: httpx.Client | None = None,
) -> dict[str, list[dict[str, Any]] | None]:
    """Open code scanning alerts for each repository, keyed by `owner/name`.

    Every tool, not just CodeQL: the shared docker.yml uploads Trivy's image
    findings to the same place, and an alert from either is a fixable defect
    nobody has fixed.

    REST, one request per repository, because GraphQL does not expose code
    scanning at all. Sequential like everything else here.

    Returns an empty mapping when the App has not been granted the permission,
    and None for any one repository whose alerts could not be read — a 404 is
    "code scanning has never run here", which is not the same fact as "no
    alerts". Either way the check stays silent rather than reporting a clean
    repository it never saw.

    One page of 100, unpaginated: past that the finding says "at least", and a
    repository with a hundred open alerts has a problem the exact number does
    not change.
    """
    if not auth.permitted(CODE_SCANNING_PERMISSION, client=client):
        logger.info(
            "App lacks the %s permission; not reading code scanning alerts",
            CODE_SCANNING_PERMISSION,
        )
        return {}

    alerts: dict[str, list[dict[str, Any]] | None] = {}
    for full_name in full_names:
        try:
            page = get_json(
                f"{settings().github_api_url}/repos/{full_name}/code-scanning/alerts",
                headers=auth.auth_headers(client=client),
                params={"state": "open", "per_page": PAGE_SIZE},
                client=client,
            )
        except Exception as exc:
            # Logged at info, not warning: a 404 from a repository that has never
            # run a scanner is the normal case for much of an estate.
            logger.info("no code scanning alerts for %s: %s", full_name, exc)
            alerts[full_name] = None
            continue
        alerts[full_name] = page if isinstance(page, list) else None

    return alerts


# The App permission GitHub calls "Issues".
ISSUES_PERMISSION = "issues"


def open_issues(
    full_names: list[str],
    client: httpx.Client | None = None,
) -> dict[str, list[dict[str, Any]] | None]:
    """Open issues for each repository, keyed by `owner/name`.

    For `checks.delivery`: the shared workflows open `terraform-pending` and
    `deploy-failed` issues, and the digest is where they are listed weekly.
    REST, one page of 100 per repository, the same shape and the same
    permission gate as `code_scanning_alerts`: without the permission nothing
    is requested and the mapping is empty, so the checks stay silent. The
    endpoint also returns pull requests; `parse.issues` drops them.
    """
    if not auth.permitted(ISSUES_PERMISSION, client=client):
        logger.info("App lacks the %s permission; not reading issues", ISSUES_PERMISSION)
        return {}

    issues: dict[str, list[dict[str, Any]] | None] = {}
    for full_name in full_names:
        try:
            page = get_json(
                f"{settings().github_api_url}/repos/{full_name}/issues",
                headers=auth.auth_headers(client=client),
                params={"state": "open", "per_page": PAGE_SIZE},
                client=client,
            )
        except Exception as exc:
            logger.info("issues unavailable for %s: %s", full_name, exc)
            issues[full_name] = None
            continue
        issues[full_name] = page if isinstance(page, list) else None

    return issues


# Where the estate's catalogue of every repository lives. Read through the same
# installation token as everything else — the App is installed on this repository
# too, so it needs no extra permission.
#
# Configurable rather than hard-coded so this is not silently wrong for anyone
# whose catalogue lives elsewhere, and so an empty setting switches the check off
# rather than producing a finding against every repository at once.
def catalogue(client: httpx.Client | None = None) -> dict[str, CatalogueEntry] | None:
    """Every repository `github-repos` declares and what its ruleset enforces.

    **None and empty mean different things.** None is "the catalogue could not be
    read", which must never render as "every repository is unmanaged"; an empty
    mapping is a catalogue that genuinely declares nothing.
    """
    cfg = settings()
    if not cfg.catalogue_repo:
        return None

    owner, _, name = cfg.catalogue_repo.partition("/")
    query = """
    query Catalogue($owner: String!, $name: String!, $path: String!) {
      repository(owner: $owner, name: $name) {
        object(expression: $path) { ... on Blob { text } }
      }
    }
    """
    try:
        data = graphql(
            cfg.github_graphql_url,
            query=query,
            variables={"owner": owner, "name": name, "path": f"HEAD:{cfg.catalogue_path}"},
            headers=auth.auth_headers(client=client),
            client=client,
        )
    except Exception as exc:
        logger.warning("could not read the catalogue at %s: %s", cfg.catalogue_repo, exc)
        return None

    text = (((data or {}).get("repository") or {}).get("object") or {}).get("text")
    if not isinstance(text, str):
        logger.warning("no catalogue file at %s:%s", cfg.catalogue_repo, cfg.catalogue_path)
        return None

    entries = parse_catalogue_entries(text)
    logger.info("catalogue declares %d repositories", len(entries))
    return entries


def parse_catalogue(text: str) -> frozenset[str]:
    """The repository names declared in a `repos = { ... }` tfvars block."""
    return frozenset(parse_catalogue_entries(text))


@dataclass(frozen=True)
class CatalogueEntry:
    """What the catalogue's ruleset enforces on one repository."""

    required_checks: frozenset[str] = frozenset()
    # `code_scanning = true`: the ruleset blocks merges on new CodeQL alerts,
    # which is how this estate enforces CodeQL, rather than a status check.
    code_scanning: bool = False


def parse_catalogue_entries(text: str) -> dict[str, CatalogueEntry]:
    """Each declared repository, mapped to what its ruleset enforces.

    The names are the reason this exists; the contexts and the code scanning
    flag ride along because they are in the same block and reading the file
    twice would be the only alternative. A repository declaring no checks maps
    to an empty set, which is different from one that is absent entirely.

    A brace-depth scan rather than a regex over the whole file, because the
    values contain nested blocks — `required_status_checks = [{ context = ... }]`
    — and a pattern loose enough to find the keys is loose enough to find those
    too. Depth is tracked so only the keys directly inside `repos` are taken.

    Not a real HCL parser: adding one for a file of quoted keys would be a
    dependency for one check. This reads what the catalogue actually looks like
    and ignores anything it does not recognise.
    """
    entries: dict[str, set[str]] = {}
    scanned: set[str] = set()
    current: str | None = None
    depth = 0
    in_repos = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        if not in_repos:
            if re.match(r"^repos\s*=\s*\{", line):
                in_repos = True
                depth = 1
            continue

        # A quoted key opening a block, directly inside `repos`.
        match = re.match(r'^"([^"]+)"\s*=\s*\{\s*$', line)
        if depth == 1 and match:
            current = match.group(1)
            entries[current] = set()
        elif current is not None:
            # `{ context = "pre-commit / Pre-commit" },` — one per line in the
            # catalogue, and nothing else in a repository block uses `context`.
            context = re.search(r'\bcontext\s*=\s*"([^"]+)"', line)
            if context:
                entries[current].add(context.group(1))
            elif re.match(r"^code_scanning\s*=\s*true\b", line):
                scanned.add(current)

        depth += line.count("{") - line.count("}")
        if depth <= 1:
            current = None
        if depth <= 0:
            break

    return {
        name: CatalogueEntry(required_checks=frozenset(contexts), code_scanning=name in scanned)
        for name, contexts in entries.items()
    }
