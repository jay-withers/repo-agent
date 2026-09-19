# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

One Python package (`repoagent`), one image, one Container Apps job. It reads
every GitHub repository the App is installed on, checks Renovate is actually
working, and emails a weekly digest on Monday mornings.

`README.md` covers *why* and how to run it. This file covers the traps.

**Status: working.** It authenticates, lists every repository, fetches detail
for each with one batched GraphQL query, runs deterministic checks, asks
DeepSeek to triage the results, and emails a digest.

**Persistence is the next thing.** Nothing is stored between runs, so the
digest cannot say what is new since last week and a finding with no GitHub
timestamp has no age. `Finding.id` exists for exactly this. The intended shape
is one JSON blob in an Azure Storage account — `{finding_id: first_seen}` —
read at the top of `scan.run()` and written at the end, authenticated with the
managed identity so it adds no new secret. Not Postgres: market-agent has one,
this does not need one to store a few hundred rows.

## The two-repo split

This repo owns its **resource group, Key Vault, managed identity and the job**.
The Container Apps environment, Log Analytics workspace and Application
Insights belong to
[`jay-withers/azure-container-apps`](https://github.com/jay-withers/azure-container-apps)
and are shared with every other project.

It resolves them **by name through a data source**, never
`terraform_remote_state` — no access to another repo's state, and the coupling
stays a convention. Two consequences:

- **A data source against a not-yet-applied platform fails at *plan* time.**
  That is why this repo plans `dev` alone rather than a dev/stg/prd matrix, and
  why there is only `backends/dev.hcl` and `environments/dev.tfvars`. Adding
  stg/prd means the platform must exist there first.
- **A job may live in a different resource group from its environment, but not
  a different region.** The resource group therefore takes its location from
  the environment rather than from a variable of its own, and jobs must set
  `location` explicitly where container apps inherit it.

This is the first tenant of that environment, so it is also the proof the split
works. If Azure ever objects to the cross-resource-group attachment, the
fallback is workloads in the platform resource group with this Key Vault
unchanged.

## The `command` decision — do not undo this

**The job sets no `command`. The Dockerfile's `ENTRYPOINT` owns the executable
name, and `args = ["scan"]` picks the subcommand.**

market-agent took a full outage from the opposite arrangement in September 2026:
`command = ["marketagent"]` sat under `lifecycle { ignore_changes }`, went stale
when the console script was renamed, and every workload crash-looped on
`exec: "investagent": executable file not found`. The Terraform had already been
corrected; the ignore meant no apply ever pushed it, and the az-cli deploy path
does not touch `command` at all. Two sources of truth for one string, only one
of them versioned with the code that defines it.

So:

- No `command` argument. The entrypoint ships inside the image, atomically with
  the code that defines the console script.
- `args` is **not** under `ignore_changes`. It changes rarely, and a subcommand
  that does not exist fails loudly with a usage error.
- `ignore_changes` covers `image` and `env` only.

`ignore_changes` takes the **whole `env` map** rather than `IMAGE_TAG`'s entry,
because indexing into a map-driven `dynamic` block by position would silently
shift if `common_env` gained or lost a key. The trade-off: changing any *other*
value in `common_env` needs a `make deploy` to land on a running revision —
`terraform plan` reports no diff even though the value it computes has moved.

## Secrets

Terraform owns the Key Vault and its RBAC but creates **no**
`azurerm_key_vault_secret`, and wires in **no native Key Vault secret
reference**. A revision carrying a reference hard-fails when the secret is
absent, whereas runtime resolution through `settings.secret()` keeps `apply`
independent of whether any secret exists yet. Do not "improve" this.

`make secrets` prints the `az keyvault secret set` commands. The values are set
out of band by whoever holds `Key Vault Secrets Officer`; the workload identity
gets read-only `Key Vault Secrets User` on its own vault, which is already
tightly scoped because the vault is per-project.

**`settings.secret()` reads the environment first and Key Vault second.** That
is what makes `make run` work with no Azure at all, and it is why `.env.example`
exists. Never commit a filled-in `.env` — `.gitignore` excludes `.env` and
`.env.*` and lets the template through.

Secrets in use: `GITHUB-APP-ID`, `GITHUB-APP-PRIVATE-KEY`, `DIGEST-EMAIL-TO`,
`RESEND-API-KEY`, `DEEPSEEK-API-KEY`.

`DEEPSEEK-API-KEY` is read with `optional_secret()`, not `secret()`, so its
absence switches triage off rather than failing the run — the same pattern as
`DIGEST-EMAIL-TO`. That is what keeps `make run` working with no DeepSeek
account, and it means adding the key needs no redeploy. **`DIGEST-EMAIL-TO` must be plain ASCII** — Resend rejects a
`to` containing anything else with a 422, and market-agent lost a day's summary
to a value pasted with curly quotes, which are invisible in
`az keyvault secret show` output. Read the codepoints (`| cat -A`) when a send
fails on the address.

## The checks, and what the model is allowed to do

**Checks are pure `(RepoSnapshot) -> list[Finding]` functions in `checks/`, and
they establish every fact the digest reports.** They do no I/O, so they are
tested by constructing a snapshot directly — see `tests/factories.py`, which
defaults to a *healthy* repository so each test names only the thing it tests.
Keep them pure. The moment a check fetches something it needs a transport, and
the test suite stops being literals.

**`llm.py` contributes exactly two fields: `summary` and `themes`.** It cannot
add, remove or alter a finding. Three things enforce that rather than merely
asking for it:

- The reply is validated against a Pydantic schema, so malformed output raises
  instead of being pasted into an email.
- Returned finding ids are intersected with the ids that were sent, so an
  invented id is dropped.
- Findings the model omits are appended in their original order. **The set that
  goes in is the set that comes out.**

This is the market-agent lesson in `digest.py` applied structurally: given a
count and no table, a model will accurately report from what it was given that
nothing happened on a day three trades executed. The defence is not a better
prompt. Triage failure is caught and logged — the digest goes out unordered
with no commentary, because the findings are the product and the prose is
decoration.

**DeepSeek, not Anthropic.** The API is OpenAI-compatible, so the integration
is one `fetch.post_json` and the SDK this repo once planned for was never
added. Two consequences worth holding on to: DeepSeek is China-hosted and its
terms permit training on inputs, so `llm.PROMPT_FILE_BUDGET` is a privacy
control as much as a cost one; and only repositories that actually have a
finding get context in the prompt.

`deepseek-chat` (V3) rather than `deepseek-reasoner` (R1) — ordering a list
someone else established is not a reasoning problem, and R1's thinking tokens
bill as output.

## GitHub API

**No search API, ever.** GitHub's search endpoints carry a far tighter
secondary rate limit than the core API and signal it with **403, not 429** —
four search calls per repository across eleven repositories tripped it within
seconds during design. `fetch.py` therefore retries 403 as well, which is a
deliberate divergence from market-agent's copy.

Everything needed comes from `GET /installation/repositories` plus a batched
GraphQL query. Requests are sequential on one client; secondary limits are
about burst and concurrency.

**The detail query batches ten repositories per request, not all of them.**
GraphQL costs are scored on the *potential* node count of the whole document,
so one query aliasing every repository fails outright on a large installation
rather than degrading. Repository names go in as **variables**, never
interpolated: `") { evil }` is a legal GitHub repository name.

**`branchProtectionRules` and `vulnerabilityAlerts` are deliberately absent**
from that query. Both need permissions beyond the App's read-only
metadata/contents/pull-requests set, and GraphQL reports a permission failure
as an `errors` entry — which `fetch.graphql` turns into a `FetchError` that
fails the whole batch, not just the field. Adding either means widening the App
first.

A failed detail query **degrades** the run rather than ending it: the REST list
alone still reports every repository and every hygiene finding.

**GitHub App auth is hand-rolled on `PyJWT[crypto]`.** Sign an RS256 JWT
(`iss` = app id, `iat` backdated 60s for clock skew, `exp` ≤ 600s or GitHub
401s) → `GET /app/installations` → `POST /app/installations/{id}/access_tokens`
→ a `ghs_…` token good for an hour. Cached in a module-level tuple rather than
`@lru_cache`, because expiry has to be checked. All App permissions are
read-only; it is installed on all repositories so a new repo is covered with no
config change.

## Copied modules

`fetch.py`, `mailer.py`, `telemetry.py`, `settings.py` and `cli.py` are copied
from market-agent, each with a header naming the source file and commit.
**Re-copy rather than diverge** — the same culture as
`scripts/check-tf-standards.sh`, which is shared verbatim across five repos.

This is consumer #2, so no library was extracted: a published package for a
solo maintainer buys a release workflow, version pins and a Renovate rule
against an imagined third consumer, while making a `fetch.py` change here able
to break market-agent's trading path. Half the copied code diverges
legitimately anyway (no Postgres here, GitHub-specific retry).

**Tripwire to revisit:** a third consumer appears, *or* the same bug gets fixed
twice in copied code.

## Testing

`make test`. Tests never touch the network or Azure: `tests/conftest.py` sets
every secret as an environment variable and **deletes `KEY_VAULT_URI`**, so a
missing one fails rather than falling through to a real call, and it clears the
`settings()` and `secret()` caches around each test. HTTP is faked with
`httpx.MockTransport` via `tests/helpers.py`. `tests/` is a package, so helpers
import as `tests.helpers` — a bare `from conftest import ...` does not resolve.

**The test RSA key is generated in-process with `cryptography`.** Never commit
a PEM, even a throwaway, to a public repo. Reset `auth._cached` between tests.

The checks are pure `(RepoSnapshot) -> list[Finding]` functions, tested by
constructing a snapshot directly with no HTTP anywhere near the test. Keep them
that way.

`tests/factories.py` holds the snapshot builders, **not `tests/helpers.py`** —
that one is copied verbatim from market-agent and is re-copied rather than
diverged, so anything specific to this project's models belongs in factories.

`conftest.py` **deletes `DEEPSEEK_API_KEY`** alongside the others, so triage is
off unless a test switches it on. A test that enabled it accidentally would
reach `api.deepseek.com` for real.

## Docker

The image runs as **uid 10001, numerically** — a named `USER` trips hadolint's
DL3066, and a runtime enforcing `runAsNonRoot` has to resolve the user before
the container starts without being able to read the image's `/etc/passwd`.

**`uv sync` installs the project editable by default**, producing a `.pth`
pointing at the build stage's path. The runtime stage copies only the
virtualenv, so the image fails with a bare `No module named 'repoagent'` from a
venv that looks complete. `--no-editable` on both syncs is the fix.

Dockerfiles are linted by the local `scripts/hadolint.sh`, **not** the upstream
`hadolint-docker` hook — that one calls `docker system info`, which fails
whenever `DOCKER_HOST` is unset, and pre-commit run from `git commit` inherits
a non-login shell where the dev container's profile snippet has not run.

**QEMU emulation swallows Python logging.** An amd64 image run under emulation
on arm64 exited 1 with **zero bytes** on both streams; the same image built
natively produced a full traceback. `print()` and argparse still worked. If a
container fails silently, rebuild for the host architecture before believing
the code is at fault.

Only amd64 is built and pushed, because Container Apps runs nothing else.

## CI

Every workflow is a thin caller of a reusable workflow in
[`jay-withers/workflows`](https://github.com/jay-withers/workflows), pinned by
commit SHA with the tag as a comment. A change to how a job *works* belongs
there so every consuming repo picks it up.

Status check contexts are namespaced `<caller job id> / <reusable job name>`.
**Read them off `gh pr checks` rather than inferring them.** The required
checks — set in `github-repos`, not here — are `pre-commit / Pre-commit`,
`test / Test`, `terraform / Terraform` and `terraform-plan`.
`ci-container-build`'s `build (repoagent)` is deliberately **not** required: it
is filtered on its trigger, so a docs-only PR never runs it, and a required
check that never reports leaves a PR pending for ever rather than failing it.

- **`extras: dev` on ci-python is the input that matters.** pytest is an
  *extra*, not a dependency group, and `uv run` installs groups but not extras
  — without it the job fails with a bare `Failed to spawn: pytest` after a
  successful-looking install.
- `python-version` matches the Dockerfile's base, and the reusable workflow runs
  `uv run --locked`, so a `pyproject.toml` edited without its lockfile fails in
  CI rather than on someone's machine.
- **Coverage is reported, never gated.** `pytest-cov` is injected by the
  workflow with `uv run --with`, an ephemeral overlay that needs no dependency
  here and does not invalidate `--locked`. The caller grants
  `pull-requests: write` for the comment — a called workflow can never hold
  more than its caller.
- `ci-terraform` is half shared, the same split market-agent and
  azure-container-apps use: `validate` comes from the shared workflow and
  reports `terraform / Terraform`, while `plan` stays here behind the
  always-reporting `terraform-plan` gate. `plan` is gated on
  `vars.AZURE_CLIENT_ID != ''`, which comes from the OIDC identity
  `github-repos` creates for this repo.
- **`cd-tag` mints a version on every merge, but `cd-publish` only rebuilds when
  `apps` content changed**, diffed against the previous release tag found with
  `git describe --tags --abbrev=0 HEAD^` rather than the push's own range — so
  an unreleased change carried by a push that minted no tag is still picked up
  later. A release version with no image published under it is expected, not a
  bug.
- **A green publish is not evidence the tags are right — check the registry.**
  Both images are pushed under the release `vX.Y.Z` and the commit's short SHA,
  and neither tag is ever moved; Container Apps creates a revision only when
  the template changes, so a re-pushed moving tag would deploy nothing and
  report success.

## `make deploy` is az cli, not terraform apply

The image and `IMAGE_TAG` are under `ignore_changes`, so a deploy moves
independently of a plan/apply cycle — no state lock, no plan of unrelated
drift, no stale local tfvars rolling the image backwards. `make deploy` resolves
the job name and resource group from `terraform output` and calls
`az containerapp job update --image ... --set-env-vars IMAGE_TAG=...`.
`--set-env-vars` updates only the name given and leaves every other env var
alone.

`image_tag`'s Terraform default must be a **tag that actually exists** in
ghcr.io. It seeds only the first revision on a brand-new environment, but a
default naming a tag nobody pushed fails at revision start-up on an image pull,
long after plan and apply both report success.

**Flip the GHCR package to public.** New packages default to private regardless
of repo visibility, and the job has no pull secret.

## Terraform conventions

`terraform/` is a deployable root configuration, not a reusable module.
`.terraform.lock.hcl` is committed and **must carry hashes for every platform
that runs Terraform**: `terraform providers lock -platform=linux_amd64
-platform=linux_arm64 -platform=darwin_arm64`. A lock file with arm64 hashes
only fails `pre-commit / Pre-commit` in CI on every PR, because CI runs amd64,
adds a hash during `init`, and the modified tracked file trips the hook.

**File layout is enforced** by `scripts/check-tf-standards.sh`, shared verbatim
with market-agent, azure-container-apps, terraform-root-aks and
azure-landingzone — a change here should be re-copied there. Variables are
split by whether they must be supplied: `variables.required.tf` and
`variables.optional.tf`.

**Comments and outputs earn their place.** Comment the non-obvious — a cost
trade-off, a provider quirk, a trap — not what the code already says.

The backend is **partial**: it prompts on a plain `terraform init` and fails
under `-input=false`, so anything not touching state uses `init -backend=false`.

## Commit messages

Conventional Commits, enforced by commitlint at commit-msg time. The
`no-commit-to-branch` hook blocks direct commits to `main`.

## Repo settings

Branch protection and repository settings live in
[jay-withers/github-repos](https://github.com/jay-withers/github-repos), whose
`terraform.tfvars` doubles as the catalogue of every repo. A change there takes
effect on `make apply`, not on merge — and applying a required check that
nothing reports leaves every PR *pending* rather than failing it.
