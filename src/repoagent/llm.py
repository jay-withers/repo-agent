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
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, Field, ValidationError

from .fetch import FetchError, get_json, post_json
from .models import Finding, RepoSnapshot, Suggestion, TriageOutcome, TriageUsage
from .settings import optional_secret, settings

logger = logging.getLogger(__name__)

# Per-file ceilings for the prompt, tighter than the 8 KB `client.MAX_FILE_BYTES`
# that governs what is stored. A README is the largest file here and the least
# useful for judging dependency health, so it gets the smallest share.
PROMPT_FILE_BUDGET = {"renovate": 2_000, "readme": 1_200, "dockerfile": 1_200}

# Enough for a paragraph, an ordering and a few suggestions — with headroom for
# the reasoning `deepseek-flash` does before any of it.
#
# **Reasoning tokens count against this budget and are billed as output**, and
# they are not visible in the reply. 1,500 was comfortable until suggestions were
# added, at which point the model reasoned harder, hit the ceiling mid-JSON, and
# every run degraded to "triage failed" with a validation error that said nothing
# about the real cause. `_ask` now names truncation explicitly, but the headroom
# is what stops it happening.
MAX_OUTPUT_TOKENS = 4_000

# Deterministic-ish, because a digest whose priorities reshuffle weekly for no
# reason teaches you to distrust it.
TEMPERATURE = 0.2

# USD per million tokens for `deepseek-flash`, off-peak and peak.
#
# **A hand-maintained copy of someone else's price list**, so the cost it
# produces is reported as an estimate and never as a fact. The balance below it
# is the number to trust. Checked 2026-09-19 against
# https://api-docs.deepseek.com/quick_start/pricing.
#
# Cache hits are fifty times cheaper than misses, which is why the two are
# tracked separately rather than as one `prompt_tokens`.
PRICES_USD_PER_MTOK = {
    "cache_hit": {False: 0.003, True: 0.006},
    "cache_miss": {False: 0.15, True: 0.3},
    "output": {False: 0.6, True: 1.2},
}

# DeepSeek charges peak rates 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday.
# The scan's own 07:00 Monday cron sits inside the second window, so the digest
# is billed at double — worth knowing before moving the schedule to save nothing.
PEAK_WINDOWS_UTC = ((1, 4), (6, 10))

# Enough for a few hundred more weekly runs. Below it the next digest is at real
# risk of a 402, which would cost the commentary silently.
LOW_BALANCE_USD = 1.0

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

You may also suggest improvements the scanner does not check for. These are \
opinions, are rendered separately from the findings, and are not counted \
anywhere. Rules for them:

6. Never restate a finding as a suggestion. The findings are already reported.
7. Never suggest something the scanner already checks: Renovate configuration \
and PR backlogs, README, LICENCE, description, CI presence, repository \
staleness, catalogue membership, Terraform lock platforms, action pinning, \
runner versions, shared Renovate preset, required checks nothing reports.
8. Be specific to what you were shown. "Add tests" is worthless; "the Dockerfile \
installs build tools into the runtime stage" is worth reading.
9. At most four. Fewer is better. An empty list is a fine answer.

Respond with JSON only, matching this shape:

{"summary": "<at most three sentences>", "order": ["<finding id>", ...], \
"themes": ["<short phrase>", ...], \
"suggestions": [{"repo": "<owner/name or empty for the estate>", \
"text": "<one sentence>"}, ...]}

`order` lists every id you were given, most important first. `themes` is at \
most three short phrases naming patterns across repositories, or an empty list.
"""


# A hard ceiling, enforced here rather than trusted to the prompt. Four opinions
# is a paragraph someone might read; twenty is a wall that buries the findings
# above it, which is the one thing this section must never do.
MAX_SUGGESTIONS = 4


class SuggestionModel(BaseModel):
    """One suggestion as the model returns it."""

    text: str = Field(default="", max_length=400)
    repo: str = Field(default="", max_length=200)


class Triage(BaseModel):
    """The model's reply, validated on the way in.

    Pydantic rather than a frozen dataclass — unlike everything in `models.py`,
    this genuinely is parsed from untrusted input, which is the case the module
    docstring there reserves it for.
    """

    summary: str = Field(default="", max_length=2_000)
    order: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list, max_length=3)
    suggestions: list[SuggestionModel] = Field(default_factory=list)


def triage(
    findings: list[Finding],
    repos: tuple[RepoSnapshot, ...],
    client: httpx.Client | None = None,
) -> TriageOutcome:
    """Reorder `findings` and produce a summary, or pass them through unchanged.

    Never raises: a triage failure costs the digest its commentary, its ordering
    and its suggestions, and must not cost the digest. The findings are the
    product; everything else here is decoration on top of them.
    """
    if not findings:
        return TriageOutcome(findings=tuple(findings))

    # Absent key means the step is off, exactly as an absent DIGEST-EMAIL-TO
    # means the mail is not sent. This is what keeps `repoagent render` working
    # on a laptop with no DeepSeek account.
    api_key = optional_secret("DEEPSEEK-API-KEY")
    if not api_key:
        logger.info("no DEEPSEEK-API-KEY, skipping triage")
        return TriageOutcome(findings=tuple(findings))

    try:
        reply, usage = _ask(api_key, _user_prompt(findings, repos), client=client)
    except (FetchError, ValidationError, ValueError, KeyError) as exc:
        logger.warning("triage failed, reporting findings unordered: %s", exc)
        return TriageOutcome(findings=tuple(findings))

    # After the call, not before: the balance that matters is the one left for
    # next week, and asking first would report a figure already out of date.
    usage = _with_balance(usage, api_key, client=client)
    return TriageOutcome(
        findings=_apply(findings, reply),
        summary=reply.summary.strip(),
        themes=tuple(reply.themes),
        usage=usage,
        suggestions=_suggestions(reply, repos),
    )


def _suggestions(reply: Triage, repos: tuple[RepoSnapshot, ...]) -> tuple[Suggestion, ...]:
    """The model's opinions, capped and with invented repositories dropped.

    The same defence as finding ids: a repository name the model made up is
    discarded rather than printed. A suggestion about the estate as a whole
    carries no repository and is kept.
    """
    known = {repo.full_name for repo in repos}
    kept: list[Suggestion] = []

    for item in reply.suggestions:
        text = item.text.strip()
        if not text:
            continue
        repo = item.repo.strip()
        if repo and repo not in known:
            logger.warning("dropping suggestion about unknown repository %r", repo)
            continue
        kept.append(Suggestion(text=text, repo=repo))
        if len(kept) == MAX_SUGGESTIONS:
            break

    return tuple(kept)


def _ask(api_key: str, prompt: str, client: httpx.Client | None) -> tuple[Triage, TriageUsage]:
    """One chat completion, validated into a Triage, with what it cost."""
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
    choice = payload["choices"][0]
    # Checked before parsing, because a truncated reply is invalid JSON and the
    # resulting validation error describes a missing brace rather than the cap
    # that caused it.
    if choice.get("finish_reason") == "length":
        raise ValueError(
            f"reply hit the {MAX_OUTPUT_TOKENS}-token cap and was truncated; "
            "raise MAX_OUTPUT_TOKENS or ask for less"
        )
    content = choice["message"]["content"]
    # The model the API *actually* served, not the one asked for: `deepseek-chat`
    # is an undocumented alias that resolves to something else, and a cost
    # attributed to the wrong model is worse than none.
    usage = _usage(payload.get("usage") or {}, payload.get("model") or cfg.deepseek_model)
    logger.info(
        "triage used %d prompt (%d cached) / %d completion tokens, est. $%.4f%s",
        usage.prompt_tokens,
        usage.cache_hit_tokens,
        usage.completion_tokens,
        usage.cost_usd or 0.0,
        " at peak rates" if usage.peak else "",
    )
    return Triage.model_validate_json(content), usage


def _usage(raw: dict, model: str, now: datetime | None = None) -> TriageUsage:
    """Token counts from the API, and an estimated cost for them.

    `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` are DeepSeek
    extensions to the OpenAI usage shape. Where they are absent — another
    compatible endpoint, or a future response — everything falls back to being
    counted as a miss, which over-estimates rather than under-estimates.
    """
    prompt = int(raw.get("prompt_tokens", 0))
    hit = int(raw.get("prompt_cache_hit_tokens", 0))
    miss = int(raw.get("prompt_cache_miss_tokens", prompt - hit))
    completion = int(raw.get("completion_tokens", 0))
    peak = _is_peak(now or datetime.now(UTC))

    cost = (
        hit * PRICES_USD_PER_MTOK["cache_hit"][peak]
        + miss * PRICES_USD_PER_MTOK["cache_miss"][peak]
        + completion * PRICES_USD_PER_MTOK["output"][peak]
    ) / 1_000_000

    return TriageUsage(
        model=model,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        completion_tokens=completion,
        peak=peak,
        cost_usd=cost,
    )


def _is_peak(moment: datetime) -> bool:
    """Whether DeepSeek's peak multiplier applies at `moment`.

    Weekends are off-peak in full. **Chinese public holidays are also off-peak
    and are not modelled**, so a run on one is costed at double what it actually
    was — an over-estimate, which is the right direction for a number labelled
    an estimate.
    """
    if moment.weekday() >= 5:
        return False
    return any(start <= moment.hour < end for start, end in PEAK_WINDOWS_UTC)


def _with_balance(usage: TriageUsage, api_key: str, client: httpx.Client | None) -> TriageUsage:
    """Attach the account's remaining balance, or leave it absent.

    One extra GET a week, justified because a job that quietly stops triaging on
    a 402 is precisely the silent failure this whole agent exists to catch. Never
    raises: not knowing the balance must not cost the digest its findings.
    """
    from dataclasses import replace

    try:
        payload = get_json(
            f"{settings().deepseek_api_url}/user/balance",
            headers={"Authorization": f"Bearer {api_key}"},
            client=client,
        )
    except (FetchError, ValueError, KeyError) as exc:
        logger.warning("could not read DeepSeek balance: %s", exc)
        return usage

    # Several currencies can be returned; USD is the one the prices above are in.
    infos = payload.get("balance_infos") or []
    usd = next((i for i in infos if i.get("currency") == "USD"), None)
    if usd is None:
        return usage

    balance = str(usd.get("total_balance", ""))
    try:
        if float(balance) < LOW_BALANCE_USD:
            logger.warning("DeepSeek balance is $%s — triage will stop when it runs out", balance)
    except ValueError:
        pass

    return replace(usage, balance_usd=balance)


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
                # Names only. The contents are fetched for the checks and are
                # deliberately not sent: a dozen workflow files per repository
                # would dominate the prompt and tell the model nothing it can act on.
                "workflows": list(repo.workflow_names),
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
