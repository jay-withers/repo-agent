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
from typing import Any

import httpx

from ..fetch import get_json
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
