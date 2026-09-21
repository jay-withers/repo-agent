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

## History

One JSON document in a blob, read whole at the start of a run and written whole
at the end — `state.py`. A blob rather than a table because there is no query to
serve, and a weekly job with `parallelism = 1` has no concurrent writer. Not
Postgres: market-agent has one, this does not need one for a few hundred rows.

It carries `{finding_id: {first_seen, repo, check, title}}` plus suppressions.
`repo/check/title` are stored only so a **resolved** finding can be named — once
it is resolved no check produces it, so nothing else describes it.

- **`Finding.id` is load-bearing.** A hash of `repo:check`, derived rather than
  stored so two scans of an unchanged repository agree. Change how it is
  computed and every finding in the estate reads as new for one week.
- **`first_seen` is deliberately not `age_days`.** One is how long the fact has
  been true (from a GitHub timestamp), the other how long we have known. Merging
  them would claim a missing LICENCE appeared the day the agent first ran.
- **Reconcile runs before triage**, so a suppressed finding never reaches
  DeepSeek: no point paying to prioritise something that will not be printed.
- **State is saved last, after the email, and only if it was sent.** A run that
  failed to send must not record its findings as seen, or the retry reports
  nothing as new. `render` never saves at all — otherwise `make run` twice would
  empty Monday's "new" section.
- Absent state degrades to "everything is new" and never fails the run, the same
  contract as triage.

**Suppressions are decisions, so they are recorded with a reason** — `--reason`
is required on `repoagent suppress`, not optional. An optional expiry exists
because most "we'll live with it" is really "not this quarter". An unparseable
expiry *expires* rather than lasting for ever, since a typo that silently
suppresses a finding permanently is the failure nobody notices. Expired and
orphaned rules are dropped on each run.

The operator commands (`state`, `suppress`, `unsuppress`) **report failure**,
unlike the scan: a silent no-op would leave someone believing they had
suppressed something, and they would find out next Monday.

**`STATE_CONTAINER_URL` lives in `common_env`, which is under
`ignore_changes`** — so Terraform will never push it to a running job. `make
deploy` sets it alongside `IMAGE_TAG` for exactly that reason. An existing job
picks it up on the next deploy, not on apply.

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

**`settings.secret()` reads the process environment, then `.env`, then Key
Vault.** That is what makes `make run` work with no Azure at all, and it is why
`.env.example` exists. Never commit a filled-in `.env` — `.gitignore` excludes
`.env` and `.env.*` and lets the template through.

**The `.env` step is a deliberate divergence from market-agent's copy.**
`SettingsConfigDict(env_file=".env")` loads that file into the `Settings` class
only and never into `os.environ`, so every secret written to `.env` was silently
ignored and fell through to Key Vault — while every document in both repositories
claimed otherwise. `settings.py` therefore imports `python-dotenv` directly, and
declares it rather than leaning on pydantic-settings' transitive copy. Port the
fix back; per the copied-modules rule below, the same bug fixed twice is the
tripwire for extracting a library.

Setting `KEY_VAULT_URI` in a local `.env` is a supported way to work: the secrets
then resolve from the real vault through your own `az login`, and no private key
is written to disk.

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

## Exempting a repository

**A repository carrying any topic in `ignore_topics` is skipped entirely** — not
fetched, not checked, not sent to the model. Default: `no-scan,tutorial`.

A topic rather than a list of repository names or a state entry, because
`github-repos` already applies topics from its catalogue, so the exemption is
declared where the rest of a repository's configuration lives. `no-scan` is the
explicit marker for anything; `tutorial` is semantic — teaching material
genuinely should not be held to infrastructure standards.

`git-demo` is the case this exists for, and its catalogue entry already said so
in a comment: *"it will show up in repo-agent's digest as missing both —
expected, not a defect"*. It carries `tutorial`, so it now drops out with no
change to either repository.

**Exempt repositories are named in the digest, not merely counted.** An
exemption nobody can see is one nobody revisits, and a topic added by mistake
would otherwise silently drop a repository out of the digest for ever.

## Security scanning: considered, not built

Reading **Dependabot, secret scanning and code scanning alerts** was designed and
deliberately not built. Surveyed across the estate first: all 13 repositories
already run gitleaks in pre-commit, every Terraform repository runs Checkov, and
12 of 13 run pre-commit in CI *and* have `pre-commit / Pre-commit` as a required
check. A "is a scanner configured" check would have been silent from the day it
shipped, and the alert APIs need three new App permissions plus a re-approved
installation.

**The residual gap, recorded rather than argued:** gitleaks in pre-commit is a
*local* control. It runs only for people who have run `pre-commit install`, and
`--no-verify` skips it — so a secret pushed from a fresh clone, from CI, or by
someone who skipped the hook never meets it. GitHub secret scanning is the
backstop for exactly that case, and nothing readable from a config file says
whether it has ever fired. Revisit if a credential ever does leak, or if the
estate stops being uniform.

## Checking an estate that is already declared

`jay-withers/github-repos` holds a catalogue of every repository and applies
branch protection, required checks, description and topics from it. So a check
asking "does this repo have branch protection" would mostly re-report that
repo's own Terraform. **The useful inversion is coverage** — `estate.unmanaged`
asks what exists on GitHub that the catalogue does not declare, because nothing
is enforcing anything on those.

`in_catalogue` is a **tri-state**. `None` means the catalogue could not be read
and must never render as "every repository is unmanaged", which is what a plain
boolean produces the first time the file moves. `catalogue_repo` empty switches
the check off entirely for the same reason.

`parse_catalogue` is a brace-depth scan, not a regex over the whole file: the
values contain nested blocks (`required_status_checks = [{ context = ... }]`)
and any pattern loose enough to find the repository keys also finds those. It is
deliberately not a real HCL parser — that would be a dependency for one check.

**A `.terraform.lock.hcl` never names a platform.** The first version of
`estate.incomplete_terraform_lock` searched for the literal `linux_amd64` and
consequently reported every Terraform repository in the estate, including this
one, whose lock is correct. Platform coverage is the **count of `h1:` hashes per
provider block** — one per platform locked — and `zh:` entries are the
registry's zip hashes, present regardless, so counting those proves nothing.
`test_platform_names_are_never_looked_for_in_a_lock_file` pins this.

**`estate.unreportable_required_check` compares the two halves of that
catalogue.** It reads `required_status_checks` out of the same block the
repository names come from, and asks whether any workflow in the repository can
produce each context. The failure it exists for is the worst one branch
protection has, and a silent one: a required check that never reports leaves
every pull request *pending* rather than failing it, so nothing merges except by
bypassing the ruleset — azure-landingzone required `terraform / Terraform` while
its workflows reported `ci-terraform`, and its Renovate backlog reached six
before anyone noticed the merges had all been bypasses.

It is deliberately half a check, because `<caller job id> / <reusable job name>`
has one half in another repository. Only the caller job id is verified; a
renamed job inside `jay-withers/workflows` would still slip through. Verifying
the far half means fetching that repository's workflows too — worth doing only
if that failure ever actually happens. Job ids are read with a regex on
two-space indentation rather than with a YAML dependency, the same trade as
`parse_catalogue` not being an HCL parser.

Workflow **contents** are fetched, not just filenames, because whether an action
is pinned to a commit and which runner a job asks for are answered by the text
and nothing else. They are deliberately **not** sent to the triage prompt —
`llm._user_prompt` sends `workflow_names` — since a dozen workflow files per
repository would dominate it.

## Onboarded is not activated

`renovate.never_opened_a_pr` exists because the other four Renovate checks all
pass on a repository that has never received a single dependency update. A repo
created from the template carries a config (so `missing_config` passes), commits
it directly rather than onboarding (no PR for `onboarding_unmerged`), has
nothing open (`stalled_prs` sees nothing) and extends the shared preset
(`default_config_only` is satisfied). Everything is green and nothing has ever
been updated.

Four repositories were in that state at once — `gym-log`, `finances`,
`azure-container-apps` and `repo-agent` itself — each showing **onboarded**
rather than **activated** in Mend's portal, each with a full Dependency
Dashboard and seven updates parked under *Awaiting Schedule*. Renovate had run
and detected everything; it had simply never had a job land inside the preset's
`before 6am on monday` window. `updateNotScheduled: true` means it rebases
existing branches at any hour, so only *creation* is confined to those six
hours a week — which is why market-agent's PRs were created at 00:55 and 05:34
but rebased at 16:06 and 19:01.

Two operator notes worth keeping:

- On the Dependency Dashboard, **`Create all awaiting schedule PRs at once` is
  the checkbox that works**. `Check this box to trigger a request for Renovate
  to run again` does not: the run re-evaluates the schedule, finds it is not
  Monday morning, and parks everything again. The existence of two separate
  checkboxes is the proof.
- `renovate_pr_ever` is a **tri-state**, for the same reason `in_catalogue` is.
  The closed-PR history is one 30-node page, so a repository with a long human
  history fills it with human pull requests. `None` means the page could not
  answer, and must never render as "Renovate has never run" — that would report
  the estate's busiest repositories as its deadest.

**The check only ever fires once per repository.** The moment Renovate opens its
first PR it goes quiet for good, so it catches a repository that never started,
not one that dies later — `stalled_prs` is the check for that. Dead-since-birth
is the case worth the finding, because nothing else in the estate will ever
mention it.

## The scan runs an hour after Renovate's burst

The estate's shared preset (`github>jay-withers/renovate`) opens pull requests
`before 6am on monday` and throttles them with `prHourlyLimit: 4`. The scan's
own cron is `0 7 * * 1`. **The agent therefore observes every repository at its
weekly peak open-PR count**, one hour after a week of updates has landed in one
burst, and it always will.

`renovate.stalled_prs` reported market-agent every Monday because of this. Six
PRs were open at 07:00; all of them had been merged by `renovate[bot]` through
platform auto-merge by that evening, each within two minutes of Renovate
rebasing it. The spread that made it look manual is
`strict_required_status_checks_policy` on the ruleset: branches must be up to
date, so one merge invalidates every other open PR and the queue drains at one
per Renovate run.

So the backlog arm requires `BACKLOG_MIN_AGE_DAYS` as well as a count — a PR
that has outlived a full weekly cycle. Volume on its own is a measurement of
the clock, not of the repository. The count itself is measured against the
preset's `prConcurrentLimit: 20`, **not** Renovate's default of 10.

## The checks, and what the model is allowed to do

**Checks are pure `(RepoSnapshot) -> list[Finding]` functions in `checks/`, and
they establish every fact the digest reports.** They do no I/O, so they are
tested by constructing a snapshot directly — see `tests/factories.py`, which
defaults to a *healthy* repository so each test names only the thing it tests.
Keep them pure. The moment a check fetches something it needs a transport, and
the test suite stops being literals.

**`llm.py` contributes `summary`, `themes` and `suggestions`, and nothing
else.** It cannot
add, remove or alter a finding. Three things enforce that rather than merely
asking for it:

- The reply is validated against a Pydantic schema, so malformed output raises
  instead of being pasted into an email.
- Returned finding ids are intersected with the ids that were sent, so an
  invented id is dropped.
- Findings the model omits are appended in their original order. **The set that
  goes in is the set that comes out.**

### Suggestions are opinion, and kept apart at every level

The advisory section exists because coded checks only ever cover what someone
thought to write. It is separated from findings **structurally, not by wording**:
a different field on `ScanResult`, a different section in the digest, excluded
from every total and from the subject line, and **never written to the state
document** — so no suggestion can ever become something the scanner remembers
having checked. `test_suggestions_are_not_written_to_state` pins the last one.

Invented repository names are dropped exactly as invented finding ids are, and
`MAX_SUGGESTIONS` caps the list — a wall of opinion buries the findings above it,
which is the one thing this section must not do. The prompt lists the checks that
already exist so the model does not re-suggest them.

**Reasoning tokens count against `MAX_OUTPUT_TOKENS` and are billed as output.**
1,500 was comfortable until suggestions were added, at which point the model
reasoned harder, hit the cap mid-JSON, and every run degraded to "triage failed"
with a validation error about a missing brace. The cap is now 4,000 and `_ask`
checks `finish_reason == "length"` first, so a truncation says so instead of
looking like malformed JSON. Output roughly quadrupled in cost — still under half
a cent.

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

`deepseek-flash` rather than `deepseek-v4-pro` — ordering a list someone else
established is not a reasoning problem, and the pro model's thinking tokens bill
as output.

**Not `deepseek-chat`.** That was the first default here and it works, but it
appears in neither `GET /models` nor the pricing page: it is an undocumented
alias that resolves to `deepseek-flash`, which the response's own `model` field
reports. An alias nobody documents can be withdrawn without notice, and this job
would find out on a Monday. `llm._usage` records the model the API *served*, not
the one asked for, so a cost is never attributed to the wrong thing.

### Cost and balance in the footer

The digest footer carries `triage <model> · <in> (<cached>) / <out> · ~$<cost> ·
$<balance> left`. Two different kinds of number, deliberately marked differently:

- **The cost is an estimate**, prefixed `~`, computed from
  `llm.PRICES_USD_PER_MTOK` — a hand-maintained copy of someone else's price
  list, checked 2026-09-19. It will go stale silently.
- **The balance is not**, because it comes from `GET /user/balance`. That is the
  number to trust, and the reason the extra weekly request is worth making: a
  job that quietly stops triaging on a 402 is exactly the silent failure this
  agent exists to catch. Below `LOW_BALANCE_USD` it logs a warning.

Cache hits cost **fifty times less** than misses, so the two are tracked
separately rather than as one `prompt_tokens`. Where a response omits the
breakdown everything counts as a miss, which over-estimates.

**Peak rates double everything**, and DeepSeek's peak window is 01:00-04:00 and
06:00-10:00 UTC on weekdays — so the scan's own `0 7 * * 1` cron sits inside it.
Moving the schedule an hour later would halve a cost measured in tenths of a
cent, which is not a reason to move it, but it should not be a surprise either.
Chinese public holidays are also off-peak and are **not** modelled, so a run on
one is over-costed.

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

`make test`. Tests never touch the network or Azure. `tests/conftest.py`:

- **`chdir`s into an empty `tmp_path`**, so a developer's own `.env` cannot reach
  the suite. Both `Settings` and `secret()` resolve `.env` relative to the
  working directory, and deleting an environment variable does not stop either
  reading the file — which silently gave anyone with a `.env` a different test
  suite from CI.
- Sets every secret as an environment variable and **sets `KEY_VAULT_URI`
  empty rather than deleting it**, so a missing secret fails rather than falling
  through to a real call. An explicit empty value beats a `.env`; an absent one
  does not.
- Clears the `settings()`, `secret()`, `optional_secret()` and `dotenv()` caches
  around each test. HTTP is faked with
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
