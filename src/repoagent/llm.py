"""Triage: asking DeepSeek to order and explain what the checks already found.

**The model never establishes a fact.** Every finding in the digest comes from a
pure function in `checks/`; this module hands those findings to a model and gets
back a priority order and a paragraph of context. That division is deliberate and
load-bearing — see `digest.py`, which records why market-agent stopped letting a
model near a number that gets reported.

Three defences make the division hold rather than merely stating it:

- The response is validated against a Pydantic schema, so a malformed reply is
  an exception rather than prose pasted into an email.
- Returned finding ids are intersected with the ids that were sent. A model that
  invents `abc123` has it dropped.
- Any finding the model omits is appended in its original order. The model can
  reorder the digest; it cannot shorten it.

DeepSeek rather than Anthropic, and over plain `fetch.post_json` rather than an
SDK: the API is OpenAI-compatible, so one POST with a JSON body is the whole
integration, and the existing timeout and error handling apply unchanged. That
is one fewer dependency than the Anthropic SDK this repository once planned for.

**What leaves the tenancy.** The prompt carries repository names, the findings,
and truncated text of each repository's Renovate config, README and Dockerfile.
It carries no other source. DeepSeek is China-hosted and its terms permit
training on inputs, so `PROMPT_FILE_BUDGET` is a privacy control as much as a
cost one, and the whole step is off unless `DEEPSEEK-API-KEY` is set.
"""

from __future__ import annotations

import json
import logging

import httpx
from pydantic import BaseModel, Field, ValidationError

from .fetch import FetchError, post_json
from .models import Finding, RepoSnapshot
from .settings import optional_secret, settings

logger = logging.getLogger(__name__)

# Per-file ceilings for the prompt, tighter than the 8 KB `client.MAX_FILE_BYTES`
# that governs what is stored. A README is the largest file here and the least
# useful for judging dependency health, so it gets the smallest share.
PROMPT_FILE_BUDGET = {"renovate": 2_000, "readme": 1_200, "dockerfile": 1_200}

# Enough for a paragraph of context and an ordering, not enough to write an
# essay nobody reads. Also a cost ceiling: DeepSeek bills output tokens at
# several times the input rate.
MAX_OUTPUT_TOKENS = 1_500

# Deterministic-ish, because a digest whose priorities reshuffle weekly for no
# reason teaches you to distrust it.
TEMPERATURE = 0.2

_SYSTEM_PROMPT = """\
You are triaging the output of an automated GitHub repository scanner.

The findings below were produced by deterministic code, not by you. They are \
facts. Your job is to decide what matters most and to explain why, in the \
context of the whole estate.

Rules, all of them absolute:

1. Do not invent findings. Use only the ids you are given.
2. Do not restate counts, totals or lists. The email already renders a full \
table of every finding above your text, and repeating it wastes the only part \
of the message a human reads.
3. Do not describe what the scanner did. Describe what the maintainer should do.
4. Your summary is at most three sentences. Lead with the single most urgent \
thing, name the repository, and say what action it needs.
5. If nothing is genuinely urgent, say so plainly in one sentence rather than \
manufacturing concern.

Respond with JSON only, matching this shape:

{"summary": "<at most three sentences>", "order": ["<finding id>", ...], \
"themes": ["<short phrase>", ...]}

`order` lists every id you were given, most important first. `themes` is at \
most three short phrases naming patterns across repositories, or an empty list.
"""


class Triage(BaseModel):
    """The model's reply, validated on the way in.

    Pydantic rather than a frozen dataclass — unlike everything in `models.py`,
    this genuinely is parsed from untrusted input, which is the case the module
    docstring there reserves it for.
    """

    summary: str = Field(default="", max_length=2_000)
    order: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list, max_length=3)


def triage(
    findings: list[Finding],
    repos: tuple[RepoSnapshot, ...],
    client: httpx.Client | None = None,
) -> tuple[tuple[Finding, ...], str, tuple[str, ...]]:
    """Reorder `findings` and produce a summary, or pass them through unchanged.

    Returns `(findings, summary, themes)`. Never raises: a triage failure costs
    the digest its commentary and its ordering, and must not cost the digest.
    The findings are the product; the prose is decoration on top of them.
    """
    if not findings:
        return tuple(findings), "", ()

    # Absent key means the step is off, exactly as an absent DIGEST-EMAIL-TO
    # means the mail is not sent. This is what keeps `repoagent render` working
    # on a laptop with no DeepSeek account.
    api_key = optional_secret("DEEPSEEK-API-KEY")
    if not api_key:
        logger.info("no DEEPSEEK-API-KEY, skipping triage")
        return tuple(findings), "", ()

    try:
        reply = _ask(api_key, _user_prompt(findings, repos), client=client)
    except (FetchError, ValidationError, ValueError, KeyError) as exc:
        logger.warning("triage failed, reporting findings unordered: %s", exc)
        return tuple(findings), "", ()

    return _apply(findings, reply), reply.summary.strip(), tuple(reply.themes)


def _ask(api_key: str, prompt: str, client: httpx.Client | None) -> Triage:
    """One chat completion, validated into a Triage."""
    cfg = settings()
    payload = post_json(
        f"{cfg.deepseek_api_url}/chat/completions",
        body={
            "model": cfg.deepseek_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            # DeepSeek honours OpenAI's JSON mode. Without it the reply arrives
            # wrapped in a ```json fence perhaps one time in five, which is a
            # parse error rather than a degradation.
            "response_format": {"type": "json_object"},
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        client=client,
    )
    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage") or {}
    logger.info(
        "triage used %s prompt / %s completion tokens",
        usage.get("prompt_tokens", "?"),
        usage.get("completion_tokens", "?"),
    )
    return Triage.model_validate_json(content)


def _apply(findings: list[Finding], reply: Triage) -> tuple[Finding, ...]:
    """Reorder `findings` by `reply.order`, keeping every one of them.

    Ids the model invented are ignored; findings it forgot are appended in their
    original order. The set that goes in is always the set that comes out.
    """
    by_id = {finding.id: finding for finding in findings}
    seen: set[str] = set()
    ordered: list[Finding] = []

    for finding_id in reply.order:
        finding = by_id.get(finding_id)
        if finding is not None and finding_id not in seen:
            ordered.append(finding)
            seen.add(finding_id)

    dropped = [f for f in findings if f.id not in seen]
    if dropped:
        logger.warning("triage omitted %d finding(s); appending them", len(dropped))
    return tuple(ordered + dropped)


def _user_prompt(findings: list[Finding], repos: tuple[RepoSnapshot, ...]) -> str:
    """The findings and just enough repository context to judge them by."""
    by_name = {repo.full_name: repo for repo in repos}
    # Only repositories that actually have a finding: context for a clean
    # repository is tokens spent to tell the model nothing is wrong there.
    named = sorted({finding.repo for finding in findings})

    context = []
    for name in named:
        repo = by_name.get(name)
        if repo is None:
            continue
        context.append(
            {
                "repo": name,
                "description": repo.description,
                "private": repo.private,
                "workflows": list(repo.workflows),
                "open_renovate_prs": len(repo.renovate_prs),
                "last_release": repo.last_release,
                "renovate_config": _clip(repo.renovate_config, "renovate"),
                "readme": _clip(repo.readme, "readme"),
                "dockerfile": _clip(repo.dockerfile, "dockerfile"),
            }
        )

    payload = {
        "findings": [
            {
                "id": f.id,
                "repo": f.repo,
                "check": f.check,
                "severity": f.severity,
                "title": f.title,
                "detail": f.detail,
                "age_days": f.age_days,
            }
            for f in findings
        ],
        "repositories": context,
    }
    return json.dumps(payload, indent=None, separators=(",", ":"))


def _clip(text: str | None, budget: str) -> str | None:
    """Trim a file to its prompt budget."""
    if not text:
        return None
    limit = PROMPT_FILE_BUDGET[budget]
    return text if len(text) <= limit else text[:limit] + "\n… [truncated]"
