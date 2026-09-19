# repo-agent

Scans every GitHub repository this App is installed on, checks that Renovate is
actually doing its job, and emails a weekly digest.

Runs as a scheduled Container Apps job on the shared environment in
[jay-withers/azure-container-apps](https://github.com/jay-withers/azure-container-apps).
This repository owns its own resource group, Key Vault, identity and the job
itself; the environment it runs on is shared and resolved by name.

## Why it exists

Renovate failing is silent. A preset that stops resolving, an automerge that
quietly stopped, a dependency dashboard full of config errors nobody opens — all
of them look identical to "no updates this week" from the outside.

The finding that prompted this: `jay-withers/template-renovate` was renamed to
`jay-withers/renovate`, but several repos still extended the old name. It kept
working only through GitHub's rename redirect, which would evaporate the moment
anything was created at the old path — breaking preset resolution everywhere at
once, on a dependency PR nobody reads closely.

## Status

**Working.** Four stages, and the order of the middle two is the design:

1. **Fetch** — the REST installation list, then one batched GraphQL query for
   the Renovate config, README, Dockerfile, workflow names, open pull requests
   and latest release of each repository.
2. **Check** — nine pure functions turn each snapshot into findings. Everything
   the digest reports as fact is established here.
3. **Triage** — DeepSeek orders those findings and writes a short paragraph of
   context. It cannot add, remove or alter one.
4. **Report** — rendered and emailed.

Stage 3 is the only one that can be skipped. No `DEEPSEEK-API-KEY`, or a failed
call, and the digest goes out with its findings in severity order and no
commentary.

Every digest ends with what the commentary cost and what is left to pay for the
next one:

```
repo-agent v0.1.0
triage deepseek-flash · 2,702 in (0 cached) / 744 out · ~$0.0009 · $9.99 left
```

The cost carries a `~` because it is computed from a price table maintained by
hand and will go stale; the balance comes from DeepSeek's own endpoint and does
not. The balance is the one worth watching — a job that quietly stops triaging
on an empty account is exactly the kind of silent failure this agent exists to
catch elsewhere.

### What it checks

| Check | Severity | What it means |
|---|---|---|
| `renovate.missing_config` | high | No config at any path Renovate reads |
| `renovate.onboarding_unmerged` | high | Onboarding PR never merged, so Renovate opens nothing |
| `renovate.stalled_prs` | medium/high | Updates not being merged, or a backlog nearing `prConcurrentLimit` |
| `renovate.pinned_to_nothing` | low | Generated config nobody ever added a policy to |
| `hygiene.no_ci` | high/low | No workflows — high when Renovate is configured, since updates merge on faith |
| `hygiene.no_readme` | medium | No README at the root |
| `hygiene.no_license` | medium | Public repository with no detectable licence |
| `hygiene.no_description` | low | Unidentifiable in a list |
| `hygiene.stale` | low | No pushes in six months |

Archived repositories are skipped wholesale: every finding would be true,
unactionable and permanent, which is how you train someone to ignore an email.

## Design

**Deterministic first.** Every check is a pure function of a `RepoSnapshot` —
no I/O, no clock — so they are tested by constructing one directly with no HTTP
anywhere near the test. A model is only worth involving for triage across the
whole fleet ("which three of these thirty matter this week"), and even then
every *figure* in the email is computed. The model gets to write commentary and
an ordering, and is told the tables are already above its text.

Three things enforce that structurally rather than trusting the prompt: the
reply is validated against a schema, invented finding ids are dropped, and any
finding the model omits is appended in its original order. The set that goes in
is the set that comes out.

That rule is not paranoia. market-agent's summary job, given a reconciliation
count and no trades table, accurately reported from what it had been given that
nothing had happened on a day three trades executed. The defence is not a better
prompt — it is never letting the model near a number that gets reported.

**Weekly, with a memory.** One JSON document in a blob — read at the start of a
run, written at the end — lets the digest say what is **new** since last week
and what has been **resolved**. A blob, not a database: the whole access pattern
is read-whole then write-whole, once a week, with no concurrent writer.

Losing it costs one week's deltas, not correctness, which is why it is LRS with
no `prevent_destroy`. Blob versioning is on, so every prior week's document is
still retrievable.

`Finding.id` is a stable hash of `repo:check`, so two scans of an unchanged
repository agree about what they are looking at. Everything else depends on that.

**Findings you have decided to live with can be suppressed**, with a reason and
optionally an expiry:

```bash
make state                      # what it remembers, including suppressions
uv run repoagent suppress a1b2c3d4e5f6 \
  --reason "deliberate teaching repo" --until 2026-12-01
uv run repoagent unsuppress a1b2c3d4e5f6
```

The reason is required, not optional — in six months an unexplained suppression
is indistinguishable from a bug. Suppressed findings never reach the model, and
the digest reports how many are held back so they do not become invisible.

**LLM triage is optional and off by default.** `DEEPSEEK-API-KEY` is read with
`optional_secret()`, so its absence switches the step off instead of failing the
run. Worth knowing what setting it switches on: repository names, the findings,
and truncated Renovate config, README and Dockerfile text are sent to DeepSeek,
which is China-hosted and whose terms permit training on inputs. Only
repositories that actually have a finding are described in the prompt, and each
file is clipped hard before it goes — a cost control and a privacy one.

**No search API, ever.** GitHub's search endpoints have a far tighter secondary
rate limit than the core API, and they signal it with **403, not 429**. Four
search calls per repository across eleven repositories tripped it within seconds
during design. Everything needed is available from the core REST API and from
GraphQL. `fetch.py` retries 403 for the same reason.

## Local development

`settings.py` reads every secret from the environment first and only then from
Key Vault, which is what makes this work with no Azure at all:

```bash
make install
cp .env.example .env    # fill in GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY
make run                # scans real GitHub, prints the digest, sends nothing
```

`make run` is `repoagent render`. `repoagent scan` is the same path plus the
email, and it needs `RESEND_API_KEY` and `DIGEST_EMAIL_TO`.

```bash
make test    # the suite; no network, no Azure, no credentials
make lint    # every pre-commit hook
```

### Don't trust logs from a locally emulated image

`make build` cross-builds `linux/amd64` because that is all Container Apps
runs. On an arm64 host that image runs under QEMU, and **QEMU silently swallows
Python logging output**: `docker run <image> scan` exits 1 with zero bytes on
both stdout and stderr, while the identical code logs a full traceback when
built natively.

`print()` and argparse still work, which makes it worse — the container looks
alive and the failure looks silent. Verified both ways: 0 bytes emulated, 1582
bytes native, same commit.

So a locally emulated run tells you the exit code and nothing else. To read what
actually happened, build without `--platform` (`docker buildx build --load -t
repoagent:native .`) or just run `make run` outside Docker. CI runners and
Container Apps are both native amd64, so neither is affected.

## The GitHub App

A GitHub App rather than a personal access token: a PAT expires within a year,
belongs to one person and carries that person's whole account, whereas an
installation token is scoped to the repositories the App is installed on and
needs no diary entry to rotate.

Create it under your account with these **read-only** permissions — Metadata,
Contents, Issues, Pull requests, Checks, Actions, Administration — and install it
on **all repositories**, so a new repo is covered without a config change.

Then record the App ID and download the private key. The installation ID is
**not** configured: it is looked up from the App's own credentials, which is one
fewer secret to rotate or get wrong.

## Secrets

Terraform creates the Key Vault but **no secret values** — nothing secret
belongs in source or in state. `make secrets` prints the exact commands:

| Secret | Notes |
|---|---|
| `GITHUB-APP-ID` | From the App's settings page |
| `GITHUB-APP-PRIVATE-KEY` | The PEM. Use `--file`, not `--value` — its newlines matter, and a shell-quoted value mangles them into a key that fails to parse |
| `RESEND-API-KEY` | |
| `DIGEST-EMAIL-TO` | Absent means build the digest and send nothing, so email is opt-in. Must be plain ASCII |

That last one is not hypothetical: market-agent lost a day's summary to a
recipient stored with curly quotes pasted from something that autocorrects,
invisible in `az keyvault secret show` output. `mailer.py` now refuses it before
the request, with a message that names the problem.

None of these are needed for `terraform apply` — they are read at runtime, which
is deliberate. A Container Apps revision carrying a native Key Vault *reference*
hard-fails when the secret is absent; resolving at runtime means the apply and
the secrets are independent, and changing the recipient is one `az` call and the
next run, with no redeploy.

## Deploying

```bash
make build IMAGE_TAG=v0.0.1
make push  IMAGE_TAG=v0.0.1
make deploy IMAGE_TAG=v0.0.1
make start                     # a scheduled job has no other way to be triggered
```

`make deploy` with no tag is a hard error. `build` and `push` default to the
local git SHA because that is what you want when iterating; deploying that
default would silently roll the job onto whatever commit happens to be checked
out, which may never have been pushed.

The job's image and env are under `lifecycle { ignore_changes }`, so `make
deploy` (az cli) owns them after the first revision and `terraform plan` will
report no change even when the running image has moved on. The corollary: a
change to any *other* value in `common_env` also needs a `make deploy` to land.

### The one rule about `command`

**Terraform sets no `command` on the job — only `args`.** The image's
`ENTRYPOINT` is the single source of truth for the executable's name.

market-agent took a full outage from the alternative: its `command =
["marketagent"]` sat under `ignore_changes`, went stale when the console script
was renamed, and every workload crash-looped on `exec: "investagent": executable
file not found`. The Terraform there had already been corrected; the ignore meant
the apply never pushed it, and the az-cli deploy path does not touch `command`.

## First-time setup

The platform must exist first, and the image must exist before the job that
pulls it:

1. Apply `azure-container-apps` (dev).
2. Create the GitHub App **and install it** — two separate actions, and the
   second is easy to miss. Needs `Metadata`, `Contents` and `Pull requests`,
   all read-only, and **All repositories** so a new repo is covered with no
   config change. Changing permissions on an existing installation raises a
   request that has to be accepted before it takes effect.
3. Merge to `main` here — `cd-tag` mints a version, `cd-publish` pushes the image.
4. **Check the GHCR package is public** — the job has no pull secret, so a
   private package fails the pull. On the first release here it was already
   public and linked to the repository, which the
   `org.opencontainers.image.source` label in the Dockerfile is what earns.
   Worth confirming rather than assuming:
   `gh api user/packages/container/repo-agent%2Frepoagent --jq .visibility`
5. `make apply ENV=dev`.
6. `make secrets`, and run what it prints.
7. `make start`, then check the digest arrived.

Ordering, not placeholders: a placeholder image seeds a revision you then have
to remember to replace.

## CI

Thin callers of reusable workflows in
[jay-withers/workflows](https://github.com/jay-withers/workflows), pinned by
commit SHA with the tag as a comment. Because they are reusable-workflow calls,
the status check contexts are namespaced `<caller job id> / <reusable job name>`
— read them off `gh pr checks` rather than inferring them.

`ci-container-build` is **not** a required check: it is path filtered, so a
docs-only pull request never runs it, and a required check that never reports
leaves the pull request pending for ever rather than failing it.

`ci-terraform` plans `dev` only, unlike the other Terraform repos. This
configuration resolves the shared environment with a data source, so a plan
against an environment the platform was never applied to fails at plan time.

Generated input/output reference: [`terraform/README.md`](terraform/README.md).
