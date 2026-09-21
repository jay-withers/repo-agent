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
        author { login }
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
    # has already fallen into. See CLAUDE.md.
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


# Where the estate's catalogue of every repository lives. Read through the same
# installation token as everything else — the App is installed on this repository
# too, so it needs no extra permission.
#
# Configurable rather than hard-coded so this is not silently wrong for anyone
# whose catalogue lives elsewhere, and so an empty setting switches the check off
# rather than producing a finding against every repository at once.
def catalogue(client: httpx.Client | None = None) -> dict[str, frozenset[str]] | None:
    """Every repository `github-repos` declares and the checks it requires.

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


def parse_catalogue_entries(text: str) -> dict[str, frozenset[str]]:
    """Each declared repository, mapped to the status checks it requires.

    The names are the reason this exists; the contexts ride along because they
    are in the same block and reading the file twice would be the only
    alternative. A repository declaring none maps to an empty set, which is
    different from one that is absent entirely.

    A brace-depth scan rather than a regex over the whole file, because the
    values contain nested blocks — `required_status_checks = [{ context = ... }]`
    — and a pattern loose enough to find the keys is loose enough to find those
    too. Depth is tracked so only the keys directly inside `repos` are taken.

    Not a real HCL parser: adding one for a file of quoted keys would be a
    dependency for one check. This reads what the catalogue actually looks like
    and ignores anything it does not recognise.
    """
    entries: dict[str, set[str]] = {}
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

        depth += line.count("{") - line.count("}")
        if depth <= 1:
            current = None
        if depth <= 0:
            break

    return {name: frozenset(contexts) for name, contexts in entries.items()}
